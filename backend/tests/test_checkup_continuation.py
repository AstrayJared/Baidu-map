"""Manual rounds: durable progress, immutable reports and lifetime route budget."""
import asyncio
import json
import time

import pytest
from fastapi.testclient import TestClient

from app import catalog
from app.checkups.store import CheckupStore, RequestIdConflict
from app.checkups.rounds import RoundConflict
from app.checkups.continuation import merge_saved_facilities
from app.checkups.facilities import FacilityOutcome
from app.checkups.models import FacilityGroup
from app.poi.online import OnlinePlanner, QueryDomain
from test_poi_online import Synthetic, two_pages, page_of_one, ORIGIN as PLANNER_ORIGIN
from test_checkup_facilities import body as three_category_body, document, run, terminal, ORIGIN
from test_checkup_report import make_report_app
from test_checkup_facilities import make_app, SyntheticPlaces, at_origin
from app.engines import IsochroneSnapshot
from life_circle.coordinates import LocalProjection


@pytest.fixture
def compact_ten_category_plan(monkeypatch, request):
    """Ten majors; the cross-round fixture has 160 pages, other fixtures have 80.

    Keep the integration fixture bounded; the planner tests exercise the full
    catalog, pagination, subdivision and strict 1200-call exhaustion separately.
    """
    from app.poi.planner import RULES
    original = catalog.poi_keys
    minors = tuple(original([major])[0] for major in catalog.majors())
    monkeypatch.setattr(catalog, 'poi_keys', lambda majors=None: tuple(
        key for key in minors if majors is None or catalog.major_of(key) in majors))
    for key in minors:
        first = RULES['queries'][key][0]
        words = [first, first + '服务']
        if request.node.name == 'test_ten_categories_complete_across_manual_rounds_without_rebuilding_boundary':
            words += [first + '机构', first + '中心']
        monkeypatch.setitem(RULES['queries'], key, words)


def body(**overrides):
    overrides.setdefault('facilities', {})
    overrides['facilities'].setdefault('categories', list(catalog.majors()))
    return three_category_body(**overrides)


def planner(budget, checkpoint=None, **kwargs):
    return OnlinePlanner(domain=QueryDomain.circle(1300), origin=PLANNER_ORIGIN,
                         categories=['pharmacy', 'market', 'primary_school', 'bank'], budget=budget, source='synthetic',
                         checkpoint=checkpoint, **kwargs)


@pytest.mark.parametrize('size', [1, 7, 60])
def test_rounds_match_uninterrupted_schedule_and_never_repeat_completed_pages(size):
    reference = Synthetic(two_pages)
    expected = asyncio.run(planner(10000).run(reference))
    fetch, checkpoint = Synthetic(two_pages), None
    for _ in range(1000):
        current = planner(size, checkpoint)
        result = asyncio.run(current.run(fetch))
        before = current.checkpoint()
        current.result()
        assert current.checkpoint() == before  # report projection must be read-only
        checkpoint = json.loads(json.dumps(before))
        assert result.attempts <= size
        if result.status == 'completed':
            break
    assert result.status == 'completed'
    assert fetch.calls == reference.calls
    assert len(fetch.calls) == len(set(fetch.calls))
    assert result.observations == expected.observations
    assert result.incomplete == expected.incomplete


def test_cache_hits_do_not_take_network_allowance():
    paid = 0
    async def fetch(sequence, page):
        nonlocal paid
        if paid == 0 and sequence['category'] == 'bank':
            paid += 1
        return page_of_one(sequence, page), None
    current = planner(1, spent=lambda: paid)
    result = asyncio.run(current.run(fetch))
    assert result.attempts > 1 and paid == 1
    assert result.stop_reason == 'budget_exhausted'


