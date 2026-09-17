from dataclasses import replace
from pathlib import Path

import pytest
from pydantic import ValidationError
from life_circle.models import RouteObservation
from life_circle.coordinates import LocalProjection

from app.contracts import PoiEvidence, Facility, RouteEvidence, AnalysisResponse, TaskStatusResponse, TaskResultResponse, OsmOfflineRequest
from app.hybrid_contracts import HybridRequest, HybridResultResponse, HybridError
from app.poi_evidence import poi_evidence, route_evidence
from tools.endpoint_e83_poi import confirmed_poi
from tools.export_contract import typescript

ORIGIN = (116.404, 39.915)
TARGET = (116.405, 39.915)


def observed(seconds=500):
    return RouteObservation(TARGET, seconds, observed_duration=seconds, request_origin=ORIGIN,
        route_origin=ORIGIN, route_destination=TARGET, origin_offset_m=0, destination_offset_m=0,
        distance_m=600, route_path=[ORIGIN, TARGET])


@pytest.mark.parametrize("seconds", [0, 500, 900, 901])
def test_production_adapter_matches_frozen_strict_endpoint_semantics(seconds):
    obs = observed(seconds)
    evidence = poi_evidence(obs, ORIGIN, TARGET, "poi")
    assert evidence.status == confirmed_poi(obs, ORIGIN, TARGET)["status"]
    assert evidence.duration == seconds
    assert evidence.model_dump(by_alias=True)["observedDuration"] == seconds


@pytest.mark.parametrize("offset", [1, 40, 80])
def test_small_and_large_offsets_are_not_strict_confirmation(offset):
    obs = replace(observed(), route_destination=LocalProjection(TARGET).to_geographic((offset, 0)),
        destination_offset_m=offset, reason="endpoint_offset" if offset > 50 else None)
    evidence = poi_evidence(obs, ORIGIN, TARGET, "poi")
    assert evidence.status == "pending" and evidence.duration is None
    assert evidence.observed_duration == 500 and evidence.endpoint_verified
    assert evidence.destination_offset_m == offset
    assert evidence.route_destination == obs.route_destination
    assert confirmed_poi(obs, ORIGIN, TARGET)["status"] == "pending"


@pytest.mark.parametrize("change", [dict(route_origin=None), dict(endpoint_verified=False),
    dict(request_origin=(116.403, 39.915)), dict(destination=(116.406, 39.915)),
    dict(reason="timeout"), dict(reason="endpoint_offset")])
def test_incomplete_or_failed_observation_cannot_become_verified(change):
    evidence = poi_evidence(replace(observed(), **change), ORIGIN, TARGET, "poi")
    assert evidence.status == "pending" and evidence.reason is not None and evidence.duration is None


def test_missing_observation_is_explicit_pending_not_unreachable():
    evidence = poi_evidence(None, ORIGIN, TARGET, "poi")
    assert evidence.status == "pending" and evidence.reason == "missing_observation"
    assert evidence.observed_duration is None and not evidence.endpoint_verified
    assert evidence.request_origin == ORIGIN and evidence.destination == TARGET


def test_request_origin_comes_from_the_call_site_when_provider_omits_it():
    evidence = poi_evidence(replace(observed(), request_origin=None), ORIGIN, TARGET, "poi")
    assert evidence.request_origin == ORIGIN and evidence.status == "verified_reachable"


@pytest.mark.parametrize("change", [dict(reason="timeout"), dict(status="pending"), dict(duration=901),
    dict(endpointVerified=False), dict(destinationOffsetM=20), dict(observedDuration=700),
    dict(routeDestination=[116.406, 39.915])])
def test_schema_rejects_conflicting_evidence(change):
    wire = poi_evidence(observed(), ORIGIN, TARGET, "poi").model_dump(by_alias=True)
    with pytest.raises(ValidationError):
        PoiEvidence.model_validate({**wire, **change})


def test_route_assembly_retains_both_diagnostics_and_legacy_invalid_duration():
    route = route_evidence(replace(observed(), reason="endpoint_offset", destination_offset_m=80,
        route_destination=(116.406, 39.915)), ORIGIN, TARGET, "poi")
    assert route.duration_s is None and route.poi_evidence.duration is None
    assert route.poi_evidence.observed_duration == 500
    assert route.path and route.poi_evidence.status == "pending"


def test_typescript_matches_current_serialization_contract():
    expected = typescript([AnalysisResponse, TaskStatusResponse, TaskResultResponse, OsmOfflineRequest,
        HybridRequest, HybridResultResponse, HybridError],
        request_models=[OsmOfflineRequest, HybridRequest])
    actual = Path(__file__).resolve().parents[2] / "life-circle-demo/src/api-contract.ts"
    assert actual.read_text(encoding="utf-8") == expected


@pytest.mark.parametrize("field,value", [("duration_s", 501), ("reason", "timeout"), ("endpoint_verified", False)])
def test_route_schema_rejects_legacy_fields_contradicting_strict_proof(field, value):
    wire = route_evidence(observed(), ORIGIN, TARGET, "poi").model_dump(by_alias=True)
    with pytest.raises(ValidationError):
        RouteEvidence.model_validate({**wire, field: value})


def test_facility_schema_rejects_evidence_for_another_identity():
    evidence = poi_evidence(observed(), ORIGIN, TARGET, "poi")
    with pytest.raises(ValidationError):
        Facility(id="another", name="fixture", category="pharmacy", location={"lng":TARGET[0], "lat":TARGET[1]},
                 in_circle=None, poi_evidence=evidence)
