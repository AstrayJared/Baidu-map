import asyncio
import json
import math
from pathlib import Path

import httpx
import pytest
from shapely.geometry import Point

from app.algorithms.hybrid_isochrone import HybridConfig, HybridIsochroneProvider
from app.algorithms.hybrid_isochrone.baidu_validator import StrictBaiduProvider, validate_payload
from app.algorithms.hybrid_isochrone.boundary_search import BoundarySearch
from app.algorithms.hybrid_isochrone.cache import EvidenceSession, ReplayProvider
from app.algorithms.hybrid_isochrone.models import Evidence, Sample, Validity
from app.algorithms.hybrid_isochrone.polygon_builder import build_polygon
from app.geo.projection import MetricProjection
from life_circle.models import CancelToken
from tools.test_origin import TEST_ORIGIN, TEST_ORIGIN_URL

ORIGIN = TEST_ORIGIN


def test_hybrid_budget_cap_and_requested_qps():
    from pydantic import ValidationError
    assert HybridConfig(request_qps=3).max_baidu_requests == 400
    with pytest.raises(ValidationError):
        HybridConfig(max_baidu_requests=401)


def test_continuation_allows_slower_pacing_without_resetting_budget():
    p = MetricProjection(32651)
    old = HybridConfig(request_qps=20).model_dump(mode='json')
    ledger = dict(version="hybrid-v1.5", origin=ORIGIN, config=old, requests_used=0, events=[], samples=[])
    s = EvidenceSession(ORIGIN, p, HybridConfig(request_qps=3), MockProvider(p, lambda x,y: 100), FastGate(), seed_ledger=ledger)
    assert s.requests_used == 0 and s.config.max_baidu_requests == 400
    with pytest.raises(ValueError, match='continuation_input_mismatch'):
        EvidenceSession(ORIGIN, p, HybridConfig(max_baidu_requests=399), MockProvider(p, lambda x,y:100), FastGate(), seed_ledger=ledger)


class FastGate:
    interval = 0

    def __init__(self):
        self.attempt_lock = asyncio.Lock()

    async def wait(self, deadline):
        return True

    def completed(self, reason):
        pass


class MockProvider:
    network = False
    identity = ("synthetic-strict",)

    def __init__(self, projection, function):
        self.projection, self.function, self.calls = projection, function, []
        self.origin = projection.origin(ORIGIN)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        pass

    async def query_walking_time(self, origin, destination, deadline):
        self.calls.append(destination)
        xy = self.projection.origin(destination)
        duration = self.function(xy[0] - self.origin[0], xy[1] - self.origin[1])
        if duration is None:
            return Evidence(reason="no_result")
        return Evidence(Validity.REACHABLE if duration <= 900 else Validity.UNREACHABLE, None,
                        duration, origin, destination, 0, 0, 200, 0)


def payload(duration=900, start=ORIGIN, end=ORIGIN):
    def location(p):
        return dict(zip(("lng", "lat"), p)) if p else None
    return {"status": 0, "result": {"routes": [{"duration": duration, "steps": [
        {"start_location": location(start), "end_location": location(end)}]}]}}


@pytest.mark.parametrize("duration,expected", [(899, True), (900, True), (901, False)])
def test_duration(duration, expected):
    e = validate_payload(payload(duration), ORIGIN, ORIGIN, MetricProjection(32651), HybridConfig())
    assert e.reachable is expected


@pytest.mark.parametrize("offset,expected", [(50, True), (50.000001, None)])
def test_offset_inclusive(offset, expected):
    class Identity:
        def origin(self, p):
            return p
    e = validate_payload(payload(end=(offset, 0), start=(0, 0)), (0, 0), (0, 0), Identity(), HybridConfig())
    assert e.reachable is expected


@pytest.mark.parametrize("body", [None, {}, {"status": True}, payload(float("nan")), payload(True), payload(-1), payload(start=None), payload(end=None)])
def test_invalid_never_negative(body):
    e = validate_payload(body, ORIGIN, ORIGIN, MetricProjection(32651), HybridConfig())
    assert e.reachable is None


@pytest.mark.parametrize("status,reason", [(2, "invalid_parameter"), (301, "quota"), (401, "rate_limit"), (101, "permission"), (7, "no_result")])
def test_business_error_states(status, reason):
    e = validate_payload({"status": status}, ORIGIN, ORIGIN, MetricProjection(32651), HybridConfig())
    assert e.reason == reason
    assert e.reachable is None