def test_legacy_observations_survive_failed_rebuild_without_inventing_page_progress():
    prior = FacilityGroup(query_status='partial', provider='test', api_version='3',
        data_source='synthetic', query_domain={}, data_obtained_at=10,
        counts_by_category={'medical': 1}, facilities=[{'id': 'old', 'category': 'pharmacy'}])
    fresh = prior.model_copy(update={'facilities': [], 'counts_by_category': {'medical': 0},
        'query_status': 'failed', 'data_obtained_at': None,
        'statistics': {'queryCompleteByMajor': {'medical': False}},
        'query_incomplete_regions': {'pharmacy': [{'type': 'Polygon', 'coordinates': []}]}})
    result = merge_saved_facilities(FacilityOutcome(group=fresh, status='failed'), prior)
    assert result.group.facilities == prior.facilities
    assert result.group.counts_by_category == {'medical': 1}
    assert result.group.data_obtained_at == 10
    assert result.group.statistics['queryCompleteByMajor'] == {'medical': False}
    assert result.group.query_coverage == []
    assert result.group.query_incomplete_regions == fresh.query_incomplete_regions


def test_round_admission_is_atomic_and_reservations_survive_restart(tmp_path):
    store = CheckupStore(tmp_path)
    store.initialize()
    store.create(task_id='a', client_request_id='initial', engine='test', fingerprint='x', payload={}, budget=200)
    store.begin_round('a', 'initial', 0, 'identity', initial=True)
    reservation = store.reserve_request('a', 1, 'poi', {'key': 'page0'})
    store.save_checkpoint('a', {'progress': 'pending response'})
    store.update('a', status='running')
    restarted = CheckupStore(tmp_path)
    assert restarted.initialize() == 1
    assert restarted.round_spend('a', 1) == {'poi': 1}
    assert restarted.get('a').network_requests == 1
    assert restarted.checkpoint('a') == {'progress': 'pending response'}
    assert restarted.saved_pages('a') == []
    restarted.complete_request(reservation, {'results': [], 'total': 0}, None)
    assert restarted.saved_pages('a')[0]['key'] == 'page0'
    assert restarted.begin_round('a', 'continue1', 0, 'identity', poi_before=1)
    assert not restarted.begin_round('a', 'continue1', 0, 'identity')
    with pytest.raises(RoundConflict):
        restarted.begin_round('a', 'duplicate-click', 0, 'identity')
    with pytest.raises(RoundConflict):
        restarted.begin_round('a', 'continue1', 9, 'identity')
    with pytest.raises(RequestIdConflict):
        restarted.create(task_id='b', client_request_id='continue1', engine='test',
                         fingerprint='x', payload={}, budget=200)


