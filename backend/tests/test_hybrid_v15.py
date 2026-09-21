import asyncio
import json
import math
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from shapely.geometry import Point, box, mapping, shape

from app.algorithms.hybrid_isochrone.cache import EvidenceSession
from app.algorithms.hybrid_isochrone.engine import HybridIsochroneProvider
from app.algorithms.hybrid_isochrone.extent import contains, computation_extent
from app.algorithms.hybrid_isochrone.models import HybridConfig
from app.algorithms.hybrid_isochrone.polygon_builder import build_polygon
from app.algorithms.hybrid_isochrone.sampler import exploratory_points
from app.config import Settings
from app.geo.projection import MetricProjection
from app.hybrid_contracts import HybridResultResponse
from app.main import create_app
from test_hybrid_isochrone import ORIGIN, FastGate, MockProvider
from tools.validate_hybrid import metrics, wilson, claim


@pytest.mark.anyio
async def test_display_shell_does_not_replace_water_clipped_calculation():
    from app.algorithms.hybrid_isochrone.hard_obstacles import LocalObstacles
    p = MetricProjection(32651)
    x, y = p.origin(ORIGIN)
    obstacles = LocalObstacles(water=box(x-20, y-2000, x+20, y+2000))
    provider = MockProvider(p, lambda x, y: math.hypot(x, y))
    engine = HybridIsochroneProvider(p, provider, FastGate(), obstacles=obstacles)
    result = await engine.compute(ORIGIN, HybridConfig(max_baidu_requests=110))
    calculated, displayed = shape(result['geometry']), shape(result['displayGeometry'])
    assert calculated.is_valid and displayed.is_valid
    assert displayed.area > calculated.area
    assert result['diagnostics']['hard_obstacle_overlap_m2'] < 1e-6
    assert result['displayGeometry']['coordinateSystem'] == 'bd09ll'
    assert result['requests_used'] == len(provider.calls) <= 110


@pytest.mark.anyio
async def test_all_request_sources_and_normalized_coordinates_must_be_inside_square():
    p = MetricProjection(32651)
    provider = MockProvider(p, lambda x, y: 500)
    session = EvidenceSession(ORIGIN, p, HybridConfig(), provider, FastGate())
    x, y = session.origin_xy
    for source in ("GEOMETRIC", "OSM_GUIDED", "BOUNDARY_REFINEMENT", "TOPOLOGY_RISK"):
        assert await session.query((x+1201, y), source) is None
        # Even an inside proposal cannot smuggle an outside exact request.
        assert await session.query((x, y), source, request_coordinate=session.coordinate((x, y+1201))) is None
    assert provider.calls == [] and session.requests_used == 0 and not session.events
    point = await session.query((x+1199, y+1199))
    assert point and len(provider.calls) == 1  # square corners, not a 1200m disk
    assert contains(session.origin_xy, point.xy, session.config)


def test_square_corner_support_and_geometry_extent():
    cfg = HybridConfig()
    extent = computation_extent((0, 0), cfg)
    assert extent.bounds == (-1200, -1200, 1200, 1200)
    points = list(exploratory_points((0, 0), 1200, 20))
    assert len(points) == 20 and len(set(points)) == 20
    assert all(extent.covers(Point(p)) for p in points)
    assert all(math.dist((0, 0), p) > 1600 for p in points[:4])
    with pytest.raises(ValueError):
        HybridConfig(analysis_half_width_m=1201)
    from app.algorithms.hybrid_isochrone.models import Sample, Evidence
    with pytest.raises(ValueError, match="polygon_sample_outside_extent"):
        build_polygon([Sample("outside", (1201, 0), (0, 0), "test", "test", 0, Evidence())], (0, 0), cfg)


@pytest.mark.anyio
async def test_edge_reachability_is_truncation_not_a_time_boundary():
    p = MetricProjection(32651)
    provider = MockProvider(p, lambda x, y: 100)
    engine = HybridIsochroneProvider(p, provider, FastGate())
    result = await engine.compute(ORIGIN, HybridConfig(max_baidu_requests=110, initial_direction_count=8))
    assert result["extent_truncated"] is True and result["quality"] == "partial"
    assert "computation_extent_truncated" in result["warnings"]
    assert all(contains(engine.session.origin_xy, s.xy, engine.session.config) for s in engine.session.samples)
    assert all(shape(result[k]).is_valid for k in ("geometry", "unknown_region", "computation_extent") if result[k])


