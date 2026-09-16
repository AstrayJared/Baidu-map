import asyncio
from dataclasses import replace

import pytest
from shapely.geometry import Point

from life_circle.coordinates import normalize
from life_circle.models import CancelToken, IsochroneRequest, RouteObservation
from tools.diagnostic_common import no_network
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.endpoint_radial_experiment import ORIGIN, P, Synthetic


def test_poi_hook_runs_after_basic_directions_even_when_circle_budget_is_spent():
    async def run():
        seen = []
        async def hook(session, rows):
            seen.append((len(rows), session.scheduler.stats.requests))
        p = Synthetic('circle')
        with no_network():
            await compute_multicross_boundary(IsochroneRequest(ORIGIN, 'bd09ll', budget=20,
                extent=1200, max_extent=1200, expand=False), p, CancelToken(), on_local_start=hook)
        assert seen == [(16, 20)]
    asyncio.run(run())


def test_cancelled_task_does_not_run_poi_hook():
    async def run():
        async def hook(*args):
            pytest.fail('Cancelled task must not start POI queries')
        token = CancelToken(); token.cancel()
        await compute_multicross_boundary(IsochroneRequest(ORIGIN, 'bd09ll'), Synthetic('circle'),
                                         token, on_local_start=hook)
    asyncio.run(run())


def test_evidence_keys_separate_uid_metric_origin_time_and_provider():
    from tools.endpoint_e83_poi import EvidenceContext, evidence_key
    base = EvidenceContext(ORIGIN, ('synthetic',), 'run-1')
    destination = normalize(P.to_geographic((300, 0)))
    key = evidence_key(base, destination)
    assert key != evidence_key(base, destination, uid='poi-uid')
    for altered in (replace(base, route_metric='distance'), replace(base, origin=(121.5, 31.3)),
                    replace(base, run_id='run-2'), replace(base, provider=('another',))):
        assert key != evidence_key(altered, destination)


def test_endpoint_mapping_and_failure_do_not_fabricate_minutes_or_unreachability():
    from tools.endpoint_e83_poi import confirmed_poi
    target = normalize(P.to_geographic((300, 0)))
    good = RouteObservation(target, 500, attempts=1, request_origin=ORIGIN,
                            route_origin=ORIGIN, route_destination=target)
    assert confirmed_poi(good, ORIGIN, target)['seconds'] == 500
    for bad in (replace(good, route_origin=(121.5, 31.3)),
                replace(good, route_destination=P.to_geographic((320, 0))),
                RouteObservation(target, reason='timeout', attempts=1)):
        result = confirmed_poi(bad, ORIGIN, target)
        assert result['status'] == 'pending' and result['seconds'] is None


def test_inside_and_outside_candidates_both_get_verified_and_shared_request_has_two_uses():
    from tools.endpoint_e83_poi import run_task
    async def run():
        # First point deliberately overlaps the deterministic 300 m east seed.
        pois = [dict(id='inside', coordinate=normalize(P.to_geographic((300, 0))), critical=True),
                dict(id='outside', coordinate=normalize(P.to_geographic((900, 0))), critical=True)]
        with no_network():
            result = await run_task('circle', Synthetic('circle'), pois, ['inside', 'outside'],
                                    circle_budget=200, poi_budget=2, shared=True)
        assert result['poi']['inside']['status'] == 'verified_reachable'
        assert result['poi']['outside']['status'] == 'verified_unreachable'
        assert result['cost']['poi_calls'] == 1
        assert result['cost']['total_calls'] == result['cost']['circle_calls'] + 1
        assert result['sharing']['circle_to_poi'] == 1
        assert any(set(e['uses']) == {'circle', 'poi'} for e in result['ledger'])
    asyncio.run(run())


def test_imported_poi_is_evidence_not_a_claim_of_bracket_progress():
    from tools.endpoint_e83_poi import run_task
    async def run():
        pois = [dict(id='p', coordinate=normalize(P.to_geographic((217, 83))), critical=True)]
        with no_network():
            result = await run_task('circle', Synthetic('circle'), pois, ['p'],
                                    circle_budget=150, poi_budget=1, shared=True)
        assert result['sharing']['poi_imported'] == 1
        assert result['sharing']['claimed_bracket_shrink_from_import'] == 0
        assert any(e['kind'] == 'poi_shared' for e in result['circle']['observationEvidence'])
    asyncio.run(run())


