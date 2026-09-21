import asyncio
import time

import pytest
from fastapi.testclient import TestClient
from life_circle.models import RouteObservation

from app.config import Settings
from app.contracts import TaskResultResponse, TaskStatusResponse, map_business_status
from app.main import create_app


def config(**kwargs):
    return Settings(_env_file=None, analysis_provider="synthetic", **kwargs)


def test_empty_qps_in_example_means_unconfigured():
    assert Settings(_env_file=None, analysis_qps="").analysis_qps is None


def body(key="request-1", **kwargs):
    return {"center": {"lng": 116.404, "lat": 39.915}, "coordinateSystem": "bd09ll",
            "budget": 200, "clientRequestId": key, **kwargs}


def finished(client, task):
    end = time.monotonic() + 10
    while time.monotonic() < end:
        result = client.get(f"/api/analyses/{task}").json()
        if result["status"] in ("completed", "failed", "cancelled"):
            return result
        time.sleep(.01)
    pytest.fail("task did not finish")


def test_real_algorithm_result_and_idempotency():
    with TestClient(create_app(config())) as client:
        response = client.post("/api/analyses", json=body())
        assert response.status_code == 202
        task = response.json()["taskId"]
        assert client.post("/api/analyses", json=body()).json()["taskId"] == task
        state = finished(client, task)
        assert state["status"] == "completed"
        assert 0 < state["requests"] <= 200
        result = client.get(f"/api/analyses/{task}/result").json()
        TaskResultResponse.model_validate(result)
        assert result["responseType"] == "result"
        assert result["businessStatus"] == "partial"
        assert result["dataSource"] == "synthetic"
        assert result["facilitiesStatus"] == "not_integrated"
        assert result["isochrone"]["geometry"]["type"] == "MultiPolygon"
        assert result["isochrone"]["coordinateSystem"] == "bd09ll"
        assert result["isochrone"]["algorithm"] == "local-multicross-e82"
        assert [band['minutes'] for band in result['isochrone']['timeBands']] == [15]
        assert result["isochrone"]["statistics"]["network_requests"] == 0
        assert client.post(f"/api/analyses/{task}/cancel").json()["status"] == "completed"
        assert client.post("/api/analyses", json=body(budget=400)).status_code == 409


def test_n05_task_models_are_explicit_and_openapi_is_typed():
    with TestClient(create_app(config())) as client:
        created = client.post("/api/analyses", json=body("contract")).json()
        TaskStatusResponse.model_validate(created)
        assert created["responseType"] == "task"
        assert created["businessStatus"] is None
        state = finished(client, created["taskId"])
        TaskStatusResponse.model_validate(state)
        assert state["status"] == "completed" and state["businessStatus"] == "partial"
        spec = client.get("/openapi.json").json()
        paths = spec["paths"]
        assert paths["/api/analyses"]["post"]["responses"]["202"]["content"]["application/json"]["schema"]["$ref"].endswith("TaskStatusResponse")
        assert paths["/api/analyses/{task_id}/result"]["get"]["responses"]["200"]["content"]["application/json"]["schema"]["$ref"].endswith("TaskResultResponse")


@pytest.mark.parametrize("quality,facilities_status,facilities,expected", [
    ("usable", "complete", [{"id": "1"}], "complete"),
    ("usable", "complete", [], "empty"),
    ("usable", "complete", None, "partial"),
    ("partial", "not_integrated", None, "partial"),
    ("partial", "complete", [{"id": "1"}], "partial"),
    ("partial", "complete", [], "partial"),
    ("insufficient", "not_integrated", None, "failed"),
])
def test_business_status_mapping(quality, facilities_status, facilities, expected):
    assert map_business_status(quality=quality, facilities_status=facilities_status,
                               facilities=facilities) == expected


@pytest.mark.parametrize("change", [{"budget": 1}, {"budget": True}, {"coordinateSystem": "wgs84"},
    {"center": {"lng": 116, "lat": 85}}, {"center": {"lng": "116", "lat": 40}}])
def test_invalid_requests(change):
    with TestClient(create_app(config())) as client:
        assert client.post("/api/analyses", json=body(**change)).status_code == 422


def test_missing_config_unknown_task_and_cors():
    settings = Settings(_env_file=None, analysis_provider="baidu", baidu_map_ak="", analysis_qps=None)
    with TestClient(create_app(settings)) as client:
        assert client.post("/api/analyses", json=body("missing-config")).status_code == 503
        assert client.get("/api/analyses/missing").status_code == 404
        assert client.get("/api/analyses/missing/result").status_code == 404
        assert client.post("/api/analyses/missing/cancel").status_code == 404
        response = client.options("/api/analyses", headers={"Origin": "http://127.0.0.1:5173",
            "Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type"})
        assert response.status_code == 200


def test_busy_cancel_no_late_result():
    calls = []

    class SlowProvider:
        network = False
        identity = ("slow",)

        async def query_walking_time(self, origin, destination, deadline):
            calls.append(destination)
            await asyncio.sleep(60)
            return RouteObservation(destination, 100)

    with TestClient(create_app(config(), provider_factory=lambda _: SlowProvider())) as client:
        task = client.post("/api/analyses", json=body()).json()["taskId"]
        assert client.get(f"/api/analyses/{task}/result").status_code == 409
        assert client.post("/api/analyses", json=body("request-2")).status_code == 409
        assert client.post(f"/api/analyses/{task}/cancel").status_code == 202
        assert finished(client, task)["status"] == "cancelled"
        count = len(calls)
        time.sleep(.05)
        assert len(calls) == count <= 2
        assert client.get(f"/api/analyses/{task}/result").status_code == 409
        assert client.post(f"/api/analyses/{task}/cancel").json()["status"] == "cancelled"


