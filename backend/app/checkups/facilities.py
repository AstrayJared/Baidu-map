"""§4.2–§4.4: one facility retrieval over the computed boundary.

The pieces already exist and are only composed here: the adaptive planner
chooses the queries, the place session meters them, the cache sits outside the
quota pool, and the POI pipeline normalizes, classifies and merges what comes
back. What this module adds is the composition and the reporting.

Three properties are the point of it:

* **The counting region is the boundary.** Rows are clipped to the query range
  (an ``around`` search is a superset) and then counted where the computed
  boundary says they are, not where the search range does. A facility just
  outside the boundary is kept as evidence, never as a finding.
* **Failure is never a zero.** A page that failed, a stage that could not build a
  transport or a boundary that is no region all leave a named reason; an empty
  group is only ever the result of a run that actually happened.
* **The two quality axes stay apart.** ``queryStatus`` says what the queries
  established, ``catalogCompleteness`` is ``unverified`` because a keyword search
  cannot verify a real directory — no count here is a completeness claim.
"""
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field

from life_circle.coordinates import LocalProjection, normalize as normalize_point
from shapely.geometry import Point, box
from shapely.ops import unary_union

from .. import catalog
from ..cache import KeyedCache
from ..cache import Entry
from ..contracts import Issue
from ..poi import plan as poi_plan
from ..poi.planner import RULES
from ..poi.cache import CachedPages
from ..poi.models import PoiCollectRequest, Point as WirePoint
from ..poi.normalize import merge_entities, normalize
from ..poi.online import (PROCESSING_STEP_LIMIT, OnlinePlanner, RunLimits, clip_to_domain,
                          coarse_blocks)
from ..quota import PLACE, attach_token
from .facility_stage import local_region, query_domain
from .models import QUERY_PADDING_M, CheckupRequest, FacilityGroup
from .places import POI_POOL, PlacesUnavailable, declared_identity, open_online

# Budgets the request itself cannot raise (``models.MAX_POI_REQUESTS``), which is
# why the stage reads its limit from the request object rather than from here.

# One page's own outcome, as the planner records it, plus where its bytes came
# from. The page's ``source`` stays the data source of the run; the cache's view
# of the same page is a different question and gets its own key.
FETCH_SOURCE = 'fetchSource'

#: The reasons a page was never sent because this application may not send more:
#: the task's own bucket or the day's allowance. Named here so the report says how
#: many pages that cost, instead of leaving a reader to count stop reasons.
ALLOWANCE_REFUSALS = frozenset({'task_budget_exhausted', 'daily_budget_exhausted'})


@dataclass(frozen=True)
class FacilityOutcome:
    """What the stage established, and what the revision must say about it.

    ``requests`` counts the pages the planner attempted, ``network_requests`` the
    upstream attempts they cost: a page served by the cache is one of the first
    and not one of the second, and a retry is one of both.
    """
    group: FacilityGroup | None
    status: str
    requests: int = 0
    network_requests: int = 0
    issues: list[Issue] = field(default_factory=list)


def stale_for(group: FacilityGroup, before: dict | None, after: dict | None, origin) -> str | None:
    """Why a retrieval counted against boundary ``before`` cannot stand for ``after``.

    A recompute keeps the paid retrieval only when the new boundary leaves every
    record on the side it was counted on. A boundary that grew could take in a
    record the run set aside as outside, so growth needs a new retrieval; one that
    shrank must still hold every counted facility. None when the retrieval stands.
    """
    if before == after:
        return None
    if before is None or after is None:
        return 'boundary_missing'
    old, new = local_region(before, origin), local_region(after, origin)
    if new.difference(old).area > 1.0:
        return 'boundary_grew'
    projection = LocalProjection(origin)
    def counted(location):
        return new.covers(Point(*projection.to_local((location['lng'], location['lat']))))
    for item in group.facilities + group.review_candidates + group.excluded_candidates:
        if not any(counted(o['location']) for o in item.get('observations') or [item]):
            return 'counted_facility_left_boundary'
    return None


