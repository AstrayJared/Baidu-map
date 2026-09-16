"""Frozen, offline E6 boundary experiments. No real provider or prior holdout access."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
import hashlib
import json
import math
from pathlib import Path
import random

import httpx
import numpy as np
from shapely.geometry import box

from life_circle.coordinates import LocalProjection
from life_circle.models import CancelToken, IsochroneRequest, RouteObservation
from life_circle.providers import BaiduProvider
from life_circle.scheduler import Scheduler
from tools.diagnostic_common import RunLedger, no_network
from tools.endpoint_boundary import BoundarySession, region_decision

ROOT=Path(__file__).resolve().parents[2]
ORIGIN=(121.513926,31.313077)
PROJECTION=LocalProjection(ORIGIN)
FAMILIES=('smooth','jump','circle','offset12','offset60','transverse30','snap8','missing','origin_drift','uniform','narrow','hole')
INITIAL=((-200,-200),(200,-200),(200,200),(-200,200),(0,0))
EXPLORE=tuple((x,y) for x in (-130,0,130) for y in (-130,0,130))


def cases():
    result=[]
    for family in FAMILIES:
        for variant in range(2):
            rng=random.Random(int(hashlib.sha256(f'20260911:E6:{family}:{variant}'.encode()).hexdigest(),16))
            theta=.45*variant
            result.append(dict(id=f'{family}-{variant}',family=family,variant=variant,normal=[math.cos(theta),math.sin(theta)],
                boundary=rng.uniform(12,45),center=[rng.uniform(25,65),rng.uniform(25,65)],radius=rng.uniform(65,95),
                small_radius=rng.uniform(4,8),narrow_width=rng.uniform(3,7)))
    return result


def duration(case,xy):
    f=case['family'];n=case['normal'];u=sum(a*b for a,b in zip(n,xy))-case['boundary']
    radial=math.dist(xy,case['center'])
    if f=='uniform':return 400 if case['variant']==0 else 1900
    if f=='circle':return 400 if radial<=case['radius'] else 1900
    if f=='hole':return 1900 if radial<case['small_radius'] else 400
    if f=='narrow':return 400 if abs(u)<case['narrow_width']/2 else 1900
    if f=='smooth':return max(0,900+2*u) if case['variant']==0 else 900+800*math.tanh(u/.02)
    return 400 if u<=0 else 1900


class MockProvider:
    network=False

    def __init__(self,case,ledger):
        self.case=case;self.ledger=ledger;self.calls=0;self.identity=('e6-mock',case['id'])
        self.transport=httpx.MockTransport(self.respond)
        self.parser=BaiduProvider('offline-parser-only')

    def respond(self,request):
        lat,lng=map(float,request.url.params['destination'].split(','));xy=list(PROJECTION.to_local((lng,lat)))
        f=self.case['family'];n=self.case['normal'];shift=(0,0)
        if f in ('offset12','offset60'):shift=tuple(v*(12 if f=='offset12' else 60) for v in n)
        if f=='transverse30':shift=(-n[1]*30,n[0]*30)
        actual=[x+d for x,d in zip(xy,shift)]
        if f=='snap8':actual=[round(x/8)*8 for x in xy]
        if f=='missing' and abs(sum(x*v for x,v in zip(actual,n))-self.case['boundary'])<8:
            return httpx.Response(200,json={'status':1})
        start=ORIGIN
        if f=='origin_drift' and abs(sum(x*v for x,v in zip(actual,n))-self.case['boundary'])<35:
            start=PROJECTION.to_geographic((1,0))
        end=PROJECTION.to_geographic(actual)
        return httpx.Response(200,json=dict(status=0,result=dict(routes=[dict(duration=duration(self.case,actual),steps=[dict(
            start_location=dict(lng=start[0],lat=start[1]),end_location=dict(lng=end[0],lat=end[1]))])])))

    async def query_walking_time(self,origin,destination,deadline):
        if self.ledger:
            self.ledger.consume('model',self.case['id'],dict(destination=list(destination)))
        self.calls+=1
        request=httpx.Request('GET','https://offline.invalid/e6',params=dict(destination=f'{destination[1]},{destination[0]}',
            coord_type='bd09ll',ret_coordtype='bd09ll',steps_info=1))
        response=await self.transport.handle_async_request(request)
        return self.parser.parse(response.json(),origin,destination)


def reference_label(case,xy):
    """Independent classification formula; no provider invocation or observed duration."""
    f=case['family'];x,y=xy
    if f=='uniform':return case['variant']==0
    if f in ('circle','hole'):
        r2=(x-case['center'][0])**2+(y-case['center'][1])**2
        return r2<=case['radius']**2 if f=='circle' else r2>=case['small_radius']**2
    signed=x*case['normal'][0]+y*case['normal'][1]-case['boundary']
    return abs(signed)<case['narrow_width']/2 if f=='narrow' else signed<=0


def boundary_distance(case,xy):
    if xy is None or case['family']=='uniform':return None
    if case['family'] in ('circle','hole'):
        return abs(math.dist(xy,case['center'])-case['radius' if case['family']=='circle' else 'small_radius'])
    u=sum(a*b for a,b in zip(case['normal'],xy))-case['boundary']
    return min(abs(u-case['narrow_width']/2),abs(u+case['narrow_width']/2)) if case['family']=='narrow' else abs(u)


def roots(case,a,b):
    """Exact analytic intersections of this segment with the independent boundary."""
    d=np.array(b)-a;a=np.array(a);f=case['family']
    if f=='uniform':return []
    if f in ('circle','hole'):
        c=a-np.array(case['center']);r=case['radius' if f=='circle' else 'small_radius']
        aa=float(d@d);bb=float(2*c@d);cc=float(c@c-r*r);disc=bb*bb-4*aa*cc
        values=[] if disc<0 or aa==0 else [(-bb-math.sqrt(disc))/(2*aa),(-bb+math.sqrt(disc))/(2*aa)]
    else:
        n=np.array(case['normal']);den=float(n@d)
        boundaries=[case['boundary']]
        if f=='narrow':boundaries=[case['boundary']-case['narrow_width']/2,case['boundary']+case['narrow_width']/2]
        values=[] if den==0 else [(v-float(n@a))/den for v in boundaries]
    return sorted(t for t in values if -1e-10<=t<=1+1e-10)


def quantiles(values):
    values=[v for v in values if v is not None]
    return dict(n=len(values),median=float(np.median(values)) if values else None,
                p95=float(np.quantile(values,.95)) if values else None,max=max(values) if values else None)


def evaluate_bracket(case,result,records):
    a,b=result['left'],result['right'];start,end=map(lambda r:np.array(r['xy']),[a,b]);w=result['width_m']
    points={tuple(r['xy']) for r in records};counts=Counter(TP=0,FP=0,FN_known=0,FN_unknown=0,TN=0,negative_unknown=0)
    refs=[];direction=(end-start)/w
    for i in range(201):
        t=-5+(i+.371)*(w+10)/201;xy=start+t*direction
        if tuple(xy) in points:continue
        truth=bool(reference_label(case,xy))
        pred=(a['duration']<=900) if t<0 else (b['duration']<=900) if t>w else None
        key=('TP' if pred is True else 'FN_known' if pred is False else 'FN_unknown') if truth else (
             'FP' if pred is True else 'TN' if pred is False else 'negative_unknown')
        counts[key]+=1;refs.append(dict(xy=xy.tolist(),truth=truth,prediction=pred))
    left,right=result['initial'];fraction=(900-left['duration'])/(right['duration']-left['duration'])
    before=[x+fraction*(y-x) for x,y in zip(left['xy'],right['xy'])]
    return dict(counts=dict(counts),reference_points=refs,source='analytic local boundary-neighbourhood; not area accuracy',
        initial_linear_distance_m=boundary_distance(case,before),final_midpoint_distance_m=boundary_distance(case,result['midpoint_xy']),
        conservative_distance_m=boundary_distance(case,result['reachable_endpoint']['xy']),
        crossings_in_final_bracket=roots(case,start,end))


async def run_case(case,ledger=None):
    if ledger:ledger.start('model',case['id'],96)
    provider=MockProvider(case,ledger)
    scheduler=Scheduler(IsochroneRequest(ORIGIN,'bd09ll',budget=96,concurrency=1,expand=False),provider,CancelToken())
    session=BoundarySession(scheduler,box(-200,-200,200,200))
    initial=[]
    for xy in INITIAL:initial.append((await session.measure(xy,'initial'))[0])
    decision=region_decision(initial[:4],initial[-1]);before_exploration=provider.calls
    for xy in EXPLORE:await session.measure(xy,'exploration')
    exploration_calls=provider.calls-before_exploration
    candidates=session.candidate_edges();results=[]
    # Freeze discovery edges before processing: this stage verifies local
    # refinement, not adaptive global connection of newly discovered surfaces.
    for a,b in candidates:results.append(await session.refine(a,b))
    rows=[]
    for result in results:rows.append(dict(result=result,evaluation=evaluate_bracket(case,result,session.records)))
    localized=[r for r in rows if r['result']['status']=='localized']
    counts=Counter()
    for r in rows:counts.update(r['evaluation']['counts'])
    report=dict(case=case,initial_region=decision,provider_calls=provider.calls,stats=asdict(scheduler.stats),
        exploration_calls=exploration_calls,unique_actual_points=len(session.records),observations=session.log,records=session.records,
        candidates=len(candidates),localized=len(localized),unfinished_reasons=dict(Counter(r['result']['reason'] for r in rows if r['result']['reason'])),
        midpoint_distance_m=quantiles([r['evaluation']['final_midpoint_distance_m'] for r in localized]),
        same_pairs_initial_linear_distance_m=quantiles([r['evaluation']['initial_linear_distance_m'] for r in localized]),
        conservative_distance_m=quantiles([r['evaluation']['conservative_distance_m'] for r in localized]),
        all_brackets_midpoint_distance_m=quantiles([r['evaluation']['final_midpoint_distance_m'] for r in rows]),
        localized_width_m=quantiles([r['result']['width_m'] for r in localized]),
        local_reference_counts=dict(counts),no_boundary_discovered=bool(case['family']!='uniform' and not candidates),brackets=rows,
        suspected_jump_brackets=sum(r['result']['suspected_jump'] for r in rows))
    assert provider.calls==scheduler.stats.requests<=96
    assert all(r['result']['width_m']<=.5 and r['evaluation']['crossings_in_final_bracket'] for r in localized)
    if ledger:ledger.finish(case['id'],'completed',provider_calls=provider.calls)
    scheduler.close()
    return report


def historical():
    from app.endpoint_model import load_endpoint_model
    folder=Path('D:/CodexOutputs/guodingyi-endpoint-e5/run-01/live')
    files={str(p):sha(p) for p in folder.rglob('*') if p.is_file()}
    model=load_endpoint_model(next((folder/'models').glob('*.json')))
    scheduler=Scheduler(IsochroneRequest(ORIGIN,'bd09ll'),MockProvider(cases()[0],None),CancelToken())
    s=BoundarySession(scheduler,box(*model.metadata['domain']))
    for o in model.observations:
        s.ingest(RouteObservation(**{k:v for k,v in o.items() if k!='id'}))
    edges=s.candidate_edges()
    refs=[r for r in json.loads((folder/'validation-predictions.json').read_text()) if 'excluded' not in r]
    near=[r for r in refs if 720<=r['observed_duration']<=1080]
    result=dict(actual_records=len(s.records),mixed_edges=len(edges),widths_m=quantiles([math.dist(a['xy'],b['xy']) for a,b in edges]),
        candidates=[dict(left=a,right=b,width_m=math.dist(a['xy'],b['xy'])) for a,b in edges],network_requests=0,
        previous_boundary_band=dict(n=len(near),fp=sum(r['observed_duration']>900 and r['prediction']['within_threshold'] is True for r in near),
             fn=sum(r['observed_duration']<=900 and r['prediction']['within_threshold'] is not True for r in near)),
        missing_new_observations=True,input_hashes=files)
    assert all(sha(Path(p))==h for p,h in files.items())
    return result


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()
def write(path,value):
    content=json.dumps(value,ensure_ascii=False,indent=2,allow_nan=False)
    with path.open('x',encoding='utf-8') as f:f.write(content)


async def run(output):
    ledger=RunLedger.create(output,{'model':2304})
    files=[p for folder in ('backend/tools','backend/app','life-circle-algorithm/src') for p in (ROOT/folder).rglob('*.py')]
    files.append(ROOT/'backend/docs/当前算法版本_E8.3.md')
    manifest=dict(cases=cases(),source_hashes={str(p):sha(p) for p in files},network_requests=0,seed=20260911)
    write(output/'frozen.json',manifest)
    results=[]
    for case in manifest['cases']:
        r=await run_case(case,ledger);write(output/(case['id']+'.json'),r);results.append(r)
        print(json.dumps({k:r[k] for k in ('case','provider_calls','candidates','localized','unfinished_reasons','midpoint_distance_m')},ensure_ascii=False),flush=True)
    hist=historical();write(output/'historical.json',hist)
    brief=[{k:r[k] for k in r if k not in ('observations','records','brackets')} for r in results]
    assert all(sha(Path(p))==h for p,h in manifest['source_hashes'].items())
    write(output/'summary.json',dict(layouts=brief,total_calls=ledger.data['used']['model'],network_requests=0,
        source_unchanged=True,historical_mixed_edges=hist['mixed_edges']))


async def guarded_run(output):
    # Windows initializes an internal loopback socketpair for the event loop.
    # Install the external-network guard after that initialization, before work.
    with no_network():await run(output)


if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    asyncio.run(guarded_run(args.output))