@pytest.mark.parametrize("http_status", [400, 401, 429, 500])
def test_http_failure_never_accepts_duration(http_status):
    e = validate_payload(payload(100), ORIGIN, ORIGIN, MetricProjection(32651), HybridConfig(), http_status)
    assert e.reachable is None
    assert e.http_status == http_status


@pytest.mark.anyio
async def test_failure_streak_stops_without_offline_fallback():
    class Failing:
        network = False
        identity = ("fail",)
        async def query_walking_time(self, *args):
            return Evidence(Validity.API_ERROR, "timeout")
    p = MetricProjection(32651)
    session = EvidenceSession(ORIGIN, p, HybridConfig(), Failing(), FastGate())
    for i in range(10):
        await session.query((session.origin_xy[0] + i * 100, session.origin_xy[1]))
    assert session.requests_used == 5
    assert session.stop_reason == "consecutive_api_failures"
    assert all(s.evidence.reachable is None for s in session.samples)


@pytest.mark.parametrize("osm,duration,validity,expected", [
    (False, 800, Validity.REACHABLE, True), (True, 1000, Validity.UNREACHABLE, False),
    (True, 500, Validity.OFFSET, None), (False, 500, Validity.OFFSET, None),
    (True, None, Validity.API_ERROR, None)])
def test_cases_a_to_e(osm, duration, validity, expected):
    sample = Sample("p", (0, 0), ORIGIN, "GEOMETRIC", "test", 0,
                    Evidence(validity, duration=duration), {"reachable": osm})
    assert sample.evidence.reachable is expected
    assert sample.to_dict()["osm_baidu_disagreement"] is (expected is not None and expected != osm)


@pytest.mark.anyio
async def test_transport_coord_contract_and_strict_endpoint():
    projection = MetricProjection(32651)
    def handler(request):
        assert request.url.params["coord_type"] == request.url.params["ret_coordtype"] == "bd09ll"
        assert request.url.params["steps_info"] == "1"
        assert request.url.params["origin"] == TEST_ORIGIN_URL
        return httpx.Response(200, json=payload(end=None))
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        async with StrictBaiduProvider("test", projection, HybridConfig(), client=client) as provider:
            e = await provider.query_walking_time(ORIGIN, ORIGIN, float("inf"))
            assert e.reason == "missing_endpoint" and e.reachable is None


@pytest.mark.anyio
async def test_dedup_budget_cancel_and_replay(tmp_path):
    p = MetricProjection(32651)
    config = HybridConfig(max_baidu_requests=2)
    provider = MockProvider(p, lambda x, y: 300)
    s = EvidenceSession(ORIGIN, p, config, provider, FastGate(), path=tmp_path / "ledger.json")
    a, b = await asyncio.gather(s.query(s.origin_xy), s.query(s.origin_xy))
    assert a is b and provider.calls == [a.coordinate]
    await s.query((s.origin_xy[0] + 100, s.origin_xy[1]))
    assert await s.query((s.origin_xy[0] + 200, s.origin_xy[1])) is None
    assert s.requests_used == 2
    replay = ReplayProvider(json.loads((tmp_path / "ledger.json").read_text()))
    assert (await replay.query_walking_time(s.origin, a.coordinate, 0)).reachable is True
    token = CancelToken()
    token.cancel()
    stopped = EvidenceSession(ORIGIN, p, config, provider, FastGate(), token=token)
    assert await stopped.query(stopped.origin_xy) is None
    assert stopped.requests_used == 0


@pytest.mark.anyio
async def test_write_failure_prevents_send(tmp_path, monkeypatch):
    p = MetricProjection(32651)
    provider = MockProvider(p, lambda x, y: 1)
    s = EvidenceSession(ORIGIN, p, HybridConfig(), provider, FastGate(), path=tmp_path / "ledger.json")
    def fail(*args):
        raise OSError("disk full")
    monkeypatch.setattr("app.algorithms.hybrid_isochrone.cache.atomic_dump", fail)
    with pytest.raises(OSError):
        await s.query(s.origin_xy)
    assert not provider.calls


@pytest.mark.anyio
@pytest.mark.parametrize("function", [lambda x, y: 100, lambda x, y: 2000, lambda x, y: None,
                                     lambda x, y: 100 if math.hypot(x, y) < 600 or 1100 < math.hypot(x, y) < 1400 else 1500])