def test_old_ledger_cannot_silently_acquire_new_extent_semantics():
    p = MetricProjection(32651)
    ledger = dict(version="hybrid-v1", origin=ORIGIN, config=HybridConfig().model_dump(),
                  requests_used=0, events=[], samples=[])
    with pytest.raises(ValueError, match="algorithm_version"):
        EvidenceSession(ORIGIN, p, HybridConfig(), MockProvider(p, lambda x, y:100), FastGate(), seed_ledger=ledger)


def test_hybrid_api_contract_lookup_degraded_readiness_and_errors(tmp_path):
    app = create_app(Settings(_env_file=None, hybrid_ledger_dir=tmp_path),
                     hybrid_provider_factory=lambda p, c: MockProvider(p, lambda x, y: math.hypot(x, y)))
    app.state.hybrid.gate = FastGate()
    with TestClient(app) as client:
        body = dict(origin=dict(zip(("lng", "lat"), ORIGIN)), coordinate_system="bd09ll",
                    client_request_id="lookup", config={"max_baidu_requests": 40})
        task = client.post("/api/v1/analysis/hybrid", json=body).json()["taskId"]
        assert client.get("/api/v1/analysis/hybrid/by-request/lookup").json()["taskId"] == task
        assert client.post("/api/v1/analysis/hybrid", json={**body, "config": {"max_baidu_requests": 41}}).json()["code"] == "hybrid_request_id_conflict"
        for _ in range(400):
            status = client.get(f"/api/v1/analysis/hybrid/{task}").json()
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(.01)
        assert status["status"] == "completed", status
        response = client.get(f"/api/v1/analysis/hybrid/{task}/result").json()
        model = HybridResultResponse.model_validate(response)
        assert model.isochrone.readiness.mode == "degraded"
        assert model.isochrone.quality != "usable"
        assert model.facilities_status == "not_integrated"
        assert "diagnostics" not in response["isochrone"]
        assert (tmp_path / task / "diagnostics.json").is_file()
        from app.hybrid_api import content_hash
        assert model.result_hash == content_hash(response["isochrone"])
        from tools.validate_hybrid import freeze
        diagnostics = json.loads((tmp_path / task / "diagnostics.json").read_text(encoding="utf-8"))
        ledger = json.loads((tmp_path / task / "ledger.json").read_text(encoding="utf-8"))
        plan = freeze(response, diagnostics, ledger)
        assert plan == freeze(response, diagnostics, ledger)
        assert len(plan["cases"]) == 100
        generated = {tuple(s["request_coordinate"]) for s in ledger["samples"]}
        assert not generated.intersection(tuple(r["coordinate"]) for r in plan["cases"])
        assert plan["result_hash"] == model.result_hash
        assert client.post("/api/v1/analysis/hybrid/by-request/lookup/cancel").json()["status"] == "completed"
        assert client.get("/api/v1/analysis/hybrid/by-request/missing").json()["code"].endswith("not_found_or_expired")
        bad = {**body, "config": {"analysis_half_width_m": 1600}}
        assert client.post("/api/v1/analysis/hybrid", json=bad).json()["code"] == "hybrid_invalid_request"
        app.state.hybrid.jobs[task].finished = time.monotonic()-1801
        assert client.get(f"/api/v1/analysis/hybrid/{task}").status_code == 404


