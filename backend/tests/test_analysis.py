from app import catalog
import json
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from life_circle.models import RouteObservation
from shapely.geometry import shape

from app.contracts import AnalysisResponse, Facility, CategoryResult, BlindPoint
from pydantic import ValidationError
from app.config import Settings
from app.main import create_app
from app.rules import DistanceRule, category_service, distance_within

CASES = json.loads((Path(__file__).parent / "fixtures/n04-boundaries.json").read_text())


@pytest.mark.parametrize("model,fields", [
    (Facility, {"id": "x", "name": "test", "location": {"lng": 121.5, "lat": 31.2}, "in_circle": None}),
    (CategoryResult, {"query_status": "complete", "count_in_circle": 1, "service_status": "unknown"}),
    (BlindPoint, {"location": {"lng": 121.5, "lat": 31.2}, "status": "unknown", "evidence": "test"}),
])
def test_category_levels_keep_legacy_input_and_reject_conflicts(model, fields):
    legacy = model(**fields, category="pharmacy")
    assert legacy.minor_category == "pharmacy" and legacy.major_category == "medical"
    for categories in [
        {"category": "school", "minor_category": "pharmacy", "major_category": "medical"},
        {"category": "pharmacy", "minor_category": "pharmacy", "major_category": "shopping"},
    ]:
        with pytest.raises(ValidationError):
            model(**fields, **categories)


@pytest.fixture
def client(monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("N04/N05 must not access the network")
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", forbidden)
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", forbidden)
    with TestClient(create_app(Settings(_env_file=None, baidu_map_ak="")), raise_server_exceptions=False) as c:
        yield c


@pytest.mark.parametrize("case", CASES["time"], ids=lambda c: c["id"])
def test_time_boundaries(case):
    assert RouteObservation((116.4, 39.9), case["duration"]).reachable is case["expected"]


@pytest.mark.parametrize("case", CASES["distance"], ids=lambda c: c["id"])
def test_distance_boundaries(case):
    assert distance_within(case["value"], case["measured"], DistanceRule(**case["rule"])) is case["expected"]


def test_two_rules_are_independent_and_missing_is_not_blind():
    rule = DistanceRule(metric="walking_route", inclusive=True, tolerance_m=0,
                        assessment_scope="sample", category_policy="per_category")
    assert distance_within(950, "walking_route", rule) is True
    assert RouteObservation((116.4, 39.9), 950).reachable is False
    assert category_service([None, False], True, rule) == "unknown"
    assert category_service([], False, rule) == "unknown"
    assert category_service([], True, rule) == "blind"
    assert category_service([True, None], False, rule) == "covered"
    assert category_service([], True, DistanceRule()) == "unknown"


def test_four_mocks_consistent(client):
    bodies = [client.get(f"/api/v1/analysis/mock/{s}").json() for s in ("complete", "partial", "failed", "empty")]
    for body in bodies:
        assert set(body) == set(bodies[0])
        assert set(body["data"]) == set(bodies[0]["data"])
        AnalysisResponse.model_validate(body)
        assert body["source"] == "mock"
        assert "ak=" not in json.dumps(body).lower()
        for category in body["data"]["categories"]:
            assert category["major_category"] in catalog.majors()
            assert category["minor_category"] == category["category"]
        for facility in body["data"]["facilities"] or []:
            assert facility["major_category"] in catalog.majors()
            assert facility["minor_category"] == facility["category"]
    assert bodies[2]["data"]["facilities"] is None
    assert bodies[3]["data"]["facilities"] == []
    partial_categories = {row['category']: row for row in bodies[1]['data']['categories']}
    assert partial_categories['market']['count_in_circle'] is None
    assert partial_categories['school']['count_in_circle'] is None
    assert partial_categories['pharmacy']['count_in_circle'] == 1


@pytest.mark.parametrize("scenario,expected", [("plane", "partial"), ("local_failure", "partial"), ("global_failure", "failed")])
def test_actual_algorithm_over_fastapi(client, scenario, expected):
    response = client.post("/api/v1/analysis/synthetic", json={
        "origin": {"lng": 116.4, "lat": 39.9}, "coordinate_system": "bd09ll", "scenario": scenario,
    })
    assert response.status_code == 200
    body = response.json()
    assert body["status"] == expected
    assert body["algorithm"]["statistics"]["network_requests"] == 0
    assert 144 <= body["algorithm"]["statistics"]["requests"] <= 400
    assert body["rules"]["distance"]["metric"] == "unconfirmed"
    assert body["data"]["facilities"] is None
    if scenario == "global_failure":
        assert body["data"]["geometry"] is None
        assert body["algorithm"]["quality"] == "insufficient"
    else:
        assert shape(body["data"]["geometry"]).is_valid
        assert not shape(body["data"]["geometry"]).is_empty


@pytest.mark.parametrize("change", [{"budget": 143}, {"budget": 801}, {"coordinate_system":"wgs84"}, {"origin":{"lng":200,"lat":39}}, {"scenario":"baidu"}, {"ak":"SECRET_INPUT"}])
def test_invalid_request_is_safe_contract(client, change):
    request = {"origin":{"lng":116.4,"lat":39.9},"coordinate_system":"bd09ll"} | change
    response = client.post("/api/v1/analysis/synthetic", json=request)
    assert response.status_code == 422
    assert response.json()["errors"][0]["code"] == "INVALID_REQUEST"
    assert "SECRET_INPUT" not in response.text
    AnalysisResponse.model_validate(response.json())


def test_internal_error_is_safe_contract(client, monkeypatch):
    def broken(*args):
        raise RuntimeError("SECRET_INTERNAL_URL")
    monkeypatch.setattr("app.analysis.run_synthetic", broken)
    response = client.post("/api/v1/analysis/synthetic", json={"origin":{"lng":116.4,"lat":39.9},"coordinate_system":"bd09ll"})
    assert response.status_code == 500
    assert "SECRET_INTERNAL_URL" not in response.text
    AnalysisResponse.model_validate(response.json())


def test_openapi_and_post_cors(client):
    spec = client.get("/openapi.json").json()
    for code in ("200", "422", "500"):
        assert spec["paths"]["/api/v1/analysis/synthetic"]["post"]["responses"][code]["content"]["application/json"]["schema"]["$ref"].endswith("AnalysisResponse")
    response = client.options("/api/v1/analysis/synthetic", headers={"Origin":"http://localhost:5173", "Access-Control-Request-Method":"POST", "Access-Control-Request-Headers":"Content-Type"})
    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:5173"