def _refusal(reason: str, major_categories, *, status: str = 'failed', detail: str | None = None) -> FacilityOutcome:
    """A stage that could not run: null group, a named reason, no counts."""
    messages = {
        'missing_ak': '未配置百度地图 AK，设施检索不可用。',
        'synthetic_mode_offline': '当前为离线合成模式，未接入真实检索服务，设施检索未运行。',
        'task_budget_exhausted': '本任务的 POI 预算已用尽，设施检索未开始。',
        'budget_too_small': '本任务的 POI 预算不足以覆盖所选的设施类别，设施检索未开始。',
        'daily_budget_exhausted': '今天的地点检索额度已用尽，设施检索未开始；额度在次日（北京时间）恢复。',
        'rate_limit': '百度地点检索返回并发超限，有界重试后仍未通过；本轮已暂停并保留已取到的页面。',
        'quota': '百度地点检索的配额已用尽，设施检索未完成。',
        'permission': '百度地点检索的权限校验未通过，设施检索未完成；请核对服务权限与 AK 配置。',
        'parameter_error': '百度地点检索拒绝了请求参数，设施检索未完成。',
        'deadline_reached': '已到任务截止时间，设施检索未完成。',
        'no_boundary_geometry': '成圈结果没有几何，设施检索没有可确定的范围。',
        'empty_boundary_geometry': '成圈几何不是可用区域，设施检索没有可确定的范围。',
        'invalid_boundary_geometry': '成圈几何无法解析，设施检索没有可确定的范围。',
        'empty_query_domain': '查询范围为空，设施检索未开始。',
    }
    if detail is not None:
        messages[reason] = detail
    # A reason the engine sent up is reported as itself, not as a missing message.
    message = messages.get(reason, f'设施检索不可用：{reason}')
    return FacilityOutcome(group=None, status=status, issues=[
        Issue(code="FACILITIES_UNAVAILABLE", message=message, scope="facilities",
              severity="error")])


def _status_for(query_status: str) -> str:
    """The snapshot's ``facilitiesStatus``.

    It describes the *query* axis: a completed retrieval is complete as a
    retrieval, which is not a claim that the catalogue is complete. A cancelled
    stage keeps what it retrieved, so it is partial, never a failure.
    """
    return {'completed': 'complete', 'cancelled': 'partial'}.get(query_status, query_status)


def budget_refusal(estimate: poi_plan.InitialEstimate, majors) -> FacilityOutcome | None:
    """Whether the first round needs more new calls than this retrieval may make.

    The decision is made on *new* calls, never on the cold page count: a first
    round the cache can already answer is not a series of requests, and refusing
    it because the day's allowance is spent would refuse work this deployment does
    not have to do. For the same reason a retrieval the cache can *partly* answer
    is never refused: those pages are evidence already paid for, and what may
    actually be sent is still bounded by the pool at the moment of dispatch.

    Only a cold retrieval — nothing reusable at all — is refused up front, and it
    is refused with its numbers, so the caller can reduce categories or raise the
    budget instead of watching a retrieval that cannot start.
    """
    reason = poi_plan.admission_refusal(estimate)
    if reason is None:
        # Either nothing has to leave the process, or part of the first round is already
        # cached and is worth running for: what may actually be sent stays bounded by
        # the pool at the moment of dispatch (§4.2.5).
        return None
    detail = (
        f'本次检索首轮需要 {estimate.initial_page_count} 页地点检索'
        f'（{estimate.blocks} 个查询分块 × {estimate.minor_categories} 个检索小类，'
        f'每类先取主关键词一次），缓存可复用 {estimate.reusable_initial_page_count} 页，'
        f'还需新增 {estimate.estimated_new_initial_calls} 次网络调用，'
        f'而本任务预算只剩 {estimate.remaining_task_budget} 次'
        + ('' if estimate.remaining_daily_budget is None
           else f'、本应用今天还剩 {estimate.remaining_daily_budget} 次')
        + '。请减少设施类别或提高本任务预算后重试；'
          '翻页与细分还需要更多次数，首轮够用不代表一定查完。')
    return _refusal(reason, majors, detail=detail)