def test_cancel_by_request_during_preparation_sends_nothing(tmp_path, monkeypatch):
    providers = []
    def slow(*args, **kwargs):
        time.sleep(.1)
        return SimpleNamespace(warnings=[])
    monkeypatch.setattr("app.hybrid_api.OsmGuidance", slow)
    def factory(p, c):
        providers.append(MockProvider(p, lambda x,y:100))
        return providers[-1]
    app = create_app(Settings(_env_file=None, hybrid_ledger_dir=tmp_path), hybrid_provider_factory=factory)
    with TestClient(app) as client:
        body = dict(origin=dict(zip(("lng", "lat"), ORIGIN)), coordinate_system="bd09ll", client_request_id="cancel")
        client.post("/api/v1/analysis/hybrid", json=body)
        assert client.post("/api/v1/analysis/hybrid/by-request/cancel/cancel").json()["status"] == "cancelling"
        time.sleep(.3)
        assert client.get("/api/v1/analysis/hybrid/by-request/cancel").json()["status"] == "cancelled"
        assert not providers


def test_failure_stage_is_recorded_without_exception_text(tmp_path, monkeypatch):
    def broken(*args, **kwargs):
        raise ValueError("SECRET-AK-in-exception")
    monkeypatch.setattr("app.hybrid_api.OsmGuidance", broken)
    app = create_app(Settings(_env_file=None, hybrid_ledger_dir=tmp_path), hybrid_provider_factory=lambda p,c:None)
    with TestClient(app) as client:
        r = client.post("/api/v1/analysis/hybrid", json=dict(origin=dict(zip(("lng", "lat"), ORIGIN)),
                 coordinate_system="bd09ll", client_request_id="fail"))
        task = r.json()["taskId"]
        time.sleep(.1)
        assert client.get(f"/api/v1/analysis/hybrid/{task}").json()["status"] == "failed"
        text = (tmp_path / task / "failure.json").read_text()
        assert "SECRET" not in text and json.loads(text)["stage"] == "preparing"


def make_validation(perfect=True, unknown=False):
    cases, samples = [], []
    for i in range(100):
        prediction = i % 2 == 0
        coordinate = [float(i), 0.]
        group = ("boundary", "inferred_fill", "interior", "exterior")[i//25]
        cases.append(dict(id=str(i), group=group, coordinate=coordinate, prediction=None if unknown and i < 21 else prediction))
        truth = prediction if perfect or i >= 11 else not prediction
        samples.append(dict(request_coordinate=coordinate, evidence=dict(reachable=truth)))
    return dict(cases=cases, generation_requests=400), dict(samples=samples, requests_used=100)


def test_validation_acceptance_does_not_hide_abstentions_or_failures():
    plan, ledger = make_validation()
    assert metrics(plan, ledger)["outcome"] == "passed"
    plan, ledger = make_validation(perfect=False)
    assert metrics(plan, ledger)["outcome"] == "failed"
    plan, ledger = make_validation(unknown=True)
    result = metrics(plan, ledger)
    assert result["outcome"] == "insufficient_evidence"
    assert result["overall"]["algorithm_unknown"] == 21
    assert result["overall"]["accuracy"] == 1
    ledger["samples"] = []
    assert metrics(plan, ledger)["overall"]["api_unknown"] == 100
    assert wilson(0, 0) is None
    assert wilson(90, 100)[0] < .9 < wilson(90, 100)[1]


def test_live_claim_cannot_be_repeated(tmp_path):
    path = tmp_path / "started.json"
    claim(path, {"budget": 500})
    with pytest.raises(FileExistsError):
        claim(path, {"budget": 500})


def test_reference_readiness_keeps_nonintersecting_water_warning_but_rejects_missing_data():
    from tools.hybrid_fixture import fixture
    from tools.validate_hybrid import reference_ready
    result = fixture().model_dump(mode="json", by_alias=True)
    readiness = result["isochrone"]["readiness"]
    for key in tuple(readiness):
        if key not in ("mode", "warnings"):
            readiness[key] = True
    readiness["warnings"] = ["hard_obstacle_water_lines_unresolved"]
    d = {"diagnostics": {"unresolved_water_lines_affecting_shell": 0}}
    assert reference_ready(result, d)
    assert readiness["mode"] == "degraded"  # never relabel the frozen response
    d["diagnostics"]["unresolved_water_lines_affecting_shell"] = 1
    assert not reference_ready(result, d)
    d["diagnostics"]["unresolved_water_lines_affecting_shell"] = 0
    readiness["graph_available"] = False
    assert not reference_ready(result, d)
