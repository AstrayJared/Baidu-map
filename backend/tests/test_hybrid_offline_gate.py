"""An offline deployment refuses the Hybrid engine by name instead of dialling out.

``osm_hybrid`` validates its boundary against endpoints Baidu actually returned.
The offline harnesses supply that evidence locally through an injected provider;
with none injected there is no local stand-in, and the engine used to fall through
to ``StrictBaiduProvider``. ``ANALYSIS_PROVIDER=synthetic`` therefore admitted a
task that could only reach for the network -- measured against a real deployment
as five attempts at ``api.map.baidu.com``, ending in ``consecutive_api_failures``
and an isochrone with no geometry.

Admission now refuses before a task exists, which is what ``places.py`` already
does for the facility stage (``synthetic_mode_offline``) and ``BaiduE82Engine``
already does for a missing walking key.
"""
import sqlite3
from pathlib import Path

from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app

from test_checkup_facilities import OfflineHybrid, body, terminal


def deployment(tmp_path, **overrides):
    """An explicitly configured deployment: never the ambient ``backend/.env``."""
    values = dict(_env_file=None, analysis_provider="synthetic", baidu_map_ak="",
                  checkup_dir=tmp_path / "checkups", hybrid_ledger_dir=tmp_path / "ledgers",
                  quota_ledger_path=tmp_path / "quota.sqlite3")
    values.update(overrides)
    return create_app(Settings(**values))


def tasks_in(app) -> list:
    path = app.state.checkups.store.path
    if not Path(path).exists():
        return []
    with sqlite3.connect(path) as connection:
        return connection.execute("SELECT task_id FROM tasks").fetchall()


def test_an_offline_deployment_refuses_the_hybrid_engine_before_a_task_exists(tmp_path):
    app = deployment(tmp_path)
    with TestClient(app) as client:
        response = client.post("/api/v2/checkups", json=body(engine="osm_hybrid"))
        assert response.status_code == 503
        payload = response.json()
        assert payload["code"] == "checkup_engine_unavailable"
        assert "离线合成模式" in payload["message"]
        # The refusal is at admission: nothing was created and nothing was queued.
        assert tasks_in(app) == []


def test_the_offline_refusal_holds_even_when_a_key_is_configured(tmp_path):
    # ``places.py`` makes this promise for the facility stage -- "an offline
    # deployment never reaches the real service, even with a key set" -- and the
    # engine half of it must not depend on the key being absent.
    app = deployment(tmp_path, baidu_map_ak="k" * 32)
    with TestClient(app) as client:
        response = client.post("/api/v2/checkups", json=body(engine="osm_hybrid"))
    assert response.status_code == 503
    assert response.json()["code"] == "checkup_engine_unavailable"


def test_the_offline_refusal_is_about_the_hybrid_engine_not_every_engine(tmp_path):
    # E8.2 has a local stand-in, so the same offline deployment still serves it.
    # Refusing everything offline would be a different, worse bug.
    app = deployment(tmp_path)
    with TestClient(app) as client:
        created = client.post("/api/v2/checkups", json=body(engine="baidu_e82"))
        assert created.status_code == 202
        assert terminal(client, created.json()["taskId"])["status"] == "completed"


def test_an_injected_provider_keeps_the_hybrid_engine_available(tmp_path):
    # The offline harnesses inject one; the gate must not refuse them.
    app = create_app(
        Settings(_env_file=None, analysis_provider="synthetic", baidu_map_ak="",
                 checkup_dir=tmp_path / "checkups", hybrid_ledger_dir=tmp_path / "ledgers",
                 quota_ledger_path=tmp_path / "quota.sqlite3"),
        hybrid_provider_factory=lambda projection, config: OfflineHybrid(projection))
    with TestClient(app) as client:
        created = client.post("/api/v2/checkups", json=body(engine="osm_hybrid"))
        assert created.status_code == 202
        assert terminal(client, created.json()["taskId"])["status"] == "completed"


def test_baidu_mode_without_a_walking_key_refuses_the_hybrid_engine_too(tmp_path):
    # The same hole from the other side: no key means the engine would build a
    # provider around an empty one and find out over the network.
    app = deployment(tmp_path, analysis_provider="baidu", baidu_map_ak="")
    with TestClient(app) as client:
        response = client.post("/api/v2/checkups", json=body(engine="osm_hybrid"))
    assert response.status_code == 503
    payload = response.json()
    assert payload["code"] == "checkup_engine_unavailable"
    assert "AK" in payload["message"]


def test_a_key_keeps_the_hybrid_engine_admissible_in_baidu_mode(tmp_path):
    # The gate must not be a blanket ban: with a key and no injection the engine
    # is admitted as before, and reports the ordinary notes.
    app = deployment(tmp_path, analysis_provider="baidu", baidu_map_ak="k" * 32)
    with TestClient(app) as client:
        engines = {item["engineId"]: item
                   for item in client.get("/api/v2/capabilities").json()["engines"]}
    hybrid = engines["osm_hybrid"]
    assert hybrid["label"] == "OSM＋百度"
    assert any("百度实际返回" in note for note in hybrid["notes"])


def test_capabilities_say_the_engine_is_unavailable_offline(tmp_path):
    app = deployment(tmp_path)
    with TestClient(app) as client:
        engines = {item["engineId"]: item
                   for item in client.get("/api/v2/capabilities").json()["engines"]}
    hybrid = engines["osm_hybrid"]
    assert "离线不可用" in hybrid["label"]
    assert any("没有替身" in note for note in hybrid["notes"])
    # The claim it cannot honour must not survive: an offline deployment must not
    # advertise endpoints "validated against what Baidu returned". Naming the
    # requirement while explaining the refusal is not the same claim.
    assert not any("端点由百度实际返回核验" in note for note in hybrid["notes"])
    assert any("离线合成模式" in note for note in hybrid["notes"])
    # What it *will accept* is unchanged: the gate is about the deployment, not
    # about which budgets the engine serves.
    assert hybrid["budgets"] == [200, 400]
    assert hybrid["requiresOsmGraph"] is True
