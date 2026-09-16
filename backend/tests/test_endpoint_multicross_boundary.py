import asyncio
import math

from shapely.geometry import Point, box

from life_circle.models import CancelToken
from test_endpoint_boundary import session, Provider
from test_endpoint_radial_boundary import request
from tools.endpoint_multicross_boundary import scan_ray, compute_multicross_boundary, connect_patch
from tools.endpoint_radial_experiment import Synthetic, local


def test_three_crossings_preserved_instead_of_only_outermost():
    async def run():
        s,p,_=session(Provider(lambda x,y:400 if x<650 or 700<x<1050 else 1900))
        s.domain=box(-1200,-1200,1200,1200)
        result=await scan_ray(s,0,610,1160,step=50,target=25)
        assert len(result['brackets'])==3
        assert all(b['width_m']<=25 for b in result['brackets'])
        assert [b['left']['duration']<=900 for b in result['brackets']]==[True,False,True]
    asyncio.run(run())


def test_repair_is_deterministic_and_records_more_than_one_crossing():
    async def run():
        outputs=[]
        for _ in range(2):
            p=Synthetic('reentry_diagnostic')
            r=await compute_multicross_boundary(request(),p,CancelToken())
            assert r['calls']==p.calls<=400
            assert r['localRepair']['patches']>0
            assert any(len(ray['brackets'])>=3 for ray in r['localRepair']['scanRays'])
            if r['geometry']:
                geometry=local(r['geometry'])
                assert all(not geometry.contains(Point(e['coordinateLocal'])) for e in r['negativeEvidence'])
            outputs.append(r)
        assert outputs[0]['geometry']==outputs[1]['geometry']
        assert outputs[0]['calls']==outputs[1]['calls']
    asyncio.run(run())


def test_unknown_sample_breaks_radial_adjacency():
    async def run():
        s,_,_=session()
        from unittest.mock import AsyncMock
        a=dict(id='a',xy=[600,0],duration=400,origin=['same'])
        b=dict(id='b',xy=[700,0],duration=1900,origin=['same'])
        s.measure=AsyncMock(side_effect=[(a,None),(None,'no_route'),(b,None)])
        s.refine=AsyncMock()
        r=await scan_ray(s,0,600,700,step=50,target=25)
        assert not r['brackets'] and len(r['unknown'])==1
        s.refine.assert_not_called()
    asyncio.run(run())


def test_actual_endpoint_stagnation_does_not_claim_localized_boundary():
    async def run():
        s,_,_=session(Synthetic('snap80'))
        s.domain=box(-1200,-1200,1200,1200)
        r=await scan_ray(s,0,600,800,step=50,target=25)
        assert r['brackets'] and any(b['status']!='localized' for b in r['brackets'])
    asyncio.run(run())


def test_patch_never_fills_negative_vertex_or_unknown_sample():
    records=[dict(id=str(i),xy=list(xy),duration=t,origin=['same']) for i,(xy,t) in enumerate([
        ((-100,-100),400),((100,-100),400),((100,100),400),((-100,100),400),((0,0),1900)])]
    r=connect_patch(records,box(-100,-100,100,100),[(50,0)],target=25)
    assert not r['reachable'].covers(Point(0,0))
    assert not r['reachable'].covers(Point(50,0))
    assert r['unknown'].area>0


def test_circle_avoids_patch_calls_and_small_budget_and_cancel_are_hard_limits():
    async def run():
        p=Synthetic('circle');r=await compute_multicross_boundary(request(),p,CancelToken())
        assert r['localRepair']['patches']==0 and r['calls']==112
        for budget in (4,150):
            p=Synthetic('reentry_diagnostic')
            r=await compute_multicross_boundary(request(budget),p,CancelToken())
            assert r['calls']==p.calls<=budget
        token=CancelToken();token.cancel();p=Synthetic('circle')
        r=await compute_multicross_boundary(request(),p,token)
        assert r['geometry'] is None and r['candidateGeometry'] is None and p.calls==0
    asyncio.run(run())


def test_new_negative_just_outside_patch_invalidates_old_fill_without_new_queries():
    from tools.endpoint_multicross_boundary import close_patch_evidence
    records=[dict(id=str(i),xy=list(xy),duration=t,origin=['same']) for i,(xy,t) in enumerate([
        ((-100,-100),400),((100,-100),400),((100,100),400),((-100,100),400),((60,0),1900)])]
    result=close_patch_evidence(records,box(-100,-100,50,100),box(-100,-100,100,100),[],target=25)
    assert result['expanded_area_m2']>0
    assert not result['estimate'].covers(Point(60,0))
    assert not result['conflicts']
