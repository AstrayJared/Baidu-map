"""§4.2 adaptive online planning; synthetic pages only, never a real transport."""
import asyncio
from collections import Counter
import math
import time

import httpx
import pytest

from app.cache import KeyedCache
from app.poi.cache import CachedPages
from app.poi.models import PoiCollectRequest
from app.poi.online import (CACHED, FINEST_BLOCK_METERS, LIVE, PROCESSING_STEP_LIMIT,
                            OnlinePlanner, PageResponse, QueryBlock, QueryDomain, RunLimits,
                            clip_to_domain, coarse_blocks)
from app.poi.planner import RULES, parameters
from app import catalog
from app.quota import BudgetExhausted, DailyBudgetExhausted
from app.request_control import RequestStopped
from life_circle.coordinates import LocalProjection
from life_circle.models import CancelToken

ORIGIN = (121.514, 31.313)
CATEGORIES = ('market', 'pharmacy', 'primary_school')


def row(uid, location=None):
    return {'uid': uid, 'name': f'合成{uid}', 'location': location or {'lng': ORIGIN[0], 'lat': ORIGIN[1]}}


def payload(rows, total=None):
    return {'status': 0, 'result_type': 'poi_type', 'total': len(rows) if total is None else total,
            'results': rows}


def page_of_one(sequence, page):
    """The default: one row, stated total 1 — a query that ends on its first page."""
    return payload([row(f"synthetic-{sequence['sequenceId']}-{page}")])


def two_pages(sequence, page):
    """Twenty rows and a stated total of forty, so every query asks for a second page."""
    return payload([row(f"synthetic-{sequence['sequenceId']}-{page}-{index}") for index in range(20)],
                   total=40)


class Transport:
    """Just the page-cache identity a wrapped fetch needs."""

    identity, api_version = 'synthetic:planner-tests', '3.0'


class Synthetic:
    """A page source that records what was asked, in the order it was asked."""

    def __init__(self, default=page_of_one):
        self.default, self.calls = default, []

    async def __call__(self, sequence, page):
        self.calls.append((sequence['tileId'], sequence['category'], sequence['query'], page))
        outcome = self.default(sequence, page)
        return outcome if isinstance(outcome, tuple) else (outcome, None)


def build(domain, *, budget=60, categories=CATEGORIES, **kwargs):
    return OnlinePlanner(domain=domain, origin=ORIGIN, categories=categories, budget=budget,
                         source='synthetic', **kwargs)


def entry(result, tile_id, category):
    return next(item for item in result.coverage
                if item['tileId'] == tile_id and item['category'] == category
                and item['query'] == RULES['queries'][category][0])


def local(lng_offset, lat_offset=0.0):
    projection = LocalProjection(ORIGIN)
    lng, lat = projection.to_geographic((lng_offset, lat_offset))
    return {'lng': lng, 'lat': lat}


def test_coarse_blocks_are_the_envelope_split_in_two_keeping_only_intersecting_blocks():
    # A bar across the full width but only the lower part of its envelope: the
    # two upper blocks of the 2×2 split hold nothing of the domain.
    domain = QueryDomain(((0, 0), (100, 0), (100, 45), (0, 45)))
    blocks = coarse_blocks(domain)
    assert [block.tile_id for block in blocks] == ['r0c0', 'r0c1']
    assert [block.bounds for block in blocks] == [(0.0, 0.0, 50.0, 50.0), (50.0, 0.0, 100.0, 50.0)]
    looked = Synthetic()
    planner = build(domain)
    asyncio.run(planner.run(looked))
    assert {call[0] for call in looked.calls} == {'r0c0', 'r0c1'}


def test_a_circular_domain_starts_from_its_four_quadrants():
    planner = build(QueryDomain.circle(1300))
    assert [block.tile_id for block in planner.coarse] == ['r0c0', 'r0c1', 'r1c0', 'r1c1']
    assert [block.bounds for block in planner.coarse] == [
        (-1300.0, -1300.0, 0.0, 0.0), (0.0, -1300.0, 1300.0, 0.0),
        (-1300.0, 0.0, 0.0, 1300.0), (0.0, 0.0, 1300.0, 1300.0)]


