"""§4.1's query range and §4.3's place transport; no request leaves the process."""
import asyncio
import json
import math
from pathlib import Path

import httpx
import pytest
from shapely.geometry import MultiPolygon, Polygon, mapping

from app.checkups.facility_stage import query_domain
from app.checkups.places import MAX_TIMEOUT_SECONDS, POI_POOL, OnlinePlaceTransport
from app.config import Settings
from app.poi.online import OnlinePlanner, QueryBlock, QueryDomain
from app.poi.planner import RULES, sequence
from app.poi.provider import whitelist
from app.quota import DEFAULT_POI_ATTEMPTS, DailyBudgetExhausted, Quota
from life_circle.coordinates import LocalProjection

ORIGIN = (121.514, 31.313)
SECRET = 'synthetic-secret'
PROJECTION = LocalProjection(ORIGIN)
PAGE = {'status': 0, 'result_type': 'poi_type', 'total': 1, 'results': [
    {'uid': 'synthetic-1', 'name': '合成药店',
     'location': {'lng': ORIGIN[0], 'lat': ORIGIN[1]}, 'telephone': 'synthetic-phone',
     'detail_info': {'classified_poi_tag': '医疗;药店',
                     'detail_url': f'https://example.invalid/?ak={SECRET}'}}]}


def geographic(point):
    """The geographic point ``point`` metres east and north of the origin."""
    return PROJECTION.to_geographic(point)


def square(half):
    """A square boundary of half-width ``half`` metres, centred on the origin."""
    return mapping(Polygon([geographic((-half, -half)), geographic((half, -half)),
                            geographic((half, half)), geographic((-half, half))]))


def quota(tmp_path, **overrides):
    overrides = {"baidu_place_daily_budget": 1600, **overrides}
    overrides.setdefault("baidu_fallback_place_daily_budget",
                         overrides["baidu_place_daily_budget"])
    # 档位切换时间固定在远未来：这些用例说的是 current 档的日额度，测试自己不该
    # 因为跑在哪一天而改变生效档位（切换之后生效的是 fallback 档的那一个数字）。
    settings = Settings(_env_file=None, baidu_map_ak=SECRET, baidu_place_qps=10000,
                        baidu_quota_fallback_at='2099-01-01T00:00:00+08:00',
                        quota_ledger_path=Path(tmp_path) / 'quota.sqlite3', **overrides)
    return Quota(settings)


def places_for(respond):
    """A transport over a responding stub; ``send`` closes the client it owns."""
    client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
    return OnlinePlaceTransport(client, SECRET)


def block():
    return sequence('r0c0', -100, -100, 200, 'pharmacy', RULES['queries']['pharmacy'][0],
                    PROJECTION)


def send(session, *pages):
    """Drive the session over these pages; the client closes however it ends."""
    assert pages, 'a fetch sends the page it is asked for'
    return asyncio.run(_send(session, pages))


async def _send(session, pages):
    try:
        return [await session(block(), page) for page in pages]
    finally:
        await session.transport.client.aclose()


def session_for(quota_, respond, *, budget=None, deadline=math.inf):
    places = places_for(respond)
    return places.session(quota_.place,
                          budget=budget or quota_.task_budget(isochrone=1), deadline=deadline)


# -- §4.1 the query range --------------------------------------------------

def test_the_range_is_the_boundary_expanded_by_the_margin():
    domain, widened = query_domain(square(50), ORIGIN)
    assert widened is False
    assert tuple(round(v) for v in domain.envelope) == (-1350, -1350, 1350, 1350)
    # The boundary is inside the range it was expanded from, not merely on it.
    assert all(domain.contains(PROJECTION.to_local(point))
               for point in square(50)['coordinates'][0])
    assert not domain.contains((1500.0, 0.0))


def test_the_margin_is_what_reaches_the_blocks_the_boundary_needs():
    domain, _ = query_domain(square(700), ORIGIN)
    assert tuple(round(v) for v in domain.envelope) == (-2000, -2000, 2000, 2000)
    # A 700 m boundary reaches all four coarse blocks; without the margin it
    # would sit inside a single one of them.
    assert all(domain.intersects(QueryBlock(f'r{x}{y}', float(x), float(y), 2000))
               for x, y in ((-2000, -2000), (0, -2000), (-2000, 0), (0, 0)))


def test_a_hole_is_left_to_the_margin_and_reported_when_it_survives():
    def ring(half):
        return [geographic((-half, -half)), geographic((half, -half)),
                geographic((half, half)), geographic((-half, half))]
    # A 1 km hole in a 5 km boundary is narrower than the margin, so expanding
    # the region closes it: the range is §4.1's expansion exactly.
    closed, widened = query_domain(mapping(Polygon(ring(2500), [ring(500)])), ORIGIN)
    assert widened is False
    assert closed.contains((0.0, 0.0)) is True
    # A 3 km hole outlives the margin, 1300 m narrower on every side. The domain
    # carries one ring, so the search range is the padded outer ring and says so:
    # a facility is counted where the boundary puts it, not where the range does.
    domain, widened = query_domain(mapping(Polygon(ring(2500), [ring(1500)])), ORIGIN)
    assert widened is True
    assert tuple(round(v) for v in domain.envelope) == (-3800, -3800, 3800, 3800)
    assert domain.contains((0.0, 0.0)) is True


