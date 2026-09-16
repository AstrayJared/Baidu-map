import asyncio
from dataclasses import replace

from shapely.geometry import shape

from life_circle.models import CancelToken, IsochroneRequest
from test_endpoint_boundary import Provider, ORIGIN
from tools.endpoint_radial_boundary import compute_radial_boundary


def request(budget=400):
    return IsochroneRequest(ORIGIN, 'bd09ll', extent=1200, max_extent=1200,
                           expand=False, budget=budget, concurrency=1)


def test_circle_uses_no_interior_checks_and_localizes_all_fixed_directions():
    async def run():
        provider = Provider(lambda x,y: (x*x+y*y)**.5*1.5)
        r = await compute_radial_boundary(request(), provider, CancelToken(), directions=16)
        assert len(r['directions']) == 16
        assert all(d['status'] == 'localized' and d['bracket']['width_m'] <= 25 for d in r['directions'])
        assert provider.calls == r['calls'] < 200
        assert not any(e['kind'] == 'interior_check' for e in r['_evidence'])
        assert shape(r['geometry']).is_valid
    asyncio.run(run())


def test_jump_is_bisected_without_needing_time_close_to_900():
    async def run():
        r = await compute_radial_boundary(request(), Provider(lambda x,y:400 if x*x+y*y<360000 else 1900), CancelToken())
        assert all(d['status'] == 'localized' for d in r['directions'])
        assert all(abs(d['bracket']['left']['duration']-d['bracket']['right']['duration']) == 1500 for d in r['directions'])
    asyncio.run(run())


def test_internal_offset_does_not_trigger_compensation_until_boundary():
    async def run():
        r = await compute_radial_boundary(request(), Provider(lambda x,y:(x*x+y*y)**.5*1.5,shift=(60,0)), CancelToken())
        assert r['calls'] <= 400
        assert not any(e['kind'].startswith('interior') for e in r['_evidence'])
        assert all(d['bracket']['width_m'] <= 25 for d in r['directions'] if d['status'] == 'localized')
        assert any(e['destination_offset_m'] > 50 for e in r['_evidence'])
    asyncio.run(run())


def test_budget_and_cancel_do_not_publish_complete_polygon():
    async def run():
        p = Provider(); r = await compute_radial_boundary(request(4),p,CancelToken())
        assert r['calls'] == p.calls <= 4 and r['uncoveredAngleFraction'] > 0
        token=CancelToken();token.cancel();p=Provider()
        r = await compute_radial_boundary(request(),p,token)
        assert p.calls == 0 and r['geometry'] is None and r['status'] == 'cancelled'
    asyncio.run(run())


def test_all_reachable_to_domain_edge_is_truncated_not_localized():
    async def run():
        r = await compute_radial_boundary(request(),Provider(lambda x,y:400),CancelToken())
        assert r['truncated'] and not any(d['status']=='localized' for d in r['directions'])
        assert r['geometry'] is None
    asyncio.run(run())


def test_adaptive_directions_are_deterministic_and_bounded():
    async def run():
        outputs=[]
        for _ in range(2):
            outputs.append(await compute_radial_boundary(request(200),Provider(lambda x,y:(x*x+y*y)**.5*1.5),CancelToken(), adaptive=True))
        assert 8 < len(outputs[0]['directions']) <= 64
        assert outputs[0]['geometry'] == outputs[1]['geometry']
        assert outputs[0]['calls'] == outputs[1]['calls'] <= 200
    asyncio.run(run())


def test_ellipse_range_probe_rounding_does_not_remove_east_west_brackets():
    async def run():
        provider=Provider(lambda x,y:900*((x/850)**2+(y/500)**2)**.5)
        r=await compute_radial_boundary(request(),provider,CancelToken(),directions=16)
        assert all(d['status']=='localized' for d in r['directions'])
        assert r['uncoveredAngleFraction'] < 1e-10
    asyncio.run(run())
