import asyncio
import math

import pytest
from shapely.geometry import box

from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken, IsochroneRequest, RouteObservation
from life_circle.scheduler import Scheduler
from tools.endpoint_boundary import BoundarySession, region_decision

ORIGIN = (121.513926, 31.313077)
P = LocalProjection(ORIGIN)


class Provider:
    network = False
    identity = ('e6-test',)

    def __init__(self, function=lambda x,y:400 if x<17 else 1900, shift=(0,0)):
        self.function, self.shift, self.calls = function, shift, 0

    async def query_walking_time(self, origin, destination, deadline):
        self.calls += 1
        x,y=P.to_local(destination); actual=P.to_geographic((x+self.shift[0],y+self.shift[1]))
        duration=self.function(x+self.shift[0],y+self.shift[1])
        offset=math.hypot(*self.shift)
        return RouteObservation(destination,duration,reason='endpoint_offset' if offset>50 else None,
            observed_duration=duration,route_origin=origin,route_destination=actual,request_origin=origin,
            destination_offset_m=offset,origin_offset_m=0)


def session(provider=None, budget=96):
    provider=provider or Provider(); token=CancelToken()
    scheduler=Scheduler(IsochroneRequest(ORIGIN,'bd09ll',budget=budget,concurrency=1,expand=False),provider,token)
    return BoundarySession(scheduler,box(-200,-200,200,200)),provider,token


def test_jump_is_queried_then_localized_without_any_900_second_observation():
    async def run():
        s,p,_=session(); a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial')
        r=await s.refine(a,b)
        assert r['status']=='localized' and r['width_m']<=.5
        assert abs(r['midpoint_xy'][0]-17)<=.25
        assert {v['duration'] for v in s.records}=={400,1900}
        assert r['iterations']>=3 and r['suspected_jump']
        assert r['linear_estimate']['usedForClassification'] is False
        assert r['interior_classification'] is None and p.calls>2
    asyncio.run(run())


@pytest.mark.parametrize('offset',[12,60])
def test_shifted_endpoints_and_compensated_request_find_actual_boundary(offset):
    async def run():
        s,p,_=session(Provider(shift=(offset,0)))
        a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial')
        assert a['xy'][0]==pytest.approx(-100+offset,abs=.06)
        r=await s.refine(a,b)
        assert r['status']=='localized' and abs(r['midpoint_xy'][0]-17)<=.25
        assert any(h['kind']=='offset_compensation' for h in r['history'])
        assert all(r['duration']==(400 if r['xy'][0]<17 else 1900) for r in s.records)
        assert s.scheduler.stats.requests==p.calls<=96
    asyncio.run(run())


def test_region_classification_requires_real_internal_center():
    def r(x,y,t):return dict(xy=[x,y],duration=t)
    corners=[r(-1,-1,800),r(1,-1,800),r(1,1,800),r(-1,1,800)]
    assert region_decision(corners,r(0,0,900))['status']=='inside_assumed'
    assert region_decision(corners,r(0,0,901))['status']=='mixed'
    assert region_decision(corners,r(2,0,800))['status']=='unknown'
    assert region_decision(corners,None)['status']=='unknown'
    assert region_decision([dict(v,duration=1000) for v in corners],r(0,0,1000))['status']=='outside_assumed'


def test_center_does_not_reuse_scheduler_derived_zero():
    async def run():
        s,p,_=session(Provider(lambda x,y:777));r,_=await s.measure((0,0),'center')
        assert p.calls==1 and r['duration']==777
        assert s.observations[0].attempts==1
    asyncio.run(run())


def test_budget_exhaustion_retains_unknown_with_separate_linear_fallback():
    async def run():
        s,p,_=session(budget=3)
        a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial')
        r=await s.refine(a,b)
        assert p.calls==3 and r['status']=='unfinished' and r['reason']=='budget'
        assert r['linear_estimate']['usedForClassification'] is False
        assert r['interior_classification'] is None
    asyncio.run(run())


def test_invalid_response_is_not_labelled_jump_and_retries_count():
    class Missing(Provider):
        async def query_walking_time(self,origin,destination,deadline):
            x,_=P.to_local(destination)
            if abs(x)<30:
                self.calls+=1;return RouteObservation(destination,reason='temporary')
            return await super().query_walking_time(origin,destination,deadline)
    async def run():
        s,p,_=session(Missing());a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial')
        r=await s.refine(a,b)
        assert r['status']=='unfinished' and r['reason']=='temporary' and not r['suspected_jump']
        assert p.calls==4 and s.scheduler.stats.retries==1
    asyncio.run(run())


def test_cancelled_late_response_cannot_update_registry_or_emit_estimate():
    class Slow(Provider):
        async def query_walking_time(self,origin,destination,deadline):
            if abs(P.to_local(destination)[0])<30:
                started.set()
                try:await release.wait()
                except asyncio.CancelledError:await release.wait()
            return await super().query_walking_time(origin,destination,deadline)
    async def run():
        nonlocal started,release
        started=asyncio.Event();release=asyncio.Event()
        s,_,token=session(Slow());a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial')
        task=asyncio.create_task(s.refine(a,b));await started.wait();token.cancel()
        r=await task;before=len(s.records);release.set();await asyncio.sleep(.03)
        assert r['status']=='cancelled' and r['linear_estimate'] is None
        assert len(s.records)==before==2
        count=s.scheduler.stats.requests;await s.measure((50,0),'later');assert s.scheduler.stats.requests==count
    started=release=None
    asyncio.run(run())


