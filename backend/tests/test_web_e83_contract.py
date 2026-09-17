"""Protect the production/experiment boundary without exposing tools to the Web.

The strict evidence object is an additive versioned production contract.
"""
from dataclasses import replace

import pytest

from app.contracts import Facility, Geometry, RouteEvidence, PoiEvidence
from life_circle.models import RouteObservation
from tools.endpoint_e83_poi import confirmed_poi

ORIGIN = (116.404, 39.915)
DESTINATION = (116.405, 39.916)


def test_route_observation_preserves_diagnostic_duration_but_invalidates_requested_duration():
    obs = RouteObservation(DESTINATION, 500, reason="endpoint_offset", endpoint_verified=True,
        request_origin=ORIGIN, route_origin=ORIGIN, route_destination=(116.406, 39.916),
        origin_offset_m=0, destination_offset_m=80)
    assert obs.duration is None and obs.observed_duration == 500
    assert obs.endpoint_verified and obs.destination_offset_m == 80
    assert confirmed_poi(obs, ORIGIN, DESTINATION)["status"] == "pending"


@pytest.mark.parametrize("duration,status", [(500, "verified_reachable"), (900, "verified_reachable"),
                                           (901, "verified_unreachable")])
def test_strict_poi_states_require_matching_endpoints(duration, status):
    obs = RouteObservation(DESTINATION, duration, request_origin=ORIGIN,
        route_origin=ORIGIN, route_destination=DESTINATION)
    assert confirmed_poi(obs, ORIGIN, DESTINATION)["status"] == status
    assert confirmed_poi(replace(obs, route_destination=None), ORIGIN, DESTINATION)["status"] == "pending"


def test_geometry_serialization_uses_the_actual_http_alias():
    wire = Geometry(type="MultiPolygon", coordinates=[]).model_dump(by_alias=True)
    assert wire == {"type": "MultiPolygon", "coordinates": [], "coordinateSystem": "bd09ll"}


def test_release_gate_facility_has_strict_poi_status():
    schema = Facility.model_json_schema(mode="serialization")
    assert "poiEvidence" in schema["properties"]
    assert set(schema["$defs"]["PoiEvidence"]["properties"]["status"]["enum"]) == {
        "pending", "verified_reachable", "verified_unreachable"}


def test_release_gate_route_has_endpoint_evidence():
    schema = RouteEvidence.model_json_schema(mode="serialization")
    assert "poiEvidence" in schema["properties"]
    required = {"duration", "reason", "observedDuration", "originOffsetM", "destinationOffsetM",
                "routeOrigin", "routeDestination", "requestOrigin", "destination", "endpointVerified"}
    assert required <= schema["$defs"]["PoiEvidence"]["properties"].keys()
