from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict
import json
import socket

from fastapi.testclient import TestClient
from life_circle.models import IsochroneRequest
import networkx as nx
import pytest
from shapely.geometry import LineString, box, mapping, shape

from app.algorithms.osm_offline.engine import OsmOfflineEngine
from app.algorithms.osm_offline.graph_store import GraphStore, PEDESTRIAN_ATTRS, save_graph_cache
from app.config import Settings
from app.geo.coordinates import wgs84_to_bd09
from app.geo.projection import MetricProjection
from app.main import create_app as production_app
from app.osm_api import router as internal_osm_router


def create_app(settings):
    """Retained OSM internals, isolated test-only route."""
    app = production_app(settings)
    app.include_router(internal_osm_router)
    return app

ORIGIN = wgs84_to_bd09(121.5, 31.2)


def fixture(tmp_path, *, coverage=True):
    config = Settings(_env_file=None, osm_data_version="test", walk_speed_mps=1,
                      osm_graph_cache_path=tmp_path / "test.osm-cache")
    projection = MetricProjection(config.osm_metric_crs)
    x, y = projection.origin(ORIGIN)
    g = nx.MultiDiGraph(crs=config.osm_metric_crs, osm_data_version="test")
    for i, dx in enumerate((-1500, 0, 1500)):
        g.add_node(i, x=x+dx, y=y)
    for u, v in ((0, 1), (1, 0), (1, 2), (2, 1)):
        a, b = g.nodes[u], g.nodes[v]
        line = LineString([(a["x"], a["y"]), (b["x"], b["y"])])
        g.add_edge(u, v, **dict.fromkeys(PEDESTRIAN_ATTRS), osmid=1, geometry=line,
                   length=1500, travel_time_s=1500)
    save_graph_cache(g, config.osm_graph_cache_path)
    return config, GraphStore(g, speed=1, crs=config.osm_metric_crs), box(x-5000, y-5000, x+5000, y+5000) if coverage else None


def request():
    return IsochroneRequest(ORIGIN, "bd09ll")


def test_engine_polygon_and_diagnostics(tmp_path):
    cfg, store, coverage = fixture(tmp_path)
    result = OsmOfflineEngine(cfg, store, coverage).compute(request())
    assert result.result.quality == "usable"
    assert result.reachable_network.length == pytest.approx(1800)
    geometry = shape(result.result.geometry)
    assert geometry.is_valid and not geometry.is_empty
    assert geometry.bounds[0] < ORIGIN[0] < geometry.bounds[2]
    assert result.diagnostics["network_requests"] == 0
    assert all(result.diagnostics[key] >= 0 for key in ("snap_ms", "routing_ms", "edge_interval_ms", "polygon_ms", "total_ms"))


def test_extract_boundary_partial_quality(tmp_path):
    cfg, store, _ = fixture(tmp_path)
    x, y = store.projection.origin(ORIGIN)
    result = OsmOfflineEngine(cfg, store, box(x-950, y-1000, x+950, y+1000)).compute(request())
    assert result.result.quality == "partial"
    assert result.diagnostics["coverage_boundary_hit"] is True
    assert "graph_coverage_boundary" in result.result.warnings


def test_missing_coverage_not_usable(tmp_path):
    cfg, store, _ = fixture(tmp_path)
    result = OsmOfflineEngine(cfg, store).compute(request())
    assert result.result.quality == "partial" and result.result.geometry is not None
    assert result.diagnostics["coverage_check_available"] is False


def test_outside_coverage_insufficient(tmp_path):
    cfg, store, _ = fixture(tmp_path)
    result = OsmOfflineEngine(cfg, store, box(0, 0, 10, 10)).compute(request())
    assert result.result.quality == "insufficient"
    assert result.result.stop_reason == "origin_outside_coverage"
    assert result.result.geometry is None


