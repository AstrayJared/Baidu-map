"""POI/route evidence over measured reachable sample points, never an area score."""
import math
import time

from shapely.geometry import Point, shape
from life_circle.coordinates import LocalProjection
from life_circle.models import RouteObservation
from life_circle.providers import BaiduProvider
from life_circle.field import business_geometry

from .contracts import AssessmentPoint, CategoryResult, CoverageEvidence, FacilityAnalysis
from .places import PlacesClient
from .place_protocol import STOP_ERRORS
from .request_control import RequestStopped, request_slot
from .rules import DistanceRule, distance_within
from .poi_evidence import poi_evidence, route_evidence

GROUPS = {"shopping": ("market", "supermarket"), "medical": ("pharmacy", "hospital_pharmacy"), "education": ("school",)}
RULE = DistanceRule(metric="walking_route", threshold_m=1000, inclusive=True, tolerance_m=100,
                    assessment_scope="isochrone", category_policy="major_minor")


async def analyze_facilities(result, client, ak, gate, token, *, max_points=9, max_routes_per_group=8, deadline=None, on_progress=None):
    if max_points < 1 or max_routes_per_group < 1:
        raise ValueError("positive sampling limits required")
    start = time.monotonic()
    deadline = min(deadline or start + 300, start + 300)
    origin = result.config.origin
    projection = LocalProjection(origin)
    # Covers the computation square plus a conservative 1.2km search margin.
    extent = max(abs(v) for v in result.local_geometry.bounds) if result.local_geometry is not None and not result.local_geometry.is_empty else result.config.extent
    radius = math.ceil(math.sqrt(2) * extent + 1200)
    places = PlacesClient(client, ak, gate, token)
    facilities, queries = await places.search(origin, radius, deadline)
    geometry = shape(result.geometry) if result.geometry else None
    for item in facilities:
        item.in_circle = geometry.covers(Point(item.location.lng, item.location.lat)) if geometry else None
        item.poi_evidence = poi_evidence(None, origin, (item.location.lng, item.location.lat), item.id)
    candidates = sorted((o for o in result.sample_observations if o.duration is not None and o.duration <= 900 and o.endpoint_verified and geometry is not None and geometry.covers(Point(*o.destination))), key=lambda o: (o.duration, o.destination))
    # Spread selected points through the time range; never claim unmeasured area coverage.
    selected = candidates[:1] if max_points == 1 else candidates if len(candidates) <= max_points else [candidates[round(i*(len(candidates)-1)/(max_points-1))] for i in range(max_points)]
    by_query = {q["category"]: q for q in queries}
    cache, routes, assessments = {}, {}, []
    requests = places.requests
    stop_reason = places.stop_reason
    warnings = ["检索为关键词与分页范围内的结果，不代表全量设施；评估仅代表实测采样点，不代表整片社区。"]
    if len(candidates) > len(selected):
        warnings.append("评估点位已抽样，其余可达点未判定。")
    if any(q["status"] != "complete" for q in queries):
        warnings.append("部分设施查询失败、截断或包含无效记录，未找到设施时保留无法判断。")

    async def route(sample, item):
        nonlocal requests, stop_reason
        key = (sample.destination, item.id)
        if key in cache:
            return cache[key]
        provider = BaiduProvider(ak, client=client, destination_uid=item.id, route_metric="distance")
        value = None
        for attempt in range(2):
            if stop_reason:
                break
            try:
                async with request_slot(gate, token, deadline) as outcome:
                    requests += 1
                    if on_progress:
                        on_progress(requests)
                    value = await provider.query_walking_time(sample.destination, (item.location.lng, item.location.lat), deadline)
                    outcome["reason"] = value.reason
            except RequestStopped:
                break
            if token.cancelled or time.monotonic() >= deadline:
                value = None
                break
            if value.reason in STOP_ERRORS or value.reason in ("auth", "configuration"):
                stop_reason = value.reason
            if value.reason not in ("temporary", "timeout"):
                break
        cache[key] = value
        if sample.destination == origin and value is not None:
            mapped = route_evidence(value, origin, (item.location.lng, item.location.lat), item.id)
            routes[item.id] = mapped
            item.poi_evidence = mapped.poi_evidence
        return value

    async def assess(sample):
        evidence = []
        for major, minors in GROUPS.items():
            items = sorted((f for f in facilities if f.major_category == major),
                           key=lambda f: (math.dist(projection.to_local(sample.destination), projection.to_local((f.location.lng, f.location.lat))), f.id))
            # Geographic separation only prefilters obviously remote candidates;
            # every positive decision uses the returned walking-route distance.
            nearby = [f for f in items if math.dist(projection.to_local(sample.destination), projection.to_local((f.location.lng, f.location.lat))) <= 1200]
            chosen, distance = None, None
            for item in nearby[:max_routes_per_group]:
                value = await route(sample, item)
                usable = value and value.endpoint_verified and value.duration is not None and not (value.duration == 0 and sample.destination != (item.location.lng, item.location.lat))
                within = distance_within(value.distance_m, "walking_route", RULE) if usable else None
                if within is True:
                    chosen, distance = item.id, value.distance_m
                    break
            state = "covered" if chosen else "unknown"
            reason = "verified_walking_route" if chosen else "catalog_completeness_unverified"
            if token.cancelled or time.monotonic() >= deadline:
                if not chosen:
                    state, reason = "unknown", "cancelled_or_deadline"
            evidence.append(CoverageEvidence(category=major, status=state, facility_id=chosen, distance_m=distance, reason=reason))
        return evidence

    # Center-anchored route evidence is a real request for the analysis center; it
    # must not depend on whether this sampler happened to measure the center itself.
    center = next((o for o in result.sample_observations if o.destination == origin), None) or RouteObservation(origin, 0)
    if geometry is not None:
        await assess(center)
    for sample in selected:
        evidence = await assess(sample)
        assessments.append(AssessmentPoint(location={"lng": sample.destination[0], "lat": sample.destination[1]}, duration_s=sample.duration, categories=evidence))
    groups = []
    for major, minors in GROUPS.items():
        for minor in minors:
            groups.append(CategoryResult(category=minor, query_status="complete" if by_query[minor]["status"] == "complete" else "failed" if by_query[minor]["status"] == "failed" else "unknown",
                count_in_circle=sum(f.in_circle is True and f.category == minor for f in facilities) if geometry else None,
                service_status="unknown"))
    status = "failed" if all(q["status"] == "failed" for q in queries) else "partial" if not assessments or len(candidates) > len(selected) or any(q["status"] != "complete" for q in queries) or any(c.status == "unknown" for p in assessments for c in p.categories) else "complete"
    # A measured point cannot establish an area of missing services.
    service_blind_regions = {major: business_geometry(Point(0, 0).buffer(0), projection) for major in GROUPS}
    summary = FacilityAnalysis(status=status, queries=queries, assessments=assessments, candidate_points=len(candidates),
        assessed_points=len(assessments), unassessed_points=len(candidates)-len(assessments), network_requests=requests,
        elapsed_seconds=time.monotonic()-start, search_radius_m=radius, routes=routes,
        service_blind_regions=service_blind_regions, warnings=warnings)
    counts = {major: sum(f.major_category == major and f.in_circle is True for f in facilities) for major in GROUPS}
    report = f"本次检索在估算15分钟圈内记录购物{counts['shopping']}处、医疗{counts['medical']}处、教育{counts['education']}处。评估{len(assessments)}/{len(candidates)}个实测可达点。"
    for major, label in [("shopping", "购物"), ("medical", "医疗"), ("education", "教育")]:
        states = [c.status for p in assessments for c in p.categories if c.category == major]
        report += f"{label}：有设施{states.count('covered')}点、无法判断{states.count('unknown')}点。"
    report += "未评估点不计入盲区；不生成覆盖率、评分或规划等级。"
    return facilities, groups, summary, report