def test_a_block_is_searched_with_its_circumscribed_circle_and_documented_parameters():
    planner = build(QueryDomain.circle(1300))
    mapping = next(iter(planner.sequences.values())).mapping
    parameters_sent = parameters(mapping, 0)
    assert parameters_sent['coord_type'] == 3 and parameters_sent['radius_limit'] == 'true'
    assert parameters_sent['scope'] == 2 and parameters_sent['page_size'] == 20
    assert parameters_sent['page_num'] == 0
    assert parameters_sent['radius'] == mapping['radius'] == math.ceil(1300/math.sqrt(2)) + 5
    assert mapping['localMeters'] == [-1300.0, -1300.0, 0.0, 0.0]


def test_the_first_round_issues_every_category_before_any_second_keyword():
    looked = Synthetic(default=two_pages)
    planner = build(QueryDomain.circle(1300))
    result = asyncio.run(planner.run(looked))
    rounds = len(planner.coarse)*len(CATEGORIES)
    first_round = [call for call in looked.calls[:rounds]]
    assert all(call[3] == 0 and call[2] == RULES['queries'][call[1]][0]
               for call in first_round)
    # The rotation is what keeps a category with more keywords from being
    # exhausted first: within each block, every category's first keyword is
    # issued before any category's second.
    for block in planner.coarse:
        calls = [call[1:3] for call in first_round if call[0] == block.tile_id]
        firsts = [index for index, (category, query) in enumerate(calls)
                  if query == RULES['queries'][category][0]]
        assert len(firsts) == len(CATEGORIES)
    assert all(call[2] != RULES['queries'][call[1]][0] or call[3] > 0
               for call in looked.calls[rounds:])
    assert result.status == 'completed' and len(result.blocks) == 4


def test_ten_majors_get_every_coarse_block_before_remaining_minors():
    categories = tuple(catalog.poi_key(catalog.minors_of(major)[0]) for major in catalog.majors())
    looked = Synthetic()
    result = asyncio.run(build(QueryDomain.circle(1300), categories=categories,
                               budget=40).run(looked))
    assert result.attempts == 40 and result.stop_reason == 'budget_exhausted'
    assert [catalog.major_of(call[1]) for call in looked.calls[:10]] == list(catalog.majors())
    assert Counter(catalog.major_of(call[1]) for call in looked.calls) == dict.fromkeys(catalog.majors(), 4)
    assert {call[0] for call in looked.calls} == {'r0c0', 'r0c1', 'r1c0', 'r1c1'}
    assert result.status == 'partial'


def test_sixty_attempts_are_reproducible_with_unequal_minor_counts():
    categories = catalog.poi_keys()
    first, second = Synthetic(), Synthetic()
    left = asyncio.run(build(QueryDomain.circle(1300), categories=categories,
                             budget=60).run(first))
    right = asyncio.run(build(QueryDomain.circle(1300), categories=categories,
                              budget=60).run(second))
    assert first.calls == second.calls
    assert len(first.calls) == left.attempts == right.attempts == 60
    assert set(catalog.major_of(call[1]) for call in first.calls[:40]) == set(catalog.majors())
    assert all(call[3] == 0 for call in first.calls)
    assert left.status == 'partial' and left.incomplete