async def collect_facilities(payload: CheckupRequest, snapshot, *, settings, context, quota,
                             budget, cache: KeyedCache, places_factory=None,
                             progress=None, store=None) -> FacilityOutcome:
    """Run the one facility retrieval of a task and report it.

    ``places_factory`` substitutes the transport for an offline or fixture-backed
    run; without it the deployment's own key and client are used, or the stage is
    refused by name.

    ``progress(sent, limit)`` is called once before the first page and after every
    page the planner receives: ``sent`` is what this stage has reserved from its
    pool so far (a cache hit reserves nothing), ``limit`` the pool's allowance.
    """
    origin = normalize_point((payload.center.lng, payload.center.lat))
    majors = tuple(payload.facilities.categories)
    categories = catalog.poi_keys(majors)
    if snapshot.geometry is None:
        return _refusal('no_boundary_geometry', majors)
    try:
        domain, widened = query_domain(snapshot.geometry, origin, QUERY_PADDING_M)
        boundary = local_region(snapshot.geometry, origin)
    except ValueError as exc:
        return _refusal(str(exc), majors)
    # Full-catalog rounds retain the existing supplemental keyword strategy.
    # The selected core scope keeps the upstream primary-keyword plan.
    full_catalog = set(majors) == set(catalog.majors())
    queries = ({key: tuple(RULES['queries'][key]) for key in categories} if full_catalog
               else poi_plan.primary_queries(categories))
    # What this retrieval will ask for, and what of it the cache can answer, from
    # the planner's own first round and the cache's own keys — not from a second,
    # approximately equal count. No transport is opened and nothing is sent.
    provider, api_version = declared_identity(settings, places_factory)
    plan = poi_plan.initial_plan(domain, origin, categories, provider=provider,
                                 api_version=api_version, queries=queries)
    estimate = poi_plan.estimate(plan, cache, task_id=context.task_id,
                                 remaining_task_budget=budget.remaining(POI_POOL),
                                 remaining_daily_budget=quota.remaining(PLACE))
    refusal = budget_refusal(estimate, majors)
    if refusal is not None and not full_catalog:
        return refusal
    started = time.time()
    # What this stage sends is what it reserves: the pool counts the attempt
    # before the request goes out, so a refused or unclear one is part of it.
    reserved = budget.spent.get(POI_POOL, 0)
    async with AsyncExitStack() as stack:
        try:
            places = (places_factory(settings) if places_factory is not None
                      else await open_online(settings, stack))
        except PlacesUnavailable as exc:
            return _refusal(exc.reason, majors)
        source = 'baidu_place' if places.network else 'synthetic'
        # The cache wraps the metered session, so a hit never enters the pool's
        # scheduling point and §9.2's single reservation stays the only one.
        session = attach_token(places.session(quota.place, budget=budget,
                                              deadline=context.deadline), context.token)
        async def journaled(sequence, page):
            key = fetch.key(sequence, page)
            budget.request_context['poi'] = {'key': key, 'sequence': sequence, 'page': page}
            before = budget.reservations.get('poi')
            answer, reason = await session(sequence, page)
            reservation = budget.reservations.get('poi')
            if store is not None and reservation is not None and reservation != before:
                store.complete_request(reservation, answer, reason)
            return answer, reason
        fetch = CachedPages(cache, journaled,
                            provider=places, task_id=context.task_id)
        checkpoint = None if store is None else store.checkpoint(context.task_id)
        if checkpoint is not None:
            for saved in store.saved_pages(context.task_id):
                cache.entries[saved['key']] = Entry(saved['payload'], context.task_id, saved['obtainedAt'])
        limit = budget.remaining(POI_POOL)
        async def pages(sequence, page):
            major = catalog.major_of(sequence['category'])
            label = f"检索设施 · {catalog.MAJOR_LABELS.get(major, major)}"
            if progress is not None:
                progress(budget.spent.get(POI_POOL, 0) - reserved, limit, label)
            try:
                return await fetch(sequence, page)
            finally:
                if progress is not None:
                    progress(budget.spent.get(POI_POOL, 0) - reserved, limit, label)
        pages.metadata = lambda sequence, page: fetch.uses.get(
            (sequence['tileId'], sequence['category'], sequence['query'], page), {})
        planner = OnlinePlanner(domain=domain, origin=origin, categories=list(categories),
                                queries=queries, budget=limit,
                                source=source, token=context.token,
                                deadline=context.deadline, checkpoint=checkpoint,
                                on_checkpoint=None if store is None else
                                    lambda value: store.save_checkpoint(context.task_id, value),
                                spent=lambda: budget.spent.get(POI_POOL, 0) - reserved,
                                # Two ceilings: the task bucket and the day bound what
                                # may be *sent*; this one bounds local scheduling, so a
                                # cached page can never spend the allowance the missing
                                # pages need, and a spent allowance cannot discard the
                                # pages the cache can still answer.
                                limits=RunLimits(processing_steps=PROCESSING_STEP_LIMIT,
                                                 drain_after_budget_refusal=True))
        pages = fetch
        if progress is not None:
            progress(0, limit)
        result = await planner.run(pages)
    return _report(payload, snapshot, result, fetch, domain, widened, boundary, origin,
                   places, source, majors, budget, started, estimate,
                   network=budget.spent.get(POI_POOL, 0) - reserved)