async def test_termination_nonmonotone_and_osm_missing(function):
    p = MetricProjection(32651)
    provider = MockProvider(p, function)
    config = HybridConfig(max_baidu_requests=110, initial_direction_count=8)
    engine = HybridIsochroneProvider(p, provider, FastGate())
    result = await engine.compute(ORIGIN, config)
    assert result["requests_used"] <= 110
    assert len(provider.calls) == len(set(provider.calls))
    assert any(s["reason"] == "independent_2d_exploration" for s in result["diagnostics"]["samples"])
    assert result["diagnostics"]["near_field_requests"] == 0
    assert not any(s["reason"] in ("near_origin_coverage", "origin_neighborhood_confirmation")
                   for s in result["diagnostics"]["samples"])
    assert result["quality"] in ("usable", "partial", "insufficient")


@pytest.mark.anyio
async def test_bracket_converges_with_unknown_midpoint():
    p = MetricProjection(32651)
    provider = MockProvider(p, lambda x, y: None if abs(x - 900) < 1 else 800 if x < 950 else 1000)
    s = EvidenceSession(ORIGIN, p, HybridConfig(max_baidu_requests=30), provider, FastGate())
    search = BoundarySearch(s)
    await search.probe(0, 700)
    await search.probe(0, 1100)
    await search.refine(30)
    assert max(b[0] - a[0] for a, b in search.brackets(0)) <= 50


def test_holes_multipolygon_and_unknown_not_filled():
    config = HybridConfig(max_triangle_edge_m=160, mixed_triangle_target_m=100)
    rows = []
    for x in range(-600, 601, 100):
        for y in range(-600, 601, 100):
            # Annulus and disconnected island; an unknown corner remains masked.
            v = Validity.UNKNOWN if x == -600 and y == -600 else Validity.REACHABLE if 150 < math.hypot(x, y) < 400 or x >= 500 and abs(y) < 150 else Validity.UNREACHABLE
            rows.append(Sample(str(len(rows)), (x, y), (x, y), "GEOMETRIC", "test", 0, Evidence(v)))
    built = build_polygon(rows, (0, 0), config)
    assert built.geometry.is_valid
    assert len(built.geometry.geoms) >= 2
    assert sum(len(g.interiors) for g in built.geometry.geoms) >= 1
    assert not built.geometry.covers(Point(0, 0))
    assert built.unknown.covers(Point(-600, -600))
    assert not built.support.covers(Point(-575, -575))


def test_arbitrary_interior_sample_affects_polygon():
    coords = [(-100, -100), (100, -100), (100, 100), (-100, 100), (11, 13)]
    rows = [Sample(str(i), xy, xy, "GEOMETRIC", "test", 0, Evidence(Validity.UNREACHABLE if i == 4 else Validity.REACHABLE)) for i, xy in enumerate(coords)]
    built = build_polygon(rows, (0, 0), HybridConfig())
    assert not built.geometry.covers(Point(11, 13))
    assert built.geometry.is_valid


@pytest.mark.anyio
async def test_river_bridge_obstacle_and_open_topology(tmp_path):
    import networkx as nx
    from shapely.geometry import LineString, mapping, box
    from shapely.ops import transform
    from app.algorithms.osm_offline.graph_store import GraphStore, PEDESTRIAN_ATTRS
    from app.algorithms.hybrid_isochrone.osm_guidance import OsmGuidance
    p = MetricProjection(32651)
    x, y = p.origin(ORIGIN)
    g = nx.MultiDiGraph(crs="EPSG:32651", osm_data_version="test")
    for i, xy in enumerate([(x - 300, y), (x, y), (x + 300, y)]):
        g.add_node(i, x=xy[0], y=xy[1])
    for u, v in [(0, 1), (1, 0), (1, 2), (2, 1)]:
        line = LineString([(g.nodes[u]["x"], g.nodes[u]["y"]), (g.nodes[v]["x"], g.nodes[v]["y"])])
        tags = dict.fromkeys(PEDESTRIAN_ATTRS)
        tags["bridge"] = "yes"
        g.add_edge(u, v, geometry=line, length=300, travel_time_s=300, osmid=1, **tags)
    store = GraphStore(g, speed=1, crs=32651)
    features = [{"type": "Feature", "geometry": mapping(transform(p.inverse.transform, box(x - 50, y - 800, x + 50, y + 800))),
                 "properties": {"risk_kind": kind}} for kind in ("water", "barrier")]
    risk = tmp_path / "risks.json"
    risk.write_text(json.dumps({"osm_data_version": "test", "features": features}))
    config = HybridConfig(max_baidu_requests=120, initial_direction_count=8)
    guide = OsmGuidance(store, (x, y), config, risk_path=risk, speed=1)
    info = guide.inspect((x, y))
    assert info["risk_score"] >= .6
    assert {"bridge", "water", "barrier"}.issubset(info["risk_kinds"])
    provider = MockProvider(p, lambda dx, dy: math.hypot(dx, dy))
    engine = HybridIsochroneProvider(p, provider, FastGate(), guide)
    result = await engine.compute(ORIGIN, config)
    assert any(s["source"] == "TOPOLOGY_RISK" for s in result["diagnostics"]["samples"])
    assert any(s["osm_baidu_disagreement"] for s in result["diagnostics"]["samples"])
    assert result["requests_used"] <= 120