def test_a_truncated_block_is_queried_again_in_smaller_circles_for_that_keyword_only():
    pharmacy = RULES['queries']['pharmacy'][0]

    def crowded_page(sequence, page):
        if sequence['tileId'] == 'r0c0' and sequence['query'] == pharmacy:
            return payload([row(f"crowded-{page}-{index}") for index in range(20)], total=150)
        return page_of_one(sequence, page)

    looked = Synthetic(default=crowded_page)
    planner = build(QueryDomain.circle(1300), categories=('pharmacy', 'primary_school'), max_pages=2)
    result = asyncio.run(planner.run(looked))
    children = [block for block in result.blocks if block.tile_id.startswith('r0c0.')]
    assert len(children) == 4 and all(block.edge == 650 for block in children)
    repeated = {(call[0], call[1]) for call in looked.calls if call[0].startswith('r0c0.')}
    assert repeated == {(block.tile_id, 'pharmacy') for block in children}
    # The parent keeps its incomplete evidence, and the children resolve it.
    parent = entry(result, 'r0c0', 'pharmacy')
    assert parent['status'] == 'partial' and parent['truncated'] is True
    assert parent['stopReason'] == 'possible_truncation' and parent['pages'] == 2
    assert result.status == 'completed' and result.stop_reason is None


def test_a_finest_block_that_stays_saturated_cannot_report_completion():
    def saturated_page(sequence, page):
        return payload([row(f"crowded-{page}-{index}") for index in range(20)], total=150)

    planner = build(QueryDomain.circle(100), categories=('pharmacy',), max_pages=2)
    assert [block.edge for block in planner.coarse] == [100.0]*4
    result = asyncio.run(planner.run(Synthetic(default=saturated_page)))
    assert result.status == 'partial'
    assert 'finest_block_saturated' in result.warnings
    assert all('.' not in block.tile_id for block in result.blocks)
    assert all(item['truncated'] for item in result.coverage)


def test_a_repeated_page_is_evidence_of_an_unstable_query_not_of_completion():
    def repeating_page(sequence, page):
        return payload([row(f"repeated-{index}") for index in range(20)], total=40)

    planner = build(QueryDomain.circle(1300), categories=('pharmacy',))
    result = asyncio.run(planner.run(Synthetic(default=repeating_page)))
    assert result.status != 'completed'
    assert not any(item['status'] == 'completed' for item in result.coverage)
    assert {item['stopReason'] for item in result.coverage} == {'pagination_anomaly',
                                                                'budget_exhausted'}
    # The anomaly is a density signal, so the block is still worth subdividing.
    assert {block.tile_id for block in result.blocks} > {'r0c0', 'r0c1', 'r1c0', 'r1c1'}
    # A source that never stops repeating keeps subdividing until the task budget
    # is gone: §4.2.5 puts subdivision under that budget rather than outside it.
    assert result.stop_reason == 'budget_exhausted' and result.attempts == 60


def test_a_failed_page_is_retried_once_inside_the_same_budget():
    pharmacy = RULES['queries']['pharmacy'][0]

    def flaky_once():
        """One refusal per block for the first keyword, then clean pages."""
        seen = Counter()

        def fetch(sequence, page):
            if sequence['query'] == pharmacy and page == 0:
                seen[sequence['tileId']] += 1
                if seen[sequence['tileId']] == 1:
                    return None, 'timeout'
            return page_of_one(sequence, page)
        return fetch

    planner = build(QueryDomain.circle(1300), categories=('pharmacy',))
    result = asyncio.run(planner.run(Synthetic(default=flaky_once())))
    assert result.status == 'completed'
    records = entry(result, 'r0c0', 'pharmacy')['pageRecords']
    assert [(record['succeeded'], record['reason']) for record in records] == [
        (False, 'timeout'), (True, None)]
    # Eight first pages, four of which were refused once and retried once.
    assert result.attempts == 12 and len(result.observations) == 8
    starved = build(QueryDomain.circle(1300), categories=('pharmacy',), budget=1)
    failed = asyncio.run(starved.run(Synthetic(default=flaky_once())))
    assert failed.status == 'failed' and failed.stop_reason == 'budget_exhausted'
    assert failed.attempts == 1 and entry(failed, 'r0c0', 'pharmacy')['pages'] == 0
    assert entry(failed, 'r0c0', 'pharmacy')['stopReason'] == 'timeout'


