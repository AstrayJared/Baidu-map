"""Bounded legacy facility retrieval with explicit incomplete-query evidence."""
import math
import time

import httpx

from .catalog import CATEGORIES
from .contracts import Facility
from .place_protocol import Pagination, RETRY_ERRORS, STOP_ERRORS, response_error
from .request_control import RequestStopped, request_slot

# Derived from the one dictionary rather than restated here. This endpoint sends
# a single request type per category, which is the category's primary query.
QUERIES = {category.key: category.query for category in CATEGORIES}


def classify(name, tag=""):
    from .classification import classify as shared_classify
    category, status, _ = shared_classify(name, [tag])
    return category if status == 'accepted' else None


def parse_facility(row):
    if not isinstance(row, dict):
        return None, "invalid"
    name, uid = row.get("name"), row.get("uid")
    if not all(isinstance(v, str) and v.strip() for v in (name, uid)):
        return None, "invalid"
    details = row.get("detail_info")
    tag = details.get("classified_poi_tag", "") if isinstance(details, dict) else ""
    category = classify(name, tag if isinstance(tag, str) else "")
    if category is None:
        return None, "excluded"
    try:
        return Facility(id=uid, name=name, category=category, location=row.get("location"), in_circle=None), None
    except ValueError:
        return None, "invalid"


class PlacesClient:
    def __init__(self, client, ak, gate, token):
        self.client, self.ak, self.gate, self.token = client, ak, gate, token
        self.requests = 0
        self.records = []
        self.conflicts = []
        self.stop_reason = None

    async def _attempt(self, origin, query, radius, page, deadline):
        status = code = None
        start = time.monotonic()
        payload, reason = None, "interrupted"
        async with request_slot(self.gate, self.token, deadline) as outcome:
            self.requests += 1
            try:
                response = await self.client.get("https://api.map.baidu.com/place/v3/around", params={
                    "ak": self.ak, "query": query, "location": f"{origin[1]:.6f},{origin[0]:.6f}",
                    "coord_type": 3, "radius": math.ceil(radius), "radius_limit": "true",
                    "scope": 2, "page_size": 20, "page_num": page, "output": "json",
                }, timeout=max(.01, min(8, deadline - time.monotonic())))
                status = response.status_code
                payload = response.json() if status == 200 else None
                code = payload.get("status") if isinstance(payload, dict) and type(payload.get("status")) is int else None
                reason = response_error(status, payload)
            except httpx.TimeoutException:
                reason = "timeout"
            except httpx.RequestError:
                reason = "network_error"
            except (ValueError, TypeError):
                reason = "invalid_response"
            outcome["reason"] = reason
        self.records.append({"query": query, "page": page, "http_status": status, "baidu_status": code,
                             "elapsed_ms": round((time.monotonic()-start)*1000), "reason": reason})
        if self.token.cancelled:
            return None, "cancelled"
        if time.monotonic() >= deadline:
            return None, "deadline"
        return (payload if reason is None else None), reason

    async def page(self, origin, query, radius, page, deadline):
        if self.stop_reason:
            return None, self.stop_reason
        for attempt in range(2):
            try:
                payload, reason = await self._attempt(origin, query, radius, page, deadline)
            except RequestStopped as exc:
                payload, reason = None, exc.reason
            if reason in STOP_ERRORS:
                self.stop_reason = reason
            if reason not in RETRY_ERRORS or attempt:
                return payload, reason

    def _merge(self, rows, requested, by_uid, conflicts, meta):
        for row in rows:
            item, reason = parse_facility(row)
            if reason:
                meta[reason] += 1
                continue
            if item.category != requested:
                meta["excluded"] += 1
            previous = by_uid.get(item.id)
            if previous and (previous.location != item.location or previous.category != item.category):
                conflicts.add(item.id)
                self.conflicts.append({"uid": item.id, "previous": previous.model_dump(), "candidate": item.model_dump()})
                meta["invalid"] += 1
            else:
                by_uid[item.id] = item

    async def search(self, origin, radius, deadline, max_pages=2, categories=None):
        by_uid, metadata, conflicts = {}, [], set()
        selected = tuple(categories) if categories is not None else tuple(QUERIES)
        for requested in selected:
            query = QUERIES[requested]
            meta = {"category": requested, "query": query, "status": "complete", "pages": 0,
                    "returned": 0, "excluded": 0, "invalid": 0, "total": None, "reason": None}
            pagination = Pagination()
            for page in range(max_pages):
                payload, reason = await self.page(origin, query, radius, page, deadline)
                if payload is None:
                    meta.update(status="partial" if pagination.pages else "failed", reason=reason)
                    break
                reason = pagination.consume(payload, max_pages)
                self._merge(payload["results"], requested, by_uid, conflicts, meta)
                meta.update(pages=pagination.pages, returned=pagination.returned, total=pagination.total)
                if reason:
                    if reason != "completed":
                        meta.update(status="truncated" if reason in ("page_limit", "possible_truncation") else "partial", reason=reason)
                    break
            if (meta["invalid"] or meta["excluded"]) and meta["status"] == "complete":
                meta.update(status="partial", reason="invalid_excluded_or_conflicting_records")
            metadata.append(meta)
        for uid in conflicts:
            by_uid.pop(uid, None)
        if conflicts:
            for meta in metadata:
                if meta["status"] == "complete":
                    meta.update(status="partial", reason="uid_conflict")
        return sorted(by_uid.values(), key=lambda p: p.id), metadata
