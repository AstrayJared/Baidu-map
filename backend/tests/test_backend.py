import json
import logging
import subprocess
import sys

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from app.baidu import API_URL, verify_baidu
from app.config import BACKEND_DIR, Settings, load_settings
from app.main import create_app

SECRET = "TEST_SECRET_DO_NOT_LOG_123"
VALID = {"status": 0, "results": [{"uid": "one", "name": "药店", "location": {"lat": 39.915, "lng": 116.404}}]}


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    monkeypatch.delenv("BAIDU_MAP_AK", raising=False)
    monkeypatch.delenv("CORS_ORIGINS", raising=False)


def config(ak=SECRET):
    return Settings(_env_file=None, baidu_map_ak=ak)


@pytest.mark.parametrize("key,configured", [("", False), (SECRET, True)])
def test_health_never_calls_baidu(key, configured, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("health attempted a network request")
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    with TestClient(create_app(config(key))) as client:
        response = client.request("GET", "/health")
    assert response.status_code == 200
    assert response.json() == {
        "status": "ok",
        "baidu_ak_configured": configured,
        "default_analysis_engine": "baidu",
        "osm_state": "unloaded",
    }
    assert SECRET not in response.text


@pytest.mark.parametrize("origin,allowed", [
    ("http://127.0.0.1:5173", True), ("http://localhost:5173", True),
    ("https://untrusted.example", False), ("http://localhost:5174", False),
])
def test_cors(origin, allowed):
    with TestClient(create_app(config())) as client:
        response = client.get("/health", headers={"Origin": origin})
        preflight = client.options("/health", headers={
            "Origin": origin, "Access-Control-Request-Method": "GET",
            "Access-Control-Request-Headers": "Content-Type",
        })
    for result in (response, preflight):
        assert result.headers.get("access-control-allow-origin") == (origin if allowed else None)
        assert "access-control-allow-credentials" not in result.headers
    assert preflight.status_code == (200 if allowed else 400)


def test_environment_overrides_dotenv(tmp_path, monkeypatch):
    env = tmp_path / ".env"
    env.write_text('BAIDU_MAP_AK=file-value\nCORS_ORIGINS=["https://team.example"]', encoding="utf-8")
    monkeypatch.setenv("BAIDU_MAP_AK", SECRET)
    settings = Settings(_env_file=env)
    assert settings.baidu_map_ak.get_secret_value() == SECRET
    assert settings.cors_origins == ["https://team.example"]
    assert SECRET not in repr(settings)


@pytest.mark.parametrize("origin", ["*", "https://*.example", "http://localhost:5173/", "https://user:pass@example.com", "file:///tmp"])
def test_reject_unsafe_origins(origin):
    with pytest.raises(ValidationError):
        Settings(_env_file=None, cors_origins=[origin])


def test_config_error_is_sanitized(monkeypatch):
    monkeypatch.setenv("CORS_ORIGINS", SECRET)
    with pytest.raises(RuntimeError) as error:
        load_settings()
    assert SECRET not in str(error.value)


def test_missing_key_does_not_send():
    def forbidden(request):
        pytest.fail("missing AK must not send")
    result = verify_baidu(config(" "), transport=httpx.MockTransport(forbidden))
    assert result["outcome"] == "missing_ak"
    assert result["http_status"] is None


@pytest.mark.parametrize("payload,outcome", [
    (VALID, "success"), ({"status": 0, "results": []}, "success"),
    ({"status": 210, "message": SECRET}, "baidu_error"),
    ({"status": 0}, "invalid_response"),
    ({"status": "0", "results": []}, "invalid_response"),
    ({"status": False, "results": []}, "invalid_response"),
    ({"status": 0, "results": [{"uid": SECRET}]}, "invalid_response"),
    ({"status": 0, "results": [{"uid": "x", "name": "x", "location": {"lat": 100, "lng": 0}}]}, "invalid_response"),
    ([SECRET], "invalid_response"),
])
def test_response_validation_and_safe_logs(payload, outcome, caplog):
    requests = []
    caplog.set_level(logging.DEBUG)
    def handle(request):
        requests.append(request)
        assert request.url.params["ak"] == SECRET
        assert request.url.params["coord_type"] == "3"
        assert request.url.params["location"] == "39.915,116.404"
        return httpx.Response(200, json=payload)
    result = verify_baidu(config(), transport=httpx.MockTransport(handle))
    assert len(requests) == 1
    assert result["outcome"] == outcome
    assert SECRET not in json.dumps(result) + caplog.text
    assert API_URL not in caplog.text
    if outcome == "success":
        assert result["result_count"] == len(payload["results"])


@pytest.mark.parametrize("kind,outcome", [
    ("timeout", "timeout"), ("network", "network_error"),
    ("http", "http_error"), ("redirect", "http_error"),
    ("invalid_json", "invalid_response"), ("unexpected", "internal_error"),
])
def test_failures_no_retry_or_leak(kind, outcome, caplog):
    calls = []
    caplog.set_level(logging.DEBUG)
    def handle(request):
        calls.append(request)
        if kind == "timeout":
            raise httpx.ReadTimeout(SECRET, request=request)
        if kind == "network":
            raise httpx.ConnectError(SECRET, request=request)
        if kind == "unexpected":
            raise RuntimeError(SECRET)
        if kind == "redirect":
            return httpx.Response(302, headers={"Location": "https://example.com/?ak=" + SECRET})
        return httpx.Response(503 if kind == "http" else 200, text=SECRET)
    result = verify_baidu(config(), transport=httpx.MockTransport(handle))
    assert len(calls) == 1
    assert result["outcome"] == outcome
    assert SECRET not in json.dumps(result) + caplog.text


def test_cli_missing_key_exit_and_record(tmp_path, monkeypatch):
    # Windows may drop an empty variable in a child process, allowing .env fallback.
    # Nonempty whitespace survives inheritance and Settings strips it to missing.
    monkeypatch.setenv("BAIDU_MAP_AK", " ")
    record = tmp_path / "record.jsonl"
    result = subprocess.run([sys.executable, "-m", "app.smoke", "--record", str(record)],
                            cwd=BACKEND_DIR, capture_output=True, text=True)
    assert result.returncode == 1
    assert json.loads(result.stdout)["outcome"] == "missing_ak"
    assert record.read_text(encoding="utf-8").strip() == result.stdout.strip()
    assert not result.stderr