def test_budget_exhaustion_keeps_the_rows_it_obtained_and_says_partial():
    planner = build(QueryDomain.circle(1300), budget=2)
    result = asyncio.run(planner.run(Synthetic()))
    assert result.status == 'partial' and result.stop_reason == 'budget_exhausted'
    assert result.attempts == 2 and len(result.observations) == 2
    assert len(result.coverage) == 4 * sum(len(RULES['queries'][c]) for c in CATEGORIES)
    assert [item['status'] for item in result.coverage][:2] == ['completed', 'completed']
    assert {item['stopReason'] for item in result.coverage[2:]} == {'budget_exhausted'}


@pytest.mark.parametrize('refusal,reason', [
    (RequestStopped('rate_limit'), 'rate_limit'),
    (DailyBudgetExhausted('place', 1600), 'daily_budget_exhausted'),
])
def test_a_scheduling_refusal_stops_the_run_and_never_reads_as_completion(refusal, reason):
    async def refuse(sequence, page):
        raise refusal

    result = asyncio.run(build(QueryDomain.circle(1300)).run(refuse))
    assert result.status == 'failed'
    assert result.stop_reason == reason and reason in result.warnings
    assert {item['stopReason'] for item in result.coverage} == {reason}


def test_clipping_keeps_the_domain_and_names_what_it_dropped():
    domain = QueryDomain.circle(300)
    kept, rejected = clip_to_domain([
        {'sourceUid': 'synthetic-in', 'location': local(10), 'provenance': ['p']},
        {'sourceUid': 'synthetic-edge', 'location': local(300), 'provenance': []},
        {'sourceUid': 'synthetic-out', 'location': local(400, 400), 'provenance': []},
    ], domain, ORIGIN)
    assert [record['sourceUid'] for record in kept] == ['synthetic-in', 'synthetic-edge']
    assert rejected == [{'sourceUid': 'synthetic-out', 'reason': 'outside_query_domain',
                         'provenance': []}]


def test_every_page_record_carries_its_own_request_success_total_and_source():
    looked = Synthetic(default=two_pages)
    result = asyncio.run(build(QueryDomain.circle(1300), categories=('pharmacy',)).run(looked))
    first = result.coverage[0]
    assert [(record['pageNum'], record['requested'], record['succeeded'], record['reason'],
             record['total'], record['returned'], record['source'])
            for record in first['pageRecords']] == [
        (0, True, True, None, 40, 20, 'synthetic'), (1, True, True, None, 40, 20, 'synthetic')]
    assert first['requestedPages'] == [0, 1] and first['successfulPages'] == [0, 1]
    assert first['reportedTotals'] == [{'pageNum': 0, 'total': 40}, {'pageNum': 1, 'total': 40}]
    assert first['pageErrors'] == [] and first['returned'] == 40 and first['total'] == 40
    assert first['source'] == 'synthetic' and first['truncated'] is False
    assert first['localMeters'] == [-1300.0, -1300.0, 0.0, 0.0]


def test_the_schedule_is_a_function_of_the_domain_and_the_budget():
    domain = QueryDomain.circle(1300)
    first, second = Synthetic(default=two_pages), Synthetic(default=two_pages)
    runs = [asyncio.run(build(domain).run(source)) for source in (first, second)]
    assert first.calls == second.calls
    assert runs[0].coverage == runs[1].coverage
    assert runs[0].status == runs[1].status == 'completed'


def test_the_planner_cannot_express_a_complete_catalog():
    result = asyncio.run(build(QueryDomain.circle(1300)).run(Synthetic()))
    assert result.catalog_completeness == 'unverified'
    with pytest.raises(AttributeError):
        result.catalog_completeness = 'complete'


