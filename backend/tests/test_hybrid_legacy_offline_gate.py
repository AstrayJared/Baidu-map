"""The legacy Hybrid endpoint must honour the offline guarantee too.

``/api/v1/analysis/hybrid`` reaches the same engine by another door, and its
admission gate asked only whether a writing key was configured:

    if not self.provider_factory and not self.settings.ak_configured:
        fail(503, "baidu_walking_not_configured")

A deployment that had declared itself offline *and* kept a key configured passed
that gate. Measured on a real deployment before this gate existed: ``POST`` returned
202, the job ran, and five attempts at ``api.map.baidu.com`` were refused only by the
harness socket guard. The fix is the same sentence the checkup engine's admission
uses, so one condition does not read as two different things depending on the door.
"""
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from test_checkup_facilities import body
from test_hybrid_offline_gate import deployment


def hybrid_request(request_id="offline-legacy"):
    return {"origin": {"lng": 121.513925, "lat": 31.313079}, "coordinate_system": "bd09ll",
            "client_request_id": request_id, "config": {"max_baidu_requests": 40}}


def test_the_legacy_hybrid_endpoint_refuses_the_same_way(tmp_path):
    # A configured key must not be enough to pass this gate.
    app = deployment(tmp_path, baidu_map_ak="k" * 32)
    with TestClient(app) as client:
        response = client.post("/api/v1/analysis/hybrid", json=hybrid_request())
    assert response.status_code == 503
    payload = response.json()
    assert payload["code"] == "synthetic_mode_offline"
    assert "离线合成模式" in payload["message"]


def test_both_doors_word_the_offline_refusal_identically(tmp_path):
    # One condition, one sentence. The two literals live in different modules and
    # this is what keeps them from drifting apart.
    app = deployment(tmp_path, baidu_map_ak="k" * 32)
    with TestClient(app) as client:
        engine = client.post("/api/v2/checkups", json=body(engine="osm_hybrid"))
        legacy = client.post("/api/v1/analysis/hybrid", json=hybrid_request("offline-second"))
    assert engine.status_code == legacy.status_code == 503
    assert engine.json()["message"] == legacy.json()["message"]


def test_an_injected_provider_keeps_the_legacy_endpoint_available(tmp_path):
    import math

    from test_hybrid_isochrone import MockProvider
    app = create_app(
        Settings(_env_file=None, analysis_provider="synthetic", baidu_map_ak="",
                 checkup_dir=tmp_path / "checkups", hybrid_ledger_dir=tmp_path / "ledgers",
                 quota_ledger_path=tmp_path / "quota.sqlite3"),
        hybrid_provider_factory=lambda projection, config: MockProvider(
            projection, lambda x, y: math.hypot(x, y)))
    with TestClient(app) as client:
        response = client.post("/api/v1/analysis/hybrid", json=hybrid_request())
    assert response.status_code == 202, response.text