@pytest.mark.usefixtures("compact_ten_category_plan")
def test_ten_categories_complete_across_manual_rounds_without_rebuilding_boundary(tmp_path):
    app = make_report_app(tmp_path, [ORIGIN])
    manager = app.state.checkups
    engine = manager.registry.get('baidu_e82')
    boundary_calls = []
    async def counted(*args, **kwargs):
        boundary_calls.append(1)
        return small_boundary()
    engine.compute = counted
    with TestClient(app) as client:
        task_id, view = run(client, body(facilities={"maxPoiRequests": 60}))
        assert view['status'] == 'completed', view
        first = document(client, task_id)
        first_bytes = json.dumps(first, sort_keys=True)
        assert first['facilities']['queryStatus'] == 'partial'
        assert view['completion']['roundPoiRequests'] == 60
        assert len(view['completion']['queryCompleteByMajor']) == 10
        assert not all(view['completion']['queryCompleteByMajor'].values())
        # Unfinished retrieval cannot produce invented grey zones.
        assert first['serviceGaps']['gapAreaM2'] == 0

        assert view['completion']['canContinue']
        history = [view['completion']['cumulativePoiRequests']]
        for number in range(2, 16):
            base = view['revision']
            request = {'clientRequestId': f'round-{number}', 'baseRevision': base}
            response = client.post(f'/api/v2/checkups/{task_id}/continue', json=request)
            assert response.status_code == 202, response.text
            repeated = client.post(f'/api/v2/checkups/{task_id}/continue', json=request)
            assert repeated.status_code == 202
            conflict = client.post(f'/api/v2/checkups/{task_id}/continue', json={**request, 'clientRequestId': 'other'})
            assert conflict.status_code == 409
            view = terminal(client, task_id)
            assert view['status'] == 'completed', view
            value = view['completion']
            assert value['roundNumber'] == number
            assert value['roundPoiLimit'] == 1200
            assert 60 < value['roundPoiRequests'] <= 1200
            assert value['routeRequests'] <= 120
            assert value['routeRequests'] + value['routeRemaining'] == 120
            assert value['cumulativePoiRequests'] >= history[-1]
            history.append(value['cumulativePoiRequests'])
            if not value['canContinue']:
                break
        final = document(client, task_id)
        assert all(value['queryCompleteByMajor'].values())
        assert final['scores']['overall']['available']
        assert value['evaluatedCategories'] == 10
        assert value['evaluationStatus'] == 'complete'
        assert len(boundary_calls) == 1
        assert final['isochrone'] == first['isochrone']
        assert json.dumps(client.get(f'/api/v2/checkups/{task_id}/result?revision=5').json(), sort_keys=True) == first_bytes
        assert final['trace']['budgets']['poi']['spent'] == history[-1]
        assert final['trace']['budgets']['route']['spent'] == value['routeRequests']
        assert client.post(f'/api/v2/checkups/{task_id}/continue',
                           json={'clientRequestId': 'done', 'baseRevision': view['revision']}).status_code == 409


def small_boundary():
    project = LocalProjection(ORIGIN)
    ring = [list(project.to_geographic(p)) for p in
            [(-200, -200), (200, -200), (200, 200), (-200, 200), (-200, -200)]]
    geometry = {'type': 'Polygon', 'coordinates': [ring]}
    return IsochroneSnapshot.build(engine_id='baidu_e82', engine_version='test', algorithm='test',
        parameters={}, geometry=geometry, display_geometry=geometry, unknown_region=None,
        uncertain_region=None, computation_extent=geometry, quality='usable', stop_reason='completed',
        warnings=[], statistics={})


def fast_app(tmp_path, places):
    app = make_app(tmp_path, places)
    app.state.checkups.offline = None
    engine = app.state.checkups.registry.get('baidu_e82')
    calls = []
    async def compute(*args, **kwargs):
        calls.append(1)
        return small_boundary()
    engine.compute = compute
    return app, calls


@pytest.mark.usefixtures("compact_ten_category_plan")
def test_legacy_report_restarts_only_retrieval_and_preserves_historical_costs(tmp_path):
    places = SyntheticPlaces(at_origin())
    app, calls = fast_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, view = run(client, body(facilities={'maxPoiRequests': 1, 'maxRouteRequests': 1}))
        first = document(client, task_id)
        assert view['completion']['routeRequests'] == 1
        # Emulate an old deployment: reports/counters remain, no checkpoint journal.
        with app.state.checkups.store._connection() as db:
            for table in ('checkup_rounds', 'checkup_requests', 'checkup_checkpoints'):
                db.execute(f'DELETE FROM {table} WHERE task_id=?', (task_id,))
        status = client.get(f'/api/v2/checkups/{task_id}').json()
        assert status['completion']['restartRetrieval']
        response = client.post(f'/api/v2/checkups/{task_id}/continue',
                               json={'clientRequestId': 'legacy', 'baseRevision': view['revision']})
        assert response.status_code == 202, response.text
        final = terminal(client, task_id)
        assert final['status'] == 'completed', final
        assert final['completion']['roundPoiLimit'] == 1200
        assert final['completion']['cumulativePoiRequests'] == 1 + final['completion']['roundPoiRequests']
        assert final['completion']['roundPoiRequests'] > 60
        assert final['completion']['routeRequests'] == 1
        assert final['completion']['routeRemaining'] == 0
        assert len(calls) == 1
        updated = document(client, task_id)
        assert updated['isochrone'] == first['isochrone']
        old_ids = {row['facilityId'] for row in first['verification']['facilities']}
        assert old_ids <= {row['facilityId'] for row in updated['verification']['facilities']}
        app.state.checkups.settings.osm_data_version = 'changed'
        rejected = client.post(f'/api/v2/checkups/{task_id}/continue',
                               json={'clientRequestId': 'incompatible', 'baseRevision': final['revision']})
        assert rejected.status_code == 409
        assert rejected.json()['code'] == 'checkup_cannot_continue'