def test_the_finest_block_edge_is_two_hundred_and_fifty_metres():
    block = QueryBlock('r0c0', 0, 0, 500)
    children = block.children()
    assert [child.tile_id for child in children] == ['r0c0.0', 'r0c0.1', 'r0c0.2', 'r0c0.3']
    assert [(child.x, child.y, child.edge) for child in children] == [
        (0.0, 0.0, 250.0), (250.0, 0.0, 250.0), (0.0, 250.0, 250.0), (250.0, 250.0, 250.0)]
    assert children[0].children() == ()
    assert QueryBlock('r0c0', 0, 0, 250).subdividable() is False
    assert QueryBlock('r0c0', 0, 0, FINEST_BLOCK_METERS*2 - 0.1).children() == ()
    with pytest.raises(ValueError, match='invalid_query_block'):
        QueryBlock('r0c0', 0, 0, 0)


def test_an_empty_page_or_a_changed_total_keeps_incomplete_evidence():
    builders = {
        # A page arrives before the stated total does, then the query stops.
        'empty_page': lambda page: payload([], total=30) if page else
                      payload([row(f"empty-{index}") for index in range(20)], total=30),
        # The stated total changes between pages, so the paging cannot be trusted.
        'changed_total': lambda page: payload(
            [row(f"changed-{page}-{index}") for index in range(20 if page else 10)],
            total=25 if page else 30),
    }

    def run(name):
        def table(sequence, page):
            return builders[name](page) if sequence['category'] == 'pharmacy' \
                else page_of_one(sequence, page)
        planner = build(QueryDomain.circle(1300), categories=('pharmacy', 'primary_school'))
        return planner, asyncio.run(planner.run(Synthetic(default=table)))

    for name in builders:
        planner, result = run(name)
        for item in result.coverage:
            if item['category'] == 'pharmacy' and '.' not in item['tileId']:
                assert item['status'] == 'partial', name
                assert item['stopReason'] == 'pagination_uncertain', name
                assert 'pagination_uncertain' in item['warnings'], name
        assert result.status != 'completed', name
        assert {block.tile_id for block in result.blocks} > {'r0c0', 'r0c1', 'r1c0', 'r1c1'}, name


def test_invalid_configuration_is_refused():
    with pytest.raises(ValueError, match='window outside supported projection'):
        build(QueryDomain.circle(20_000_000))
    with pytest.raises(ValueError, match='invalid_query_domain'):
        QueryDomain(((0, 0), (1, 1)))
    with pytest.raises(ValueError, match='empty_query_domain'):
        build(QueryDomain(((0.0, 0.0), (0.0, 0.0), (0.0, 0.0))))
    with pytest.raises(ValueError, match='unknown_category'):
        # ``supermarket`` 现在在设施目录里（购物），换一个目录里真的没有的名字。
        build(QueryDomain.circle(1300), categories=('no_such_category',))
    with pytest.raises(ValueError, match='budget must be a positive integer'):
        build(QueryDomain.circle(1300), budget=0)
    with pytest.raises(ValueError, match='at least one category required'):
        build(QueryDomain.circle(1300), categories=())


def test_the_planner_takes_the_request_centre():
    request = PoiCollectRequest(coordinateSystem='bd09ll')
    planner = OnlinePlanner(domain=QueryDomain.circle(1300),
                            origin=(request.center.lng, request.center.lat),
                            categories=CATEGORIES, budget=60, source='synthetic')
    assert len(planner.coarse) == 4
    assert len(planner.sequences) == 4 * sum(len(RULES['queries'][c]) for c in CATEGORIES)