def test_async_geometry_api_no_facilities(tmp_path, monkeypatch):
    import time
    from fastapi.testclient import TestClient
    from app.main import create_app
    from app.config import Settings
    async def forbidden(*args, **kwargs):
        raise AssertionError("facilities must not be invoked")
    monkeypatch.setattr("app.analyses.analyze_facilities", forbidden)
    app = create_app(Settings(_env_file=None, hybrid_ledger_dir=tmp_path),
                     hybrid_provider_factory=lambda p, c: MockProvider(p, lambda x, y: math.hypot(x, y)))
    app.state.hybrid.gate = FastGate()
    with TestClient(app) as client:
        body = {"origin": {"lng": ORIGIN[0], "lat": ORIGIN[1]}, "coordinate_system": "bd09ll",
                "config": {"max_baidu_requests": 25}, "client_request_id": "test-hybrid"}
        prefix = '/api/v1/analysis/hybrid'
        assert client.get('/health').json()['default_analysis_engine'] == 'baidu'
        paths = client.get('/openapi.json').json()['paths']
        assert '/api/v1/analysis/osm_offline' in paths
        assert '/api/analyses' in paths and prefix in paths
        pure_baidu_body = {"center": {"lng": ORIGIN[0], "lat": ORIGIN[1]},
                           "coordinateSystem": "bd09ll", "budget": 200,
                           "clientRequestId": "test-pure-baidu"}
        assert client.post(prefix, json=pure_baidu_body).status_code == 422
        assert client.post('/api/analyses', json=body).status_code == 422
        response = client.post(prefix, json=body)
        assert response.status_code == 202
        task = response.json()["taskId"]
        assert client.post(prefix, json=body).json()["taskId"] == task
        for _ in range(100):
            status = client.get(f"{prefix}/{task}").json()
            if status["status"] in ("completed", "failed"):
                break
            time.sleep(.01)
        assert status["status"] == "completed", status
        result = client.get(f"{prefix}/{task}/result")
        assert result.status_code == 200
        assert result.json()["facilitiesStatus"] == "not_integrated"
        assert result.json()["algorithm"]["requests_used"] <= 25
        assert result.json()['algorithm']['algorithm'] == 'hybrid'
        assert result.json()['algorithm']['algorithm_version'] == 'hybrid-v1.5.0'
        # The pure-Baidu namespace cannot see or resume a Hybrid task.
        assert client.get(f'/api/analyses/{task}/result').status_code == 404


@pytest.mark.anyio
async def test_explicit_continuation_retains_budget_and_never_retries_lost_response(tmp_path):
    p = MetricProjection(32651)
    config = HybridConfig(max_baidu_requests=3)
    provider = MockProvider(p, lambda x, y: 100)
    session = EvidenceSession(ORIGIN, p, config, provider, FastGate(), path=tmp_path / "first.json")
    sample = await session.query(session.origin_xy)
    old = json.loads((tmp_path / "first.json").read_text())
    old["samples"] = []  # Simulate a response lost after durable reservation.
    resumed = EvidenceSession(ORIGIN, p, config, provider, FastGate(), seed_ledger=old)
    assert resumed.requests_used == 1
    result = await resumed.query(session.origin_xy, request_coordinate=sample.coordinate)
    assert result.evidence.reachable is None
    assert result.evidence.reason == "interrupted_response_unknown_no_retry"
    assert len(provider.calls) == 1
    await resumed.query((session.origin_xy[0] + 100, session.origin_xy[1]))
    await resumed.query((session.origin_xy[0] + 200, session.origin_xy[1]))
    assert resumed.requests_used == 3
    assert await resumed.query((session.origin_xy[0] + 300, session.origin_xy[1])) is None