def test_api_offline_and_422_envelope(tmp_path, monkeypatch):
    cfg, store, coverage = fixture(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("OSM runtime attempted network access")
    import life_circle.engine
    monkeypatch.setattr(life_circle.engine, "compute_isochrone", forbidden)
    with TestClient(create_app(cfg)) as client:
        # Windows creates a loopback socketpair to start its event loop first.
        monkeypatch.setattr(socket, "create_connection", forbidden)
        monkeypatch.setattr(socket.socket, "connect", forbidden)
        client.app.state.osm_offline = OsmOfflineEngine(cfg, store, coverage)
        body = {"origin": {"lng": ORIGIN[0], "lat": ORIGIN[1]}, "coordinate_system": "bd09ll", "algorithm": "osm_offline"}
        response = client.post("/api/v1/analysis/osm_offline", json=body)
        assert response.status_code == 200
        payload = response.json()
        assert payload["algorithm"]["algorithm"] == "osm_offline"
        assert payload["algorithm"]["quality"] == "usable"
        assert payload["status"] == "partial"  # Facilities deliberately unexecuted.
        assert "success" not in payload
        bad = client.post("/api/v1/analysis/osm_offline", json={**body, "threshold": 901})
        assert bad.status_code == 422
        assert bad.json()["status"] == "failed"


def test_missing_cache_api_not_empty(tmp_path):
    cfg = Settings(_env_file=None, osm_graph_cache_path=tmp_path / "missing")
    with TestClient(create_app(cfg)) as client:
        response = client.post("/api/v1/analysis/osm_offline", json={"origin": {"lng":121.5,"lat":31.2}, "coordinate_system":"bd09ll"})
        assert response.status_code == 200
        assert response.json()["status"] == "failed"
        assert response.json()["algorithm"]["quality"] == "insufficient"
        assert response.json()["algorithm"]["stopReason"] == "graph_cache_missing"


def test_load_once_concurrent_graph_immutability(tmp_path, monkeypatch):
    cfg, store, coverage = fixture(tmp_path)
    import app.algorithms.osm_offline.engine as module
    original = module.load_graph_cache
    calls = []
    def load(path):
        calls.append(path)
        return original(path)
    monkeypatch.setattr(module, "load_graph_cache", load)
    with TestClient(create_app(cfg)) as client:
        engine = client.app.state.osm_offline
        before = nx.node_link_data(engine.store.graph)
        with ThreadPoolExecutor(max_workers=4) as pool:
            outputs = list(pool.map(engine.compute, [request()]*8))
        assert len(calls) == 1
        assert all(o.result.geometry == outputs[0].result.geometry for o in outputs)
        assert before == nx.node_link_data(engine.store.graph)


def test_bad_cache_config_and_coverage(tmp_path):
    cfg, _, _ = fixture(tmp_path)
    cfg.walk_speed_mps = 2
    assert OsmOfflineEngine.load(cfg).unavailable_reason == "cache_speed_or_cost_mismatch"
    cfg.walk_speed_mps = 1
    cfg.osm_data_version = "other"
    assert OsmOfflineEngine.load(cfg).unavailable_reason == "cache_data_version_mismatch"
    cfg.osm_data_version = "test"
    cfg.osm_coverage_boundary_path = tmp_path / "missing.geojson"
    assert OsmOfflineEngine.load(cfg).coverage_reason == "coverage_check_invalid"


def test_unconfigured_osm_does_not_read_city_cache(tmp_path, monkeypatch):
    cfg, _, _ = fixture(tmp_path)
    cfg.osm_data_version = "unconfigured"
    import app.algorithms.osm_offline.engine as module
    def forbidden(path):
        pytest.fail("Unconfigured service read the whole city cache")
    monkeypatch.setattr(module, "load_graph_cache", forbidden)
    assert OsmOfflineEngine.load(cfg).unavailable_reason == "osm_data_version_unconfigured"


def test_request_typescript_defaults_follow_pydantic():
    from app.contracts import OsmOfflineRequest
    from tools.export_contract import typescript
    generated = typescript([OsmOfflineRequest], request_models=[OsmOfflineRequest])
    assert 'algorithm?: "osm_offline"' in generated
    assert 'threshold?: 900' in generated
    assert 'origin: Origin' in generated


def test_500_uses_existing_envelope(tmp_path, monkeypatch):
    cfg, _, _ = fixture(tmp_path)
    with TestClient(create_app(cfg), raise_server_exceptions=False) as client:
        def broken(request):
            raise RuntimeError("private details")
        monkeypatch.setattr(client.app.state.osm_offline, "compute", broken)
        response = client.post("/api/v1/analysis/osm_offline", json={"origin":{"lng":121.5,"lat":31.2},"coordinate_system":"bd09ll"})
        assert response.status_code == 500
        assert response.json()["errors"][0]["code"] == "INTERNAL_ERROR"
        assert "private details" not in response.text