@pytest.mark.usefixtures("compact_ten_category_plan")
def test_cancel_then_restart_keeps_checkpoint_and_never_auto_resumes(tmp_path):
    places = SyntheticPlaces(at_origin(), delay=.02)
    app, calls = fast_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, first = run(client, body(facilities={'maxPoiRequests': 1, 'maxRouteRequests': 1}))
        response = client.post(f'/api/v2/checkups/{task_id}/continue',
                               json={'clientRequestId': 'interrupt', 'baseRevision': first['revision']})
        assert response.status_code == 202
        for _ in range(100):
            status = client.get(f'/api/v2/checkups/{task_id}').json()
            if status['completion']['roundPoiRequests'] > 1:
                break
            time.sleep(.01)
        assert client.post(f'/api/v2/checkups/{task_id}/cancel').status_code == 202
        stopped = terminal(client, task_id)
        assert stopped['status'] == 'cancelled'
        checkpoint = app.state.checkups.store.checkpoint(task_id)
        count = len(places.sent)
    app, new_calls = fast_app(tmp_path, places)
    with TestClient(app) as client:
        restored = client.get(f'/api/v2/checkups/{task_id}').json()
        assert restored['completion']['canContinue']
        assert restored['completion']['reportRevision'] == 5
        assert len(places.sent) == count and new_calls == []
        assert app.state.checkups.store.checkpoint(task_id) == checkpoint
        response = client.post(f'/api/v2/checkups/{task_id}/continue',
                               json={'clientRequestId': 'resume-after-restart', 'baseRevision': restored['revision']})
        assert response.status_code == 202, response.text
        final = terminal(client, task_id)
        assert final['status'] == 'completed', final
        assert new_calls == []
        assert len(places.sent) == len(set(places.sent))
        assert final['completion']['cumulativePoiRequests'] == len(places.sent)


@pytest.mark.usefixtures("compact_ten_category_plan")
def test_default_budget_completes_ten_categories_in_one_round(tmp_path):
    app = make_report_app(tmp_path, [ORIGIN])
    app.state.checkups.registry.get('baidu_e82').compute = lambda *args, **kwargs: _small_boundary_async()
    with TestClient(app) as client:
        task_id, view = run(client, body())
        value = view['completion']
        assert view['status'] == 'completed', view
        assert value['roundPoiLimit'] == 1200
        assert 60 < value['roundPoiRequests'] < 1200
        assert value['evaluationStatus'] == 'complete'
        assert value['evaluatedCategories'] == 10
        assert all(value['queryCompleteByMajor'].values())
        result = document(client, task_id)
        assert result['scores']['overall']['available']
        assert result['completion']['roundPoiLimit'] == 1200
        assert result['report']['completion']['roundPoiLimit'] == 1200


async def _small_boundary_async():
    return small_boundary()


def test_dense_queries_stop_at_1200_including_retries():
    attempts = 0
    async def fetch(sequence, page):
        nonlocal attempts
        attempts += 1
        if attempts % 3 == 1:
            return None, 'timeout'
        answer = two_pages(sequence, page)
        answer['total'] = 1000  # truncated blocks require subdivision
        return answer, None
    current = OnlinePlanner(domain=QueryDomain.circle(1300), origin=PLANNER_ORIGIN,
        categories=catalog.poi_keys(catalog.majors()), budget=1200, source='synthetic')
    result = asyncio.run(current.run(fetch))
    assert result.status == 'partial'
    assert result.stop_reason == 'budget_exhausted'
    assert result.attempts == attempts == 1200
    assert current.checkpoint()['queue'] or any(current.checkpoint()['later'].values())