def test_cancel_lost_response_by_request_key_never_creates_work():
    class Slow:
        network = False
        identity = ("lost-response",)

        async def query_walking_time(self, *args):
            await asyncio.sleep(60)

    with TestClient(create_app(config(), provider_factory=lambda _: Slow())) as client:
        assert client.post('/api/analyses/by-request/missing/cancel').status_code == 404
        assert len(client.app.state.analyses.jobs) == 0
        task = client.post('/api/analyses', json=body('lost-key')).json()['taskId']
        response = client.post('/api/analyses/by-request/lost-key/cancel')
        assert response.status_code == 202 and response.json()['taskId'] == task
        assert finished(client, task)['status'] == 'cancelled'
        assert len(client.app.state.analyses.jobs) == 1


def test_insufficient_is_completed_not_empty():
    class Unknown:
        network = False
        identity = ("unknown",)

        async def query_walking_time(self, origin, destination, deadline):
            return RouteObservation(destination, reason="no_result")

    with TestClient(create_app(config(), provider_factory=lambda _: Unknown())) as client:
        task = client.post("/api/analyses", json=body()).json()["taskId"]
        state = finished(client, task)
        assert state["status"] == "completed" and state["businessStatus"] == "failed"
        result = client.get(f"/api/analyses/{task}/result").json()["isochrone"]
        assert result["geometry"] is None
        assert result["quality"] == "insufficient"


def test_expiry_and_retention():
    with TestClient(create_app(config())) as client:
        manager = client.app.state.analyses
        task = client.post("/api/analyses", json=body()).json()["taskId"]
        finished(client, task)
        manager.jobs[task].finished_at -= 1801
        assert client.get(f"/api/analyses/{task}").status_code == 404


def test_terminal_capacity_is_twenty():
    from app.analyses import AnalysisInput, AnalysisManager, Job
    manager = AnalysisManager(config())
    for index in range(21):
        job = Job(str(index), AnalysisInput(**body(str(index))), "synthetic", status="completed",
                  finished_at=time.monotonic())
        manager.jobs[job.task_id] = job
    manager.prune()
    assert len(manager.jobs) == 20 and "0" not in manager.jobs


def test_geometry_reconstruction_keeps_health_and_cancel_responsive(monkeypatch):
    import threading
    entered, release = threading.Event(), threading.Event()

    from tools import endpoint_boundary_band
    original = endpoint_boundary_band.connect_estimate
    def blocked(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(endpoint_boundary_band, "connect_estimate", blocked)
    with TestClient(create_app(config())) as client:
        task = client.post("/api/analyses", json=body()).json()["taskId"]
        try:
            assert entered.wait(5)
            assert client.get("/health").status_code == 200
            assert client.get(f"/api/analyses/{task}").json()["stage"] == "reconstructing"
            client.post(f"/api/analyses/{task}/cancel")
        finally:
            release.set()
        assert finished(client, task)["status"] == "cancelled"
        assert client.get(f"/api/analyses/{task}/result").status_code == 409


def test_exception_is_sanitized_and_client_closed():
    closed = []

    class Failing:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        @property
        def network(self):
            raise RuntimeError("credential-bearing transport error")

    with TestClient(create_app(config(), provider_factory=lambda _: Failing())) as client:
        task = client.post("/api/analyses", json=body()).json()["taskId"]
        state = finished(client, task)
        assert state["status"] == "failed" and closed
        assert "credential" not in str(state)


def test_shared_qps_gate_spaces_jobs_and_honors_deadline():
    from app.analyses import RateGate
    now = [100.0]
    sleeps = []

    async def sleep(delay):
        sleeps.append(delay)
        now[0] += delay

    async def run():
        gate = RateGate(2, clock=lambda: now[0], sleep=sleep)
        assert await gate.wait(110)
        assert await gate.wait(110)
        assert not await gate.wait(100.75)
        assert await gate.wait(110)
        assert sleeps == [0, .5, .5]
    asyncio.run(run())


def test_shared_qps_gate_rechecks_after_early_timer_wakeup():
    from app.analyses import RateGate
    now = [100.0]
    async def sleep(delay):
        now[0] += delay - .01 if delay > .02 else delay
    async def run():
        gate = RateGate(3, clock=lambda: now[0], sleep=sleep)
        sends = []
        for _ in range(5):
            assert await gate.wait(110)
            sends.append(now[0])
        assert all(b-a >= 1/3-1e-9 for a,b in zip(sends,sends[1:]))
    asyncio.run(run())


def test_shutdown_cancels_provider_and_closes_context():
    closed = []

    class Slow:
        network = False
        identity = ("shutdown",)

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            closed.append(True)

        async def query_walking_time(self, *args):
            await asyncio.sleep(60)

    app = create_app(config(), provider_factory=lambda _: Slow())
    with TestClient(app) as client:
        task = client.post("/api/analyses", json=body()).json()["taskId"]
    assert closed
    assert app.state.analyses.jobs[task].status == "cancelled"
