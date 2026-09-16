import asyncio

import pytest
from shapely.geometry import Point, shape

from life_circle.coordinates import normalize
from tools.diagnostic_common import no_network
from tools.endpoint_e83_poi import run_task
from tools.endpoint_radial_experiment import P, Synthetic


def test_ordinary_poi_is_imported_before_local_queries_without_more_poi_calls():
    async def run():
        pois = [dict(id='ordinary', coordinate=normalize(P.to_geographic((217, 83))), critical=False)]
        with no_network():
            result = await run_task('circle', Synthetic('circle'), pois, ['ordinary'],
                circle_budget=250, poi_budget=1, shared=True, poi_policy='early')
        assert result['sharing']['poi_imported'] == 1
        assert result['sharing']['late_poi_imported'] == 0
        assert result['cost']['poi_calls'] == 1
        assert result['poi']['ordinary']['status'] == 'verified_reachable'
    asyncio.run(run())


@pytest.mark.parametrize('policy', ['guided', 'discover'])
def test_reachable_poi_outside_radial_circle_triggers_local_search(policy):
    async def run():
        import math
        point = (850*math.cos(.17), 850*math.sin(.17))
        pois = [dict(id='tip', coordinate=normalize(P.to_geographic(point)), critical=False)]
        with no_network():
            result = await run_task('narrow', Synthetic('narrow'), pois, ['tip'],
                circle_budget=250, poi_budget=1, shared=True, poi_policy=policy)
        guide = result['circle']['localRepair']['poiGuidance']
        assert guide['positive_outside'] == 1
        assert result['circle']['localRepair']['calls'] > 0
        assert result['cost']['circle_calls'] <= 250 and result['cost']['poi_calls'] == 1
        assert shape(result['circle']['geometry']).is_valid
        assert not any(shape(result['circle']['geometry']).covers(Point(e['route_destination']))
            for e in result['circle']['observationEvidence'] if e['accepted'] and e['observed_duration'] > 900)
    asyncio.run(run())


def test_discovery_preserves_old_geometry_when_no_far_reachable_poi_exists():
    async def run():
        pois = [dict(id='ordinary', coordinate=normalize(P.to_geographic((217, 83))), critical=False),
                dict(id='outside', coordinate=normalize(P.to_geographic((900, 0))), critical=False)]
        results = []
        for policy in ('critical', 'discover'):
            with no_network():
                results.append(await run_task('circle', Synthetic('circle'), pois, ['ordinary', 'outside'],
                    circle_budget=250, poi_budget=2, shared=True, poi_policy=policy))
        assert results[0]['circle']['geometry'] == results[1]['circle']['geometry']
        assert results[0]['cost'] == results[1]['cost']
    asyncio.run(run())


def test_guided_search_does_not_guess_snapped_poi_entrance():
    async def run():
        pois = [dict(id='offset', coordinate=normalize(P.to_geographic((217, 83))), critical=True)]
        with no_network():
            result = await run_task('snap80', Synthetic('snap80'), pois, ['offset'],
                circle_budget=250, poi_budget=1, shared=True, poi_policy='guided')
        assert result['poi']['offset']['status'] == 'pending'
        assert result['poi']['offset']['seconds'] is None
    asyncio.run(run())


@pytest.mark.parametrize('shared,policy', [(False, 'early'), (True, 'unknown')])
def test_bad_guidance_configuration_fails_before_queries(shared, policy):
    async def run():
        p = Synthetic('circle')
        with pytest.raises(ValueError):
            await run_task('circle', p, [], [], circle_budget=250, poi_budget=0,
                           shared=shared, poi_policy=policy)
        assert p.calls == 0
    asyncio.run(run())