def test_parts_too_far_apart_widen_the_range_instead_of_dropping_one():
    boundary = MultiPolygon([Polygon([geographic((-3400, -100)), geographic((-3200, -100)),
                                      geographic((-3200, 100)), geographic((-3400, 100))]),
                             Polygon([geographic((3200, -100)), geographic((3400, -100)),
                                      geographic((3400, 100)), geographic((3200, 100))])])
    domain, widened = query_domain(mapping(boundary), ORIGIN)
    assert widened is True
    # Widening searches more than §4.1 asks for and never less: both parts stay
    # inside the range, which is the whole of what the envelope promises.
    for part in boundary.geoms:
        assert all(domain.contains(PROJECTION.to_local(point))
                   for point in part.exterior.coords)


def test_a_shape_that_cannot_be_read_is_refused_without_quoting_it():
    for geometry in (None, 'POLYGON((0 0, 1 1, 0 0))', {'type': 'Unknown'},
                     {'type': 'Polygon', 'coordinates': 'not coordinates'},
                     {'type': 'Polygon', 'coordinates': [[['a', 'b'], ['c', 'd'], ['a', 'b']]]}):
        with pytest.raises(ValueError, match='^invalid_boundary_geometry$'):
            query_domain(geometry, ORIGIN)


def test_a_shape_that_is_no_region_is_refused_as_empty():
    for geometry in ({'type': 'Point', 'coordinates': list(ORIGIN)},
                     {'type': 'LineString', 'coordinates': [[0, 0], [1, 1]]},
                     {'type': 'Polygon', 'coordinates': []},
                     {'type': 'Polygon', 'coordinates': [[[0, 0], [1, 1], [0, 0]]]},
                     {'type': 'GeometryCollection', 'geometries': []}):
        with pytest.raises(ValueError, match='^empty_boundary_geometry$'):
            query_domain(geometry, ORIGIN)


# -- §9.2 every attempt is reserved before it is sent ----------------------

def test_nothing_is_sent_when_the_pool_is_already_spent(tmp_path):
    def never(request):
        pytest.fail('a request was sent without a reservation')

    quota_ = quota(tmp_path)
    session = session_for(quota_, never, budget=quota_.task_budget(isochrone=1, poi=0))
    with pytest.raises(Exception) as refusal:
        send(session, 0)
    assert type(refusal.value).__name__ == 'BudgetExhausted'
    assert quota_.ledger.spent('place') == 0


def test_the_daily_allowance_is_checked_before_the_request(tmp_path):
    quota_ = quota(tmp_path, baidu_place_daily_budget=0)
    session = session_for(quota_, lambda request: pytest.fail('a request was sent'))
    with pytest.raises(DailyBudgetExhausted):
        send(session, 0)
    assert quota_.ledger.spent('place') == 0


def test_a_deadline_that_has_passed_is_checked_before_the_request(tmp_path):
    quota_ = quota(tmp_path)
    budget = quota_.task_budget(isochrone=1)
    session = session_for(quota_, lambda request: pytest.fail('a request was sent'),
                          budget=budget, deadline=0.0)
    with pytest.raises(Exception) as refusal:
        send(session, 0)
    assert type(refusal.value).__name__ == 'DeadlineReached'
    # The refusal is on the near side of the send and before the reservation: an
    # attempt that expired while waiting was never dispatched, so it costs neither
    # its bucket slot nor a daily unit. Anything that *was* dispatched stays
    # counted (see test_quota), so the ledger still never under-counts a send.
    assert budget.remaining(POI_POOL) == DEFAULT_POI_ATTEMPTS
    assert quota_.ledger.spent('place') == 0


def test_one_attempt_is_one_documented_request(tmp_path):
    sends = []

    def respond(request):
        sends.append(request)
        assert request.method == 'GET'
        params = request.url.params
        assert params['query'] == RULES['queries']['pharmacy'][0]
        assert params['coord_type'] == '3' and params['scope'] == '2'
        assert params['page_size'] == '20' and params['page_num'] == '0'
        assert params['radius_limit'] == 'true' and params['output'] == 'json'
        assert params['location'].endswith(f',{ORIGIN[0]:.6f}')
        assert request.extensions['timeout']['read'] == MAX_TIMEOUT_SECONDS
        return httpx.Response(200, json=PAGE)

    quota_ = quota(tmp_path)
    session = session_for(quota_, respond)
    payload, reason = send(session, 0)[0]
    assert (reason, payload['total']) == (None, 1)
    assert len(sends) == 1
    assert quota_.ledger.spent('place') == 1


def test_the_key_and_the_fields_outside_the_allowlist_never_cross(tmp_path):
    def respond(request):
        assert request.url.params['ak'] == SECRET
        return httpx.Response(200, json=PAGE)

    quota_ = quota(tmp_path)
    payload, _ = send(session_for(quota_, respond), 0)[0]
    assert payload == whitelist(PAGE, SECRET)
    text = json.dumps(payload, ensure_ascii=False)
    assert SECRET not in text and 'synthetic-phone' not in text
    assert 'example.invalid' not in text


