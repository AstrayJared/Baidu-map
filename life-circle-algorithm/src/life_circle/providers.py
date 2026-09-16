import math
import os
import re
import time

import httpx

from .coordinates import LocalProjection, normalize
from .models import RouteObservation


class AnalyticProvider:
    network = False
    identity = ("synthetic", "v1")

    def __init__(self, origin, function):
        self.projection = LocalProjection(origin)
        self.function = function
        self.calls = 0

    async def query_walking_time(self, origin, destination, deadline):
        self.calls += 1
        value = self.function(*self.projection.to_local(destination))
        return RouteObservation(destination, None if value is None else float(value), "no_result" if value is None else None)


class BaiduProvider:
    """Explicit opt-in HTTP adapter. Retries and QPS belong to Scheduler.

    A supplied AsyncClient belongs to the caller; otherwise use this provider as
    an async context manager. Only IP-validated server AKs are supported in v1.
    """
    network = True
    endpoint = "https://api.map.baidu.com/directionlite/v1/walking"

    def __init__(self, ak=None, *, client=None, origin_uid=None, destination_uid=None, route_metric="duration"):
        self._ak = ak or os.environ.get("BAIDU_MAP_AK")
        if not self._ak:
            raise ValueError("真实 Provider 需要设置 BAIDU_MAP_AK")
        self.client = client
        self._owns_client = client is None
        self.origin_uid, self.destination_uid = origin_uid, destination_uid
        if route_metric not in ("duration", "distance"):
            raise ValueError("Unsupported route metric")
        self.route_metric = route_metric
        self.identity = ("baidu", "directionlite/v1/walking", "bd09ll", "steps=1", origin_uid, destination_uid, route_metric)

    async def __aenter__(self):
        if self.client is None:
            self.client = httpx.AsyncClient(follow_redirects=False)
        return self

    async def __aexit__(self, *args):
        if self._owns_client and self.client is not None:
            await self.client.aclose()
            self.client = None

    @staticmethod
    def _endpoint(value):
        if not isinstance(value, dict):
            return None
        coordinates = []
        for axis in ("lng", "lat"):
            component = value.get(axis)
            # Baidu walking steps may encode coordinates as decimal strings.
            # Reject booleans, non-finite values and Python-only syntax.
            if type(component) is str:
                component = component.strip()
                if len(component) > 64 or not re.fullmatch(
                    r"[+-]?(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)(?:[eE][+-]?[0-9]+)?",
                    component,
                ):
                    return None
            elif type(component) not in (int, float):
                return None
            try:
                component = float(component)
            except (ValueError, OverflowError):
                return None
            if not math.isfinite(component):
                return None
            coordinates.append(component)
        point = tuple(coordinates)
        try:
            normalize(point)
        except (ValueError, TypeError, OverflowError):
            return None
        return point

    def parse(self, payload, origin, destination):
        def unknown(reason):
            return RouteObservation(destination, reason=reason, endpoint_verified=False)
        if not isinstance(payload, dict) or type(payload.get("status")) is not int:
            return unknown("invalid_response")
        status = payload["status"]
        if status != 0:
            reason = {1: "temporary", 2: "invalid_parameter", 7: "no_result", 301: "quota", 302: "quota", 401: "rate_limit", 402: "rate_limit"}.get(status)
            if status in (101, 102, 200, 201, 202, 203, 210, 211, 220, 230, 240, 250, 251, 252, 260, 261):
                reason = "permission"
            return unknown(reason or "upstream_status")
        result = payload.get("result")
        if not isinstance(result, dict) or not isinstance(result.get("routes"), list):
            return unknown("invalid_response")
        valid = []
        offset_routes = []
        reason = "no_result" if not result["routes"] else "invalid_duration"
        projection = LocalProjection(origin)
        for route in result["routes"]:
            if not isinstance(route, dict):
                continue
            observation = RouteObservation(destination, route.get("duration"))
            if observation.duration is None:
                continue
            steps = route.get("steps")
            start = end = None
            if isinstance(steps, list) and steps and isinstance(steps[0], dict) and isinstance(steps[-1], dict):
                start = self._endpoint(steps[0].get("start_location"))
                end = self._endpoint(steps[-1].get("end_location"))
            origin_shift = math.dist(projection.to_local(start), projection.to_local(origin)) if start else None
            destination_shift = math.dist(projection.to_local(end), projection.to_local(destination)) if end else None
            offset = (origin_shift is not None and origin_shift > 50) or (destination_shift is not None and destination_shift > 50)
            distance = route.get("distance")
            if type(distance) not in (int, float) or not math.isfinite(distance) or distance < 0:
                distance = None
            path = []
            for step in steps if isinstance(steps, list) else []:
                if not isinstance(step, dict) or not isinstance(step.get("path"), str):
                    continue
                for pair in step["path"].split(";"):
                    parts = pair.split(",")
                    point = self._endpoint({"lng": parts[0], "lat": parts[1]}) if len(parts) == 2 else None
                    if point and (not path or path[-1] != point):
                        path.append(point)
            if self.route_metric == "distance" and distance is None:
                reason = "invalid_distance"
                continue
            item = RouteObservation(destination, observation.duration, reason="endpoint_offset" if offset else None,
                observed_duration=observation.duration, endpoint_verified=start is not None and end is not None,
                route_origin=start, route_destination=end, distance_m=distance, route_path=path,
                origin_offset_m=origin_shift, destination_offset_m=destination_shift)
            (offset_routes if offset else valid).append(item)
        candidates = valid or offset_routes
        return min(candidates, key=lambda o: o.distance_m if self.route_metric == "distance" else o.observed_duration) if candidates else unknown(reason)

    async def query_walking_time(self, origin, destination, deadline):
        if self.client is None:
            raise RuntimeError("请通过 async with BaiduProvider() 使用适配器")
        origin, destination = normalize(origin), normalize(destination)
        if time.monotonic() >= deadline:
            return RouteObservation(destination, reason="deadline")
        params = {
            "ak": self._ak, "origin": f"{origin[1]:.6f},{origin[0]:.6f}",
            "destination": f"{destination[1]:.6f},{destination[0]:.6f}",
            "coord_type": "bd09ll", "ret_coordtype": "bd09ll", "steps_info": "1",
        }
        if self.origin_uid:
            params["origin_uid"] = self.origin_uid
        if self.destination_uid:
            params["destination_uid"] = self.destination_uid
        try:
            response = await self.client.get(self.endpoint, params=params, timeout=min(8, deadline - time.monotonic()))
            if response.status_code != 200:
                code = response.status_code
                reason = "rate_limit" if code == 429 else "temporary" if code >= 500 else "permission" if code in (401, 403) else "invalid_parameter" if code == 400 else "http_error"
                return RouteObservation(destination, reason=reason)
            try:
                payload = response.json()
            except ValueError:
                return RouteObservation(destination, reason="invalid_response")
            return self.parse(payload, origin, destination)
        except httpx.TimeoutException:
            return RouteObservation(destination, reason="timeout")
        except httpx.RequestError:
            return RouteObservation(destination, reason="temporary")