#: §5 B2 决策 1：普通体检必须查到完整结果，**最多允许残余 20% 的地段**。
#: 这里定义的是"残余"的口径，不是"现实中有多少设施已被找到"—— 后者永远无法由关键词
#: 检索证明（``catalog_completeness`` 恒为 ``unverified``）。
SHARED_COMPLETION_TARGET = 0.80


def shared_completion(incomplete: dict, boundary, *,
                      target: float = SHARED_COMPLETION_TARGET) -> dict:
    """How much of the boundary *every* selected category finished searching.

    The measure is the **intersection of all categories' finished areas**, not the
    average of per-category coverage. The difference is the whole point: a run that
    finished nine categories and never asked about the tenth scores 90% by average
    and 0% here, and only the second number answers "may a reader treat this report
    as covering the area?". So the gaps are unioned across categories first, and
    overlapping gaps are therefore counted once rather than once per category.

    The denominator is the computed boundary, so the query padding can neither help
    nor hurt the ratio. ``unknown`` is reserved for "cannot be measured" — no
    boundary, or no area to take a fraction of. A run whose queries failed reports
    ``unmet``: unfinished is unfinished, and it must not read as *unknown* just to
    look less conclusive.
    """
    domain_area = float(boundary.area)
    categories = sorted(incomplete or {})
    if domain_area <= 0:
        return {'status': 'unknown', 'reason': 'empty_boundary_area', 'target': target,
                'boundaryAreaM2': 0.0, 'sharedCompletedAreaM2': 0.0,
                'sharedCompletionRatio': None, 'residualRatio': None,
                'categories': categories}
    gaps = [box(x0, y0, x1, y1)
            for bounds in (incomplete or {}).values() for x0, y0, x1, y1 in bounds]
    # Clipped to the boundary: a gap block can stick out past it, and the part that
    # was never inside the reported area is not a residual of the reported area.
    gap = unary_union(gaps).intersection(boundary) if gaps else None
    gap_area = 0.0 if gap is None or gap.is_empty else float(gap.area)
    completed = min(domain_area, max(0.0, domain_area - gap_area))
    ratio = completed / domain_area
    return {'target': target,
            'status': 'met' if ratio >= target else 'unmet',
            'boundaryAreaM2': round(domain_area, 6),
            'sharedCompletedAreaM2': round(completed, 6),
            'sharedCompletionRatio': round(ratio, 6),
            'residualRatio': round(1.0 - ratio, 6),
            'categories': categories}


def _incomplete_regions(incomplete: dict, projection) -> dict:
    """Unfinished query blocks (local metres) → bd09ll polygons, per category."""
    regions = {}
    for category, boxes in (incomplete or {}).items():
        polygons = []
        for x0, y0, x1, y1 in boxes:
            ring = [list(projection.to_geographic(corner))
                    for corner in ((x0, y0), (x1, y0), (x1, y1), (x0, y1), (x0, y0))]
            polygons.append({'type': 'Polygon', 'coordinates': [ring]})
        if polygons:
            regions[category] = polygons
    return regions


