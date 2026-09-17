"""Strict parsing strategy over the existing, no-retry Baidu transport."""
import math
import httpx

from life_circle.providers import BaiduProvider

from .models import Evidence, Validity


def validate_payload(payload, origin, destination, projection, config, http_status=200):
    if http_status != 200:
        reason = "rate_limit" if http_status == 429 else "permission" if http_status in (401, 403) else "http_error"
        return Evidence(Validity.RATE_LIMIT if http_status == 429 else Validity.API_ERROR, reason, http_status=http_status)
    if not isinstance(payload, dict) or type(payload.get("status")) is not int:
        return Evidence(reason="invalid_response", http_status=http_status)
    status = payload["status"]
    base = dict(http_status=http_status, business_status=status)
    if status:
        reason = {2: "invalid_parameter", 301: "quota", 302: "quota", 401: "rate_limit", 402: "rate_limit", 7: "no_result"}.get(status, "upstream_status")
        if status in (101, 102, 200, 201, 202, 203, 210, 211, 220, 230, 240, 250, 251, 252, 260, 261):
            reason = "permission"
        return Evidence(Validity.RATE_LIMIT if reason == "rate_limit" else Validity.API_ERROR, reason, **base)
    result = payload.get("result")
    routes = result.get("routes") if isinstance(result, dict) else None
    if not isinstance(routes, list):
        return Evidence(reason="invalid_response", **base)
    rejected, accepted = [], []
    for route in routes:
        if not isinstance(route, dict):
            continue
        duration = route.get("duration")
        if type(duration) not in (int, float) or not math.isfinite(duration) or duration < 0:
            rejected.append(Evidence(reason="invalid_duration", **base))
            continue
        steps = route.get("steps")
        start = end = None
        if isinstance(steps, list) and steps and isinstance(steps[0], dict) and isinstance(steps[-1], dict):
            start = BaiduProvider._endpoint(steps[0].get("start_location"))
            end = BaiduProvider._endpoint(steps[-1].get("end_location"))
        evidence = Evidence(duration=duration, returned_origin=start, returned_destination=end, **base)
        if start is None or end is None:
            evidence.reason = "missing_endpoint"
            rejected.append(evidence)
            continue
        try:
            evidence.origin_offset_m = math.dist(projection.origin(origin), projection.origin(start))
            evidence.destination_offset_m = math.dist(projection.origin(destination), projection.origin(end))
        except (ValueError, OverflowError):
            evidence.reason = "invalid_endpoint"
            rejected.append(evidence)
            continue
        if not all(math.isfinite(v) for v in (evidence.origin_offset_m, evidence.destination_offset_m)):
            evidence.reason = "invalid_endpoint"
            rejected.append(evidence)
        elif max(evidence.origin_offset_m, evidence.destination_offset_m) > config.endpoint_offset_limit_m:
            evidence.validity, evidence.reason = Validity.OFFSET, "endpoint_offset"
            rejected.append(evidence)
        else:
            evidence.validity = Validity.REACHABLE if duration <= config.time_limit_seconds else Validity.UNREACHABLE
            evidence.reason = None
            accepted.append(evidence)
    return min(accepted, key=lambda e: e.duration) if accepted else next(
        (e for e in rejected if e.validity == Validity.OFFSET), rejected[0] if rejected else Evidence(reason="no_result", **base))


class StrictBaiduProvider(BaiduProvider):
    def __init__(self, ak, projection, config, **kwargs):
        super().__init__(ak, **kwargs)
        self.projection, self.config = projection, config
        self.identity = (*self.identity, "strict-v1", config.endpoint_offset_limit_m)

    async def __aenter__(self):
        if self.client is None:
            self.client = httpx.AsyncClient(follow_redirects=False, trust_env=False,
                                            transport=httpx.AsyncHTTPTransport(retries=0, trust_env=False))
        return self

    def parse(self, payload, origin, destination):
        return validate_payload(payload, origin, destination, self.projection, self.config)

    def transport_failure(self, destination, reason, *, http_status=None, error_type=None):
        return Evidence(Validity.RATE_LIMIT if reason == "rate_limit" else Validity.API_ERROR,
                        reason, http_status=http_status, transport_error_type=error_type)

    async def query_walking_time(self, origin, destination, deadline):
        result = await super().query_walking_time(origin, destination, deadline)
        if isinstance(result, Evidence):
            return result
        # The base transport returns RouteObservation on HTTP/transport failure.
        return Evidence(Validity.RATE_LIMIT if result.reason == "rate_limit" else Validity.API_ERROR,
                        result.reason or "invalid_response")