def test_conflicts_and_other_origins_never_shrink_bracket():
    s,_,_=session()
    def obs(t,start=ORIGIN):return RouteObservation(ORIGIN,t,attempts=1,route_origin=start,route_destination=P.to_geographic((20,0)),request_origin=ORIGIN)
    assert s.ingest(obs(800))[0] is not None
    assert s.ingest(obs(1000))[1]=='conflicting_observations'
    assert not s.records
    assert s.ingest(obs(700,P.to_geographic((1,0))))[1]=='different_actual_origin'


def test_all_new_observations_can_be_rebuilt_by_e5_model():
    async def run():
        from app.endpoint_model import build_endpoint_model
        s,_,_=session();a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial')
        await s.measure((0,100),'initial');await s.refine(a,b)
        rebuilt=build_endpoint_model(s.observations,ORIGIN,extent=200,radius=200,synthetic=False)
        last=s.records[-1];obs=next(o for o in s.observations if list(P.to_local(o.route_destination))==last['xy'])
        q=rebuilt.query_bd09(obs.route_destination)
        assert q['evidence']=='observed' and q['duration_seconds']==last['duration']
        assert len(rebuilt.records)==len(s.records)>3
    asyncio.run(run())


def test_snapped_repeated_endpoint_stops_and_preserves_wide_interval():
    class Snap(Provider):
        async def query_walking_time(self,origin,destination,deadline):
            x,y=P.to_local(destination);self.calls+=1
            actual=P.to_geographic((0 if x<50 else 100,0));t=400 if x<50 else 1900
            return RouteObservation(destination,t,route_origin=origin,route_destination=actual)
    async def run():
        s,p,_=session(Snap());a,_=await s.measure((0,0),'initial');b,_=await s.measure((100,0),'initial')
        r=await s.refine(a,b)
        assert r['reason']=='offset_stagnation' and r['width_m']>99
        assert p.calls<=4 and len(s.records)==2
    asyncio.run(run())


def test_transverse_offset_preserves_two_dimensional_actual_brackets():
    async def run():
        s,_,_=session(Provider(shift=(0,30)))
        a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial')
        r=await s.refine(a,b)
        assert r['status']=='localized' and abs(r['midpoint_xy'][0]-17)<=.25
        for h in r['history']:
            if h['accepted']:assert h['after_width_m']<=.75*h['before_width_m']
        assert r['left']['xy'][1]!=0 and r['right']['xy'][1]!=0
    asyncio.run(run())


def test_steep_continuous_field_can_trigger_only_a_suspected_jump():
    async def run():
        s,_,_=session(Provider(lambda x,y:900+800*math.tanh((x-17)/.02)))
        a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial');r=await s.refine(a,b)
        assert r['status']=='localized' and r['suspected_jump']
        assert 'confirmed_jump' not in r and r['interior_classification'] is None
    asyncio.run(run())


def test_experiment_parameters_and_independent_geometry_are_consistent():
    from tools.endpoint_boundary_experiment import cases,reference_label,roots,boundary_distance,duration
    assert cases()==cases() and len(cases())==24
    for c in cases():
        for xy in [(-71,39),(33,-17),(108,101)]:
            assert reference_label(c,xy)==(duration(c,xy)<=900)
        if c['family'] not in ('circle','hole','uniform','narrow'):
            a=[-200*n for n in c['normal']];b=[200*n for n in c['normal']]
            rr=roots(c,a,b)
            assert len(rr)==1
            point=[x+rr[0]*(y-x) for x,y in zip(a,b)]
            assert boundary_distance(c,point)<1e-10


def test_local_cell_driver_stops_uniform_cases_and_keeps_center_queries():
    from tools.endpoint_boundary_experiment import run_case
    from tools.diagnostic_common import no_network
    case=dict(id='unit-uniform',family='uniform',variant=0,normal=[1,0],boundary=17,center=[40,40],radius=80,
              small_radius=6,narrow_width=4)
    async def run():
        with no_network():return await run_case(case)
    result=asyncio.run(run())
    assert result['initial_region']['status']=='inside_assumed'
    assert result['provider_calls']==13 and result['candidates']==0
    assert result['exploration_calls']==8


def test_bracket_reference_results_are_serializable_before_artifact_creation():
    import json
    from tools.endpoint_boundary_experiment import evaluate_bracket,write
    async def run():
        s,_,_=session();a,_=await s.measure((-100,0),'initial');b,_=await s.measure((100,0),'initial')
        result=await s.refine(a,b)
        c=dict(family='jump',normal=[1,0],boundary=17)
        return evaluate_bracket(c,result,s.records)
    payload=asyncio.run(run())
    restored=json.loads(json.dumps(payload,allow_nan=False))
    assert all(type(r['truth']) is bool for r in restored['reference_points'])