def test_retry_attempts_and_budget_exhaustion_remain_pending():
    from tools.endpoint_e83_poi import run_task
    class Failure(Synthetic):
        async def query_walking_time(self, origin, destination, deadline):
            self.calls += 1
            return RouteObservation(destination, reason='timeout')
    async def run():
        pois = [dict(id='p', coordinate=normalize(P.to_geographic((200, 0))), critical=True)]
        with no_network():
            result = await run_task('failure', Failure('circle'), pois, ['p', 'p'],
                                    circle_budget=0, poi_budget=2, shared=False)
        assert result['cost']['poi_calls'] == 2
        assert result['poi']['p']['status'] == 'pending'
        assert result['poi']['p']['seconds'] is None
        assert result['cost']['retries'] == 1
    asyncio.run(run())


def test_direct_repeated_categories_are_cached_fairly_without_a_circle():
    from tools.endpoint_e83_poi import run_task
    async def run():
        pois = [dict(id='p', coordinate=normalize(P.to_geographic((200, 0))), critical=True)]
        with no_network():
            result = await run_task('circle', Synthetic('circle'), pois, ['p', 'p', 'p'],
                                    circle_budget=0, poi_budget=3, shared=False)
        assert result['cost']['total_calls'] == 1
        assert result['circle'] is None
        assert result['poi']['p']['status'] == 'verified_reachable'
    asyncio.run(run())


def test_late_negative_poi_retracts_shared_circle_without_extra_provider_queries():
    from tools.endpoint_e83_poi import run_task
    target = normalize(P.to_geographic((217, 83)))
    class InteriorNegative(Synthetic):
        async def query_walking_time(self, origin, destination, deadline):
            obs = await super().query_walking_time(origin, destination, deadline)
            return replace(obs, duration=1900, observed_duration=1900) if destination == target else obs
    async def run():
        pois = [dict(id='late', coordinate=target, critical=False)]
        with no_network():
            result = await run_task('interior_negative', InteriorNegative('circle'), pois, ['late'],
                                    circle_budget=150, poi_budget=1, shared=True)
        from shapely.geometry import shape
        assert result['poi']['late']['status'] == 'verified_unreachable'
        assert not shape(result['circle']['geometry']).covers(Point(target))
        assert result['sharing']['late_poi_imported'] == 1
        assert result['cost']['poi_calls'] == 1 and result['cost']['circle_calls'] <= 150
    asyncio.run(run())


@pytest.mark.parametrize('mismatch', ['uid', 'metric', 'actual_origin'])
def test_incompatible_import_does_not_prime_circle_cache(mismatch):
    from shapely.geometry import box
    from life_circle.scheduler import Scheduler
    from tools.endpoint_boundary import BoundarySession
    from tools.endpoint_e83_poi import EvidenceContext, import_poi
    p = Synthetic('circle'); context = EvidenceContext(ORIGIN, p.identity, 'test')
    scheduler = Scheduler(IsochroneRequest(ORIGIN, 'bd09ll'), p, CancelToken())
    session = BoundarySession(scheduler, box(-1200, -1200, 1200, 1200))
    target = normalize(P.to_geographic((217, 83)))
    obs = RouteObservation(target, 500, attempts=1, request_origin=ORIGIN,
                           route_origin=ORIGIN, route_destination=target)
    entry = dict(context=context, uid=None, observation=obs)
    if mismatch == 'uid':
        entry['uid'] = 'different-destination-contract'
    elif mismatch == 'metric':
        entry['context'] = replace(context, route_metric='distance')
    else:
        entry['observation'] = replace(obs, route_origin=(121.5, 31.3))
    assert not import_poi(session, entry, context)
    assert target not in session._requests and target not in scheduler.cache
    assert not session.records
    scheduler.close()


def test_pending_true_poi_counts_as_missed_in_quality_metrics():
    from tools.endpoint_e83_poi_experiment import group_scores
    score = group_scores(['p'], {'p': {'status': 'pending', 'seconds': None}},
                         {'p': {'reachable': True, 'seconds': 500}})
    assert score['missed_reachable'] == score['pending'] == 1
    assert score['missed_rate'] == 1 and score['false_accept_rate'] is None
