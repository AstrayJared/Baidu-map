"""Offline E8 pilot; no AK access and network calls blocked by the harness."""
import argparse
import asyncio
from collections import Counter
import hashlib
import json
import math
from pathlib import Path

import numpy as np
from shapely.geometry import Point, Polygon, LineString, mapping, shape
from shapely.ops import transform, unary_union

from life_circle.coordinates import LocalProjection
from life_circle.models import CancelToken, IsochroneRequest, RouteObservation
from tools.diagnostic_common import no_network
from tools.endpoint_boundary_surface import compute_boundary_surface
from tools.endpoint_radial_boundary import compute_radial_boundary
from tools.live_smoke import dump

ORIGIN=(121.513926,31.313077)
P=LocalProjection(ORIGIN)
CASES=('circle','ellipse','jump','concave','narrow','offset60','snap80','missing','island_diagnostic','reentry_diagnostic')
METHODS=('fixed8','fixed16','fixed32','adaptive8','e61')
REENTRY=unary_union([Point(0,0).buffer(650,quad_segs=360),Point(850,250).buffer(200,quad_segs=180),
                    LineString([(450,400),(850,450)]).buffer(45)])


def radius(case,angle):
    if case=='ellipse':
        return 1/math.sqrt((math.cos(angle)/850)**2+(math.sin(angle)/500)**2)
    if case=='concave':
        return 650+150*math.cos(3*angle)
    if case=='narrow':
        d=(angle-.17+math.pi)%(2*math.pi)-math.pi
        return 600+300*math.exp(-(d/.045)**2)
    return 650


def reference(case):
    if case=='reentry_diagnostic':
        return REENTRY
    return Polygon([(radius(case,a)*math.cos(a),radius(case,a)*math.sin(a))
                    for a in np.linspace(0,2*math.pi,1440,endpoint=False)])


class Synthetic:
    network=False
    def __init__(self,case):
        self.case=case; self.identity=('e8-synthetic',case); self.calls=0
    async def query_walking_time(self,origin,destination,deadline):
        self.calls+=1
        x,y=P.to_local(destination)
        if self.case=='offset60':x+=60
        if self.case=='snap80':x,y=round(x/80)*80,round(y/80)*80
        angle=math.atan2(y,x)
        if self.case=='missing' and .1<angle<.4 and math.hypot(x,y)>500:
            return RouteObservation(destination,reason='no_route')
        duration=900*math.hypot(x,y)/radius(self.case,angle)
        if self.case=='jump':duration=400 if duration<=900 else 1900
        if self.case=='island_diagnostic' and math.hypot(x-300,y)<80:duration=1900
        if self.case=='reentry_diagnostic':duration=400 if REENTRY.covers(Point(x,y)) else 1900
        actual=P.to_geographic((x,y)); offset=math.dist((x,y),P.to_local(destination))
        return RouteObservation(destination,duration,observed_duration=duration,
            reason='endpoint_offset' if offset>50 else None,route_origin=origin,
            route_destination=actual,request_origin=origin,origin_offset_m=0,destination_offset_m=offset)


def local(geometry):
    return transform(lambda x,y,z=None:((np.asarray(x)-P.origin[0])*P.sx,
                                       (np.asarray(y)-P.origin[1])*P.sy),shape(geometry))


def score(result,truth):
    pred=local(result['geometry']) if result['geometry'] is not None else Polygon()
    if pred.is_empty:
        return dict(outer_iou=0,truth_to_prediction_p95_m=None,prediction_to_truth_p95_m=None,
                    geometry_valid=True,geometry_empty=True)
    def distances(a,b):
        return [a.interpolate((i+.371)/1440,normalized=True).distance(b) for i in range(1440)]
    return dict(outer_iou=pred.intersection(truth).area/pred.union(truth).area,
        truth_to_prediction_p95_m=float(np.quantile(distances(truth.boundary,pred.boundary),.95)),
        prediction_to_truth_p95_m=float(np.quantile(distances(pred.boundary,truth.boundary),.95)),
        geometry_valid=pred.is_valid,geometry_empty=False)


async def experiment(output):
    output.mkdir(parents=True,exist_ok=True)
    repo=Path(__file__).resolve().parents[2]
    paths=sorted([*repo.joinpath('backend').rglob('*.py'),*repo.joinpath('life-circle-algorithm/src').rglob('*.py')])
    hashes={p.relative_to(repo).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    dump(output/'protocol.json',dict(seed=20260911,budget=400,extent=1200,target_m=25,
        cases=CASES,methods=METHODS,network_enabled=False,source_sha256=hashes,
        reference_samples=1440,interior_islands_excluded_from_target=True))
    rows=[]
    for case in CASES:
        truth=reference(case)
        dump(output/f'{case}-reference-local.json',dict(coordinateSystem='local_m',geometry=mapping(truth)))
        for method in METHODS:
            provider=Synthetic(case)
            request=IsochroneRequest(ORIGIN,'bd09ll',budget=400,extent=1200,max_extent=1200,expand=False,concurrency=1)
            if method=='e61':
                result=await compute_boundary_surface(request,provider,CancelToken())
            else:
                count=8 if method=='adaptive8' else int(method[5:])
                result=await compute_radial_boundary(request,provider,CancelToken(),directions=count,adaptive=method=='adaptive8')
            evidence=result['_evidence']
            widths=[r['bracket']['width_m'] for r in result.get('directions',[]) if r.get('bracket')]
            row=dict(case=case,method=method,calls=provider.calls,**score(result,truth),
                interior_calls=sum(e['kind']=='interior_check' for e in evidence),
                phases=dict(Counter(e['kind'] for e in evidence)),
                directions=len(result.get('directions',[])),
                localized=sum(r['status']=='localized' for r in result.get('directions',[])),
                bracket_width_p95_m=float(np.quantile(widths,.95)) if widths else None,
                uncovered_angle_fraction=result.get('uncoveredAngleFraction'),
                seed=20260911,live_calls=0)
            assert row['calls']<=400 and row['geometry_valid']
            if method!='e61':assert row['interior_calls']==0
            rows.append(row)
            dump(output/f'{case}-{method}.json',{k:v for k,v in result.items() if not k.startswith('_')})
            dump(output/'metrics.json',rows)
            print(json.dumps({k:row[k] for k in ('case','method','calls','outer_iou','truth_to_prediction_p95_m') }),flush=True)
    source_ok=all(hashlib.sha256((repo/name).read_bytes()).hexdigest()==sha for name,sha in hashes.items())
    dump(output/'audit.json',dict(source_hashes_unchanged=source_ok,runs=len(rows),
        total_synthetic_calls=sum(r['calls'] for r in rows),live_calls=0,
        budgets_passed=all(r['calls']<=400 for r in rows),
        radial_interior_calls=sum(r['interior_calls'] for r in rows if r['method']!='e61')))
    assert source_ok


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('D:/CodexOutputs/guodingyi-radial-boundary-e8/run-01'))
    args=parser.parse_args()
    if (args.output/'protocol.json').exists():raise SystemExit('Use a new offline output directory')
    async def guarded():
        # Windows creates the event loop's local socket pair before blocking network I/O.
        with no_network():
            await experiment(args.output)
    asyncio.run(guarded())


if __name__=='__main__':main()
