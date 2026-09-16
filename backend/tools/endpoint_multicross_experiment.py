"""Frozen offline E8.2 comparison; source truth never enters the algorithm."""
import argparse
import asyncio
import hashlib
import json
import math
from pathlib import Path

from shapely.affinity import rotate
from shapely.geometry import Point, Polygon

from life_circle.models import IsochroneRequest, CancelToken, RouteObservation
from tools.diagnostic_common import no_network
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.endpoint_radial_boundary import compute_radial_boundary
from tools.endpoint_radial_experiment import Synthetic, CASES, ORIGIN, P, REENTRY, reference, score, local
from tools.live_smoke import dump

CASES82=(*CASES,'reentry_rotated11','reentry_rotated33')
METHODS=('e81_fixed16','e81_adaptive8','e82_step50','e82_step100')


def truth_for(case):
    if case.startswith('reentry_rotated'):return rotate(REENTRY,int(case.removeprefix('reentry_rotated')),origin=(0,0))
    return reference(case)


class RotatedSynthetic(Synthetic):
    async def query_walking_time(self,origin,destination,deadline):
        if not self.case.startswith('reentry_rotated'):
            return await super().query_walking_time(origin,destination,deadline)
        self.calls+=1
        duration=400 if truth_for(self.case).covers(Point(P.to_local(destination))) else 1900
        return RouteObservation(destination,duration,route_origin=origin,route_destination=destination,
            request_origin=origin,origin_offset_m=0,destination_offset_m=0)


async def run(output):
    output.mkdir(parents=True,exist_ok=False)
    root=Path(__file__).resolve().parents[2]
    paths=sorted([*root.joinpath('backend').rglob('*.py'),*root.joinpath('life-circle-algorithm/src').rglob('*.py')])
    hashes={p.relative_to(root).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    dump(output/'protocol.json',dict(cases=CASES82,methods=METHODS,budget=400,extent_m=1200,
        seed=20260911,target_m=25,radial_trigger_m=100,margin_m=75,angular_spacing_m=75,
        network_enabled=False,source_sha256=hashes))
    metrics=[]
    for case in CASES82:
        truth=truth_for(case)
        for method in METHODS:
            provider=RotatedSynthetic(case)
            request=IsochroneRequest(ORIGIN,'bd09ll',budget=400,extent=1200,max_extent=1200,expand=False,concurrency=1)
            if method.startswith('e82'):
                r=await compute_multicross_boundary(request,provider,CancelToken(),radial_step=int(method.split('step')[1]))
            else:
                r=await compute_radial_boundary(request,provider,CancelToken(),boundary_bands=True,
                    directions=16 if method=='e81_fixed16' else 8,adaptive=method=='e81_adaptive8')
            candidate=r.get('candidateGeometry')
            candidate_score=score(dict(geometry=candidate),truth)
            pred=local(r['geometry']) if r['geometry'] else Polygon()
            cp=local(candidate) if candidate else Polygon()
            unknown=local(r['unknownRegion']) if r.get('unknownRegion') else Polygon()
            repair=r.get('localRepair',{})
            scans=repair.get('scanRays',[])
            brackets=[b for ray in scans for b in ray['brackets']]+repair.get('edgeBrackets',[])
            row=dict(case=case,method=method,calls=provider.calls,**score(r,truth),
                candidate_iou=candidate_score['outer_iou'],
                candidate_p95_truth_to_prediction_m=candidate_score['truth_to_prediction_p95_m'],
                candidate_p95_prediction_to_truth_m=candidate_score['prediction_to_truth_p95_m'],
                false_positive_m2=pred.difference(truth).area,false_negative_m2=truth.difference(pred).area,
                candidate_false_positive_m2=cp.difference(truth).area,
                candidate_false_negative_m2=truth.difference(cp).area,
                unknown_area_m2=unknown.area,unknown_area_available='unknownRegion' in r,
                patches=repair.get('patches',0),local_calls=repair.get('calls',0),
                multicross_rays=sum(len(ray['brackets'])>=3 for ray in scans),
                brackets=len(brackets),localized=sum(b['status']=='localized' for b in brackets),
                quality=r['quality'],negative_filled=sum(pred.buffer(-1e-6).covers(Point(P.to_local(e['coordinate']))) for e in r.get('negativeEvidence',[])),
                components=len(pred.geoms) if pred.geom_type=='MultiPolygon' else int(not pred.is_empty),
                holes=sum(len(p.interiors) for p in pred.geoms) if pred.geom_type=='MultiPolygon' else len(pred.interiors),
                internal_calls=r['phases'].get('interior_check',0))
            assert row['calls']==r['calls']<=400 and row['negative_filled']==0 and row['internal_calls']==0
            metrics.append(row)
            dump(output/f'{case}-{method}.json',{k:v for k,v in r.items() if not k.startswith('_')})
            dump(output/'metrics.json',metrics)
            print(json.dumps({k:row[k] for k in ('case','method','calls','outer_iou','candidate_iou','multicross_rays','unknown_area_m2')}),flush=True)
    unchanged=all(hashlib.sha256((root/n).read_bytes()).hexdigest()==h for n,h in hashes.items())
    dump(output/'audit.json',dict(runs=len(metrics),total_synthetic_calls=sum(r['calls'] for r in metrics),
        live_calls=0,source_hashes_unchanged=unchanged,budget_passed=True,negative_filled=0,internal_calls=0))
    assert unchanged


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,required=True)
    args=parser.parse_args()
    async def guarded():
        with no_network():await run(args.output)
    asyncio.run(guarded())


if __name__=='__main__':main()