def _report(payload, snapshot, result, fetch, domain, widened, boundary, origin, places,
            source, majors, budget, started, estimate, *, network: int) -> FacilityOutcome:
    categories = catalog.poi_keys(majors)
    records, quarantine = [], []
    for observation in result.observations:
        try:
            records.append(normalize(observation.row, observation.provenance, source))
        except ValueError as exc:
            quarantine.append({'reason': str(exc), 'provenance': observation.provenance})
    inside, outside_domain = clip_to_domain(records, domain, origin)
    projection = LocalProjection(origin)
    request = PoiCollectRequest(center=WirePoint(lng=float(payload.center.lng),
                                                  lat=float(payload.center.lat)),
                                coordinate_system='bd09ll', categories=list(categories))
    # ``within`` is the boundary, in metres, inclusive of its edge: a facility on
    # the boundary is inside the area being reported on.
    def counted(point):
        return boundary.covers(Point(*projection.to_local((point['lng'], point['lat']))))

    # Outside the boundary but inside the query range: not counted, but they serve
    # the places near the boundary (§2.3), which is what the padding is for.
    nearby = []
    accepted, review, excluded, outside_boundary, merged = merge_entities(
        inside, request, within=counted, nearby=nearby)
    coverage = result.coverage
    major_queries = {major: sum(len(entry['pageRecords']) for entry in coverage
                                if catalog.major_of(entry['category']) == major)
                     for major in majors}
    major_blocks = {major: sorted({entry['tileId'] for entry in coverage
                                   if catalog.major_of(entry['category']) == major
                                   and entry['pageRecords']}) for major in majors}
    unfinished = {major: [{'tileId': entry['tileId'], 'category': entry['category'],
                            'query': entry['query'], 'status': entry['status'],
                            'stopReason': entry['stopReason']}
                           for entry in coverage if catalog.major_of(entry['category']) == major
                           and entry['status'] != 'completed'] for major in majors}
    completeness = {major: not any(boxes for minor, boxes in result.incomplete.items()
                                   if catalog.major_of(minor) == major) for major in majors}
    for entry in coverage:
        for record in entry['pageRecords']:
            use = fetch.uses.get((record['tileId'], record['category'], record['query'],
                                  record['pageNum']))
            if use is not None:
                record[FETCH_SOURCE], record['obtainedAt'] = use['source'], use['obtainedAt']
                # 这一页是谁付的钱。它跟着页面记录走，也就跟着每个设施的 provenance 走 ——
                # §5 B2 决策 2 的"复用不重置来源期限"因此是可执行的：一条设施明细能不能
                # 提供，由**它的来源**的期限决定，而不是由读它的那次体检决定。
                record['sourceTaskId'] = use.get('sourceTaskId')
    data_times = [record['obtainedAt'] for entry in coverage for record in entry['pageRecords']
                  if record['succeeded'] and 'obtainedAt' in record]
    live = sum(1 for use in fetch.uses.values() if use['source'] == 'live')
    # 这一版明细的数据来源清单：来源 → 付了多少页、其中多少是新取的、最早什么时候取的。
    # 报告据此说明"这些数据是谁的、什么时候到期"，而不必再去读缓存内部状态。
    sources: dict[str, dict] = {}
    for use in fetch.uses.values():
        source_id = use.get('sourceTaskId')
        if source_id is None:
            continue
        item = sources.setdefault(source_id, {'pages': 0, 'live': 0, 'obtainedAt': use['obtainedAt']})
        item['pages'] += 1
        if use['source'] == 'live':
            item['live'] += 1
        item['obtainedAt'] = min(item['obtainedAt'], use['obtainedAt'])
    duplicates = {item['possibleDuplicateGroup'] for item in accepted + review
                  if item['possibleDuplicateGroup']}
    warnings = sorted(
        set(result.warnings)
        | ({'quarantined_records'} if quarantine else set())
        | ({'classification_needs_review'} if review else set())
        | ({'possible_duplicates'} if duplicates else set())
        | ({'outside_boundary_records'} if outside_boundary else set())
        | ({'query_domain_widened'} if widened else set()))
    group = FacilityGroup(
        query_status=result.status, provider=places.identity, api_version=places.api_version,
        data_source=source,
        query_domain={
            'coordinateSystem': 'bd09ll', 'origin': [origin[0], origin[1]],
            'paddingMeters': QUERY_PADDING_M, 'widened': widened,
            'envelopeLocalMeters': list(domain.envelope),
            'polygonLocalMeters': [list(point) for point in domain.polygon],
        },
        data_obtained_at=min(data_times) if data_times else None,
        # The first round this run was planned from, and what of it the cache
        # answered. It is an estimate taken before the first page, published so a
        # reader can see why the retrieval was allowed to start — and it is not a
        # promise that paging and subdivision finish within it.
        initial_plan=estimate.as_contract(),
        # §5 B2 决策 2：这一版明细出自哪几个任务。存的是**因果事实**（谁付的钱、多少页、
        # 什么时候取的），不是"什么时候到期"—— 到期限是一个事件，冻结进不可变修订里
        # 就一定会写错。
        source_tasks=sources,
        counts_by_category={major: sum(1 for item in accepted
                                       if catalog.major_of(item['category']) == major)
                            for major in majors},
        facilities=accepted, nearby_facilities=nearby, review_candidates=review,
        excluded_candidates=excluded,
        # Both kinds of record that are not findings for this region are kept
        # where they are: one whose row could not be read, one outside the query
        # range, and one inside the range but outside the boundary. The reason
        # tells them apart, and a nearby facility stays evidence for the region.
        quarantine=quarantine + outside_domain + outside_boundary, query_coverage=coverage,
        query_incomplete_regions=_incomplete_regions(result.incomplete, projection),
        # 同一个不完整集合的两种呈现：多边形给地图画图，面积比例给"这次体检算不算查完"。
        # 两者出自同一个 ``result.incomplete``，所以地图上的空白与报告里的比例不会互相矛盾。
        query_area_coverage=shared_completion(result.incomplete, boundary),
        statistics={
            'rawRecords': sum(entry['returned'] for entry in coverage),
            'invalidRecords': len(quarantine),
            'outsideDomainRecords': len(outside_domain),
            'outsideBoundaryRecords': len(outside_boundary),
            'nearbyServiceSources': len(nearby),
            'uidMergedRecords': merged,
            'acceptedRecords': len(accepted), 'reviewRecords': len(review),
            'excludedRecords': len(excluded),
            'possibleDuplicateGroups': len(duplicates),
            'queryAttempts': result.attempts, 'budget': result.budget,
            'requestsByMajor': major_queries, 'touchedBlocksByMajor': major_blocks,
            'unfinishedByMajor': unfinished, 'stopReason': result.stop_reason,
            'queryCompleteByMajor': completeness,
            'budgetSpent': budget.spent.get(POI_POOL, 0),
            # Pages, not fetches: a page asked for twice is one page.
            'sequences': len(coverage), 'blocks': len(result.blocks),
            'livePages': live, 'cachedPages': len(fetch.uses) - live, 'pageAttempts': network,
            # ``processedPages`` is what the planner scheduled — a cached replay is
            # one of those — while ``networkCalls`` is what actually left the
            # process. Keeping both is what makes "the allowance bounded the *new*
            # calls, not the work" checkable rather than asserted.
            'processedPages': result.attempts, 'networkCalls': result.network_calls,
            'processingLimit': PROCESSING_STEP_LIMIT,
            'allowanceRefusedPages': sum(1 for entry in coverage for record in entry['pageRecords']
                                         if record.get('reason') in ALLOWANCE_REFUSALS),
            'engineQuality': snapshot.quality, 'elapsedSeconds': round(time.time() - started, 6),
        },
        warnings=warnings, stop_reason=result.stop_reason)
    issues = [Issue(code="FACILITY_WARNING", message=warning, scope="facilities")
              for warning in warnings]
    return FacilityOutcome(group=group, status=_status_for(result.status),
                           requests=result.attempts, network_requests=network, issues=issues)