def test_a_throttled_page_is_retried_once_and_the_round_keeps_going():
    """一次并发超限不再终止整轮：重试成功之后，剩下的查询照常跑完。

    这是"十大类下百度并发限制导致拿不到结果"的直接修复：以前第一次 401 就让整轮归零，
    现在它只花掉一次有界重试，并且仍被记进它自己的分块记录里。
    """
    looked = []

    class ThrottleOnce:
        async def __call__(self, sequence, page):
            looked.append(sequence['sequenceId'])
            if len(looked) == 1:
                return None, 'rate_limit'
            return page_of_one(sequence, page), None

    planner = build(QueryDomain.circle(1300), budget=10, categories=('pharmacy',))
    result = asyncio.run(planner.run(ThrottleOnce()))
    assert result.stop_reason is None
    assert result.status == 'completed'
    assert 'rate_limit' not in result.warnings
    # 第一次失败留在它自己的页面记录里，没有被抹掉，也没有被读成"这里没有设施"。
    errors = [error for item in result.coverage for error in item['pageErrors']]
    assert [error['reason'] for error in errors] == ['rate_limit']
    assert errors[0]['pageNum'] == 0


def test_persistent_throttling_pauses_after_one_bounded_retry_and_keeps_progress():
    """持续限流：同一页重试一次就暂停本轮，不是每一类都先花一次请求再说。"""
    looked = []

    class AlwaysThrottled:
        async def __call__(self, sequence, page):
            looked.append(sequence['sequenceId'])
            return None, 'rate_limit'

    planner = build(QueryDomain.circle(1300), budget=60, categories=('pharmacy',))
    result = asyncio.run(planner.run(AlwaysThrottled()))
    assert len(looked) == 2 and looked[0] == looked[1]
    assert result.attempts == 2
    assert result.stop_reason == 'rate_limit' and 'rate_limit' in result.warnings
    assert result.status == 'failed'
    assert {item['stopReason'] for item in result.coverage} == {'rate_limit'}


# -- §五 两把尺子：本地处理上限与新增网络预算分开 ---------------------------
#
# 这一节的每一项都对着同一类错误：把"页面调度了多少次"当成"发了多少次新请求"。
# 缓存重放不花额度，所以它不该能停住一次还在找缺页的检索；而额度用完也不该让
# 已经拿到手的缓存证据跟着一起消失。

def limited(domain, *, categories=('pharmacy',), steps=PROCESSING_STEP_LIMIT, budget=60,
            **kwargs):
    """一次按 v2 口径跑的检索：网络额度由服务池管，本地处理另有上限。"""
    return OnlinePlanner(domain=domain, origin=ORIGIN, categories=categories, budget=budget,
                         source='synthetic', limits=RunLimits(processing_steps=steps,
                                                              drain_after_budget_refusal=True),
                         **kwargs)


def pharmacy_pages(categories=('pharmacy',)):
    """这些类别跑满首轮两页所需的页面处理次数。"""
    return 2 * 4 * sum(len(RULES['queries'][category]) for category in categories)


def test_a_cache_hit_is_not_a_new_network_call():
    """缓存命中的页面不耗尽新增调用预算：本次要发的是缺页那几次。"""
    order = []

    async def fetch(sequence, page):
        order.append((sequence['sequenceId'], page))
        # 前四条由缓存回答，其余才真的派发；两者返回同样的页，只有记账不同。
        manner = CACHED if len(order) <= 4 else LIVE
        return PageResponse(two_pages(sequence, page), None, manner)

    planner = limited(QueryDomain.circle(1300), budget=0)
    result = asyncio.run(planner.run(fetch))
    assert result.status == 'completed'
    assert result.attempts == pharmacy_pages()
    assert result.network_calls == result.attempts - 4
    # 页数（处理）与调用数（新增）是两个数，不是一个数的两种说法。
    assert result.budget == 0


def test_the_local_processing_ceiling_has_its_own_stop_reason():
    """本地处理上限停下来时，说的不是"网络额度用尽"。"""
    planner = limited(QueryDomain.circle(1300), steps=3)
    result = asyncio.run(planner.run(Synthetic(default=two_pages)))
    assert result.attempts == 3 and result.network_calls == 3
    assert result.stop_reason == 'processing_limit_reached'
    assert 'processing_limit_reached' in result.warnings
    assert result.status != 'completed'