def test_old_round_schema_migrates_actual_initial_and_fixed_continuation_limits(tmp_path):
    store = CheckupStore(tmp_path)
    store.initialize()
    store.create(task_id='old', client_request_id='old-request', engine='test', fingerprint='x',
                 payload={'facilities': {'max_poi_requests': 7}}, budget=200)
    with store._connection() as db:
        db.execute('DROP TABLE checkup_rounds')
        db.execute('CREATE TABLE checkup_rounds (task_id TEXT,number INTEGER,request_id TEXT,'
                   'base_revision INTEGER,identity TEXT,legacy INTEGER,poi_before INTEGER,'
                   'route_before INTEGER,network_before INTEGER,attempts_before INTEGER)')
        db.execute("INSERT INTO checkup_rounds VALUES ('old',1,'old-request',0,'x',0,0,0,0,0)")
        db.execute("INSERT INTO checkup_rounds VALUES ('old',2,'old-next',5,'x',0,7,1,8,8)")
    store.create_schema()
    store.create_schema()  # migration is idempotent
    with store._connection() as db:
        rows = db.execute('SELECT number,poi_limit FROM checkup_rounds ORDER BY number').fetchall()
    assert [tuple(row) for row in rows] == [(1, 7), (2, 60)]


def test_budget_contract_accepts_1200_and_keeps_historical_completion_default():
    from pydantic import ValidationError
    from app.checkups.models import CheckupFacilities, CheckupCompletion
    assert CheckupFacilities().max_poi_requests == 1200
    assert CheckupFacilities(max_poi_requests=1200).max_poi_requests == 1200
    with pytest.raises(ValidationError):
        CheckupFacilities(max_poi_requests=1201)
    assert CheckupCompletion().round_poi_limit == 60


@pytest.mark.usefixtures("compact_ten_category_plan")
def test_partial_report_rejects_changed_data_version(tmp_path):
    app, _ = fast_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        task_id, view = run(client, body(facilities={'maxPoiRequests': 1, 'maxRouteRequests': 1}))
        app.state.checkups.settings.osm_data_version = 'changed'
        response = client.post(f'/api/v2/checkups/{task_id}/continue',
            json={'clientRequestId': 'changed-data', 'baseRevision': view['revision']})
        assert response.status_code == 409
        assert response.json()['code'] == 'checkup_incompatible_continuation'


def test_retention_clears_durable_details_but_preserves_paid_reservations(tmp_path):
    store = CheckupStore(tmp_path)
    store.initialize()
    store.create(task_id='expired', client_request_id='first', engine='test', fingerprint='x',
                 payload={}, budget=200)
    store.begin_round('expired', 'first', 0, 'identity', initial=True, poi_limit=1200)
    reservation = store.reserve_request('expired', 1, 'poi', {'key': 'page', 'name': 'detail'})
    store.complete_request(reservation, {'results': [{'name': 'detail'}]}, None)
    store.save_checkpoint('expired', {'observations': [{'name': 'detail'}]})
    assert store.mark_details_cleared('expired', now=time.time())
    assert not store.mark_details_cleared('expired', now=time.time())
    assert store.checkpoint('expired') is None
    assert store.saved_pages('expired') == []
    assert store.round_spend('expired', 1) == {'poi': 1}
    store.close()


def test_stored_1600_request_remains_readable_without_widening_new_requests():
    from app.checkups.models import CheckupRequest
    from pydantic import ValidationError
    historical = body(facilities={'maxPoiRequests': 1600})
    with pytest.raises(ValidationError):
        CheckupRequest(**historical)
    assert CheckupRequest.from_stored(historical).facilities.max_poi_requests == 1600
    assert historical['facilities']['maxPoiRequests'] == 1600