@pytest.mark.parametrize('status,body,expected', [
    (429, None, 'rate_limit'),
    (403, None, 'permission'),
    (500, None, 'upstream_error'),
    (200, {'status': 4}, 'quota'),
    (200, {'status': 2}, 'parameter_error'),
    (200, {'status': 0, 'result_type': 'city'}, 'non_poi_response'),
    # The legacy reader's rule, kept: a body that does not name itself as a
    # point-of-interest listing is not read as one, even with a clean status.
    (200, {'status': 0, 'results': []}, 'invalid_response'),
    (200, {'status': 0, 'result_type': 'poi_type', 'results': [{}] * 21}, 'invalid_response'),
])
def test_a_refusal_is_reported_and_never_reads_as_a_result(tmp_path, status, body, expected):
    quota_ = quota(tmp_path)
    session = session_for(quota_, lambda request: httpx.Response(status, json=body))
    assert send(session, 0)[0] == (None, expected)
    # The attempt was sent, so it stays counted: a refusal is not a zero result.
    assert quota_.ledger.spent('place') == 1


def test_a_body_that_cannot_be_read_is_a_sent_attempt_with_an_unknown_result(tmp_path):
    quota_ = quota(tmp_path)
    session = session_for(quota_,
                          lambda request: httpx.Response(200, content=b'<html>not json</html>'))
    assert send(session, 0)[0] == (None, 'invalid_response')
    assert quota_.ledger.spent('place') == 1


def test_a_transport_error_propagates_and_leaves_the_attempt_counted(tmp_path):
    def explode(request):
        raise httpx.ConnectError('synthetic failure', request=request)

    quota_ = quota(tmp_path)
    session = session_for(quota_, explode)
    with pytest.raises(httpx.ConnectError):
        send(session, 0)
    assert quota_.ledger.spent('place') == 1
    # The pool released its in-flight slot, so the planner's retry may proceed.
    assert quota_.place.gate.attempt_lock.locked() is False


def test_two_pages_of_one_sequence_share_a_session_a_budget_and_a_slot(tmp_path):
    pages = []

    def respond(request):
        pages.append(request.url.params['page_num'])
        return httpx.Response(200, json=PAGE)

    quota_ = quota(tmp_path)
    budget = quota_.task_budget(isochrone=1, poi=2)
    session = session_for(quota_, respond, budget=budget)
    assert [reason for _, reason in send(session, 0, 1)] == [None, None]
    assert (pages, quota_.ledger.spent('place')) == (['0', '1'], 2)
    assert budget.remaining(POI_POOL) == 0
    with pytest.raises(Exception) as refusal:
        send(session, 2)
    assert type(refusal.value).__name__ == 'BudgetExhausted'
    assert pages == ['0', '1']


def test_a_second_task_is_a_second_budget_not_a_second_pool(tmp_path):
    def respond(request):
        return httpx.Response(200, json=PAGE)

    quota_ = quota(tmp_path)
    first = quota_.task_budget(isochrone=1, poi=1)
    assert send(session_for(quota_, respond, budget=first), 0)[0][1] is None
    with pytest.raises(Exception) as refusal:
        send(session_for(quota_, respond, budget=first), 0)
    assert type(refusal.value).__name__ == 'BudgetExhausted'
    # A new task gets a new bucket and may place its own attempt: the buckets are
    # per task, and the pool they both draw from is the same one service pool.
    second = quota_.task_budget(isochrone=1, poi=1)
    assert send(session_for(quota_, respond, budget=second), 0)[0][1] is None
    assert quota_.ledger.spent('place') == 2


def test_the_transport_carries_the_identity_the_cache_and_the_record_join_on():
    assert OnlinePlaceTransport.identity == 'baidu_place'
    assert OnlinePlaceTransport.api_version == '3.0'
    # A network transport, unlike the synthetic replay one.
    assert OnlinePlaceTransport.network is True
    assert POI_POOL == 'poi'


def test_a_pool_refusal_stops_the_planner_and_is_never_a_result(tmp_path):
    """§9.2: the planner turns the pool's refusal into a stop reason."""
    async def run():
        quota_ = quota(tmp_path)
        session = session_for(quota_, lambda request: pytest.fail('a request was sent'),
                              budget=quota_.task_budget(isochrone=1, poi=0))
        planner = OnlinePlanner(domain=QueryDomain.circle(120), origin=ORIGIN,
                                categories=['pharmacy'], budget=4, source='online')
        try:
            return await planner.run(session), quota_.ledger.spent('place')
        finally:
            await session.transport.client.aclose()

    result, spent = asyncio.run(run())
    assert result.status == 'failed' and result.stop_reason == 'task_budget_exhausted'
    assert result.stop_reason in result.warnings
    assert [record['reason'] for entry in result.coverage for record in entry['pageRecords']] \
        == ['task_budget_exhausted']
    assert all(entry['returned'] == 0 for entry in result.coverage)
    # The task's own pool refused it, so nothing was sent and nothing was spent.
    assert spent == 0
