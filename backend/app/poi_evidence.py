"""Production mapping of existing observations; no experimental runner or new I/O."""
import math

from life_circle.coordinates import LocalProjection, normalize

from .contracts import PoiEvidence, RouteEvidence


def same_endpoint(actual, requested):
    return actual is not None and math.dist(LocalProjection(requested).to_local(actual), (0, 0)) <= 1e-5


def poi_evidence(observation, origin, destination, facility_id):
    # These are the actual normalized arguments sent to the existing Provider.
    origin, destination = normalize(origin), normalize(destination)
    values = dict(facility_id=facility_id, request_origin=origin, destination=destination)
    if observation is None:
        return PoiEvidence(**values, status="pending", reason="missing_observation", duration=None,
            observed_duration=None, endpoint_verified=False, route_origin=None, route_destination=None,
            origin_offset_m=None, destination_offset_m=None)
    obs = observation
    parsed = bool(obs.endpoint_verified and obs.route_origin is not None and obs.route_destination is not None)
    reason = obs.reason
    if reason is None:
        if not parsed:
            reason = "endpoints_unverified"
        elif (not same_endpoint(obs.destination, destination)
              or (obs.request_origin is not None and not same_endpoint(obs.request_origin, origin))
              or not same_endpoint(obs.route_origin, origin)
              or not same_endpoint(obs.route_destination, destination)
              or any(offset is not None and offset > 1e-5
                     for offset in (obs.origin_offset_m, obs.destination_offset_m))):
            reason = "endpoint_mapping_unconfirmed"
        elif obs.duration is None or obs.observed_duration != obs.duration:
            reason = "invalid_duration"
    duration = obs.duration if reason is None else None
    return PoiEvidence(**values,
        status="pending" if duration is None else "verified_reachable" if duration <= 900 else "verified_unreachable",
        reason=reason, duration=duration, observed_duration=obs.observed_duration,
        endpoint_verified=parsed, route_origin=obs.route_origin, route_destination=obs.route_destination,
        origin_offset_m=obs.origin_offset_m, destination_offset_m=obs.destination_offset_m)


def route_evidence(observation, origin, destination, facility_id):
    obs = observation
    return RouteEvidence(distance_m=obs.distance_m, duration_s=obs.duration,
        endpoint_verified=obs.endpoint_verified, reason=obs.reason,
        path=obs.route_path if obs.endpoint_verified else [],
        poi_evidence=poi_evidence(obs, origin, destination, facility_id))