def test_a_spent_allowance_stops_the_pages_that_need_it_not_the_run():
    """额度用完：缺页不联网，但已经能读到的缓存证据一条都不丢。"""
    cached, refused = [], []

    async def fetch(sequence, page):
        if page == 0:
            cached.append(sequence['sequenceId'])
            return PageResponse(two_pages(sequence, page), None, CACHED)
        refused.append(sequence['sequenceId'])
        raise BudgetExhausted('poi', 2)

    planner = limited(QueryDomain.circle(1300))
    result = asyncio.run(planner.run(fetch))
    assert result.network_calls == 0
    # 第一页的每一条都留下了：这正是"一个缺页不能让其余证据消失"。
    assert len(result.observations) == 20 * len(cached)
    assert result.status == 'partial'
    assert result.stop_reason == 'network_budget_exhausted'
    # 每一页自己停下来的原因仍然是池子的那一句，不是被概括掉的"额度用尽"。
    assert {entry['stopReason'] for entry in result.coverage} == {'task_budget_exhausted'}
    assert result.stop_reason in result.warnings
    assert 'task_budget_exhausted' in result.warnings


def test_a_cache_only_run_still_checks_cancellation_and_the_deadline():
    """零网络调用的纯缓存流程也要有界：它绕过了配额池，就不能指望池子来停它。"""
    looked = Synthetic()
    token = CancelToken()
    token.cancel()
    cancelled = asyncio.run(limited(QueryDomain.circle(1300), token=token).run(looked))
    assert cancelled.stop_reason == 'cancelled' and cancelled.attempts == 0

    expired = asyncio.run(limited(QueryDomain.circle(1300),
                                  deadline=time.monotonic() - 1).run(looked))
    assert expired.stop_reason == 'deadline_reached' and expired.attempts == 0
    assert looked.calls == []


def test_a_pure_cache_run_observes_a_cancellation_that_already_arrived():
    """§五.9：纯缓存路径既不进池子也不等任何东西 —— 每个调度步都得让出一次。

    取消信号已经排进事件循环，但这轮页面全在缓存里、全程没有 await 点：不让出的话，
    它会把手上的页面全部处理完再"看到"取消，停在一个已经太晚的地方。
    """
    cache = KeyedCache()
    planner = limited(QueryDomain.circle(1300), steps=64)
    adapter = CachedPages(cache, Synthetic(), provider=Transport(), task_id='t')
    for state in planner.first_round:
        cache.store(adapter.key(state.mapping, 0), page_of_one(state.mapping, 0), task_id='t')

    async def cancel_while_running():
        asyncio.get_running_loop().call_soon(planner.token.cancel)
        return await planner.run(adapter)

    result = asyncio.run(cancel_while_running())
    assert result.stop_reason == 'cancelled'
    assert result.attempts == 0            # 一个页面都不该再处理
    assert result.network_calls == 0
    assert result.observations == []


def test_a_dispatched_but_failed_call_is_still_a_new_network_call():
    """超时是发出去之后才发生的：它同样花了额度，不能被记成"没发出去"。"""
    async def timeout(sequence, page):
        raise httpx.TimeoutException('the connection stalled')

    planner = limited(QueryDomain.circle(1300), steps=64)
    result = asyncio.run(planner.run(timeout))
    assert result.attempts == result.network_calls == 2 * 4 * len(RULES['queries']['pharmacy'])
    assert result.status == 'failed'
    assert {record['reason'] for entry in result.coverage for record in entry['pageRecords']} \
        == {'timeout'}


def test_a_refusal_before_dispatch_costs_nothing():
    """配额入口在派发前拒绝：那一页既不是新调用，也不该被算成花掉的额度。"""
    async def refuse(sequence, page):
        raise BudgetExhausted('poi', 0)

    planner = limited(QueryDomain.circle(1300), steps=64)
    result = asyncio.run(planner.run(refuse))
    assert result.attempts > 0
    assert result.network_calls == 0
    assert result.stop_reason == 'network_budget_exhausted'
