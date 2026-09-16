"""Frozen E1 observation replay, no provider invocation or surface reconstruction."""
import argparse
import copy
import hashlib
import json
import platform
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

from life_circle.coordinates import LocalProjection
from tools.diagnostic_common import no_network
from tools.endpoint_observations import build_layer, diagnose_triangle

ROOT=Path(__file__).resolve().parents[2]
CENTER=(121.513926,31.313077)
EXTENT=1600
INPUTS={
    'adaptive800':(Path('D:/CodexOutputs/guodingyi-adaptive-800-20260913/ledger.json'),
        '12245bea7e2e5fb975f9b4cdb766f292d5de085e5457f017abf4fb9901182a36'),
    'comparison400':(Path('D:/CodexOutputs/guodingyi-boundary-comparison-v2-continuation/ledger.json'),
        '820872044db67424b95668677cdaabc219285a0293b8ddd7bf7645431f98654d'),
}


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def write(path, data):
    path.write_text(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False),encoding='utf-8')


def synthetic_cases():
    projection=LocalProjection(CENTER)
    def event(i=1, requested=(100,100), actual=None):
        return dict(id=i,origin=list(CENTER),destination=list(projection.to_geographic(requested)),
            route_origin=list(CENTER),route_destination=list(projection.to_geographic(actual or requested)),
            duration=800,outcome='success',endpoint_verified=True)
    zero=event();small=event(actual=(112,100))
    # Wall x=105 is scenario annotation only: the layer receives no wall feature.
    wall=event(actual=(112,100));wall['duration']=1100
    drift=event(2);drift['route_origin']=list(projection.to_geographic((1,0)))
    conflict=event(2);conflict['duration']=1100
    missing=event();missing['route_destination']=None
    outside=event(requested=(1590,100),actual=(1601,100))
    return {'zero':[zero],'small_offset':[small],'across_wall':[wall],
        'duplicate':[zero,event(2,(110,100),(100,100))], 'conflict':[zero,conflict],
        'origin_drift':[zero,drift],'missing_endpoint':[missing],'outside_extent':[outside]}


def validate_case(name,layer):
    s=layer['summary'];r=layer['records']
    checks={
        'zero':s['usable_records']==1 and r[0]['request_pair_exactly_supported'],
        'small_offset':s['usable_records']==1 and not r[0]['request_pair_exactly_supported'],
        'across_wall':s['usable_records']==1 and not r[0]['request_pair_exactly_supported'] and r[0]['duration']==1100,
        'duplicate':s['spatial_points']==1 and s['duplicate_excess']==1,
        'conflict':s['conflicted_points']==1 and s['threshold_conflicts']==1 and s['consensus_points']==0,
        'origin_drift':s['origin_groups']==2 and s['spatial_points']==2,
        'missing_endpoint':s['usable_records']==0 and 'missing_actual_destination' in r[0]['issues'],
        'outside_extent':s['usable_records']==0 and 'outside_extent' in r[0]['issues'],
    }
    return checks[name]


def execute(output, inputs=None):
    inputs=INPUTS if inputs is None else inputs
    output=Path(output)
    output.mkdir(parents=True,exist_ok=False)
    (output/'started.marker').write_text('E1 exclusive run; do not reset',encoding='ascii')
    write(output/'status.json',dict(status='preparing',real_requests=0))
    try:
        sources={str(path):expected for path,expected in inputs.values()}
        if any(digest(p)!=expected for p,expected in sources.items()):
            raise ValueError('Frozen input mismatch')
        code=[ROOT/'backend/tools/endpoint_observations.py',Path(__file__),
              ROOT/'backend/docs/当前算法版本_E8.3.md']
        production=[*sorted((ROOT/'life-circle-algorithm/src/life_circle').glob('*.py')),
                    *sorted((ROOT/'backend/app').rglob('*.py'))]
        frozen=dict(protocol='E1',created_utc=datetime.now(timezone.utc).isoformat(),
            head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            python=platform.python_version(),inputs=sources,code={str(p):digest(p) for p in code},
            production={str(p):digest(p) for p in production},center=CENTER,extent=EXTENT,real_requests=0)
        write(output/'freeze.json',frozen)
        with no_network():
            started=time.perf_counter()
            cases={}
            for name,events in synthetic_cases().items():
                before=copy.deepcopy(events)
                layer=build_layer(events,name,CENTER,EXTENT)
                passed=validate_case(name,layer) and events==before and build_layer(list(reversed(events)),name,CENTER,EXTENT)==layer
                cases[name]=dict(passed=passed,layer=layer,
                    boundary_accuracy_evaluated=False,obstacle_connectivity_verified=False)
            write(output/'synthetic.json',cases)
            cohorts={};excluded=0
            for source,(path,_) in inputs.items():
                data=json.loads(path.read_text(encoding='utf-8'))
                selected=[e for e in data['events'] if e.get('phase') in ('adaptive','uniform','radial')]
                excluded+=len(data['events'])-len(selected)
                for phase in sorted({e['phase'] for e in selected}):
                    name=f'{source}-{phase}'
                    events=[e for e in selected if e['phase']==phase]
                    before=copy.deepcopy(events);t=time.perf_counter()
                    layer=build_layer(events,name,CENTER,EXTENT)
                    elapsed=time.perf_counter()-t
                    if events!=before or layer!=build_layer(list(reversed(events)),name,CENTER,EXTENT):
                        raise ValueError('Mutation or nondeterministic output')
                    write(output/f'{name}.json',layer)
                    cohorts[name]=dict(**layer['summary'],build_seconds=elapsed)
            unchanged=all(digest(p)==h for group in ('inputs','code','production') for p,h in frozen[group].items())
            if not unchanged: raise ValueError('Frozen source changed during run')
            result=dict(passed=all(c['passed'] for c in cases.values()),stage='E1_observation_contract_only',
                synthetic_cases={k:v['passed'] for k,v in cases.items()},cohorts=cohorts,
                excluded_reference_events=excluded,sources_unchanged=unchanged,real_requests=0,
                reconstruction_runs=0,reference_evaluations=0,total_seconds=time.perf_counter()-started)
            write(output/'summary.json',result)
            write(output/'status.json',dict(status='completed' if result['passed'] else 'failed',real_requests=0))
            return result
    except BaseException:
        write(output/'status.json',dict(status='failed',real_requests=0,error='Experiment stopped; no raw exception recorded'))
        raise


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output',type=Path,default=Path('D:/CodexOutputs/guodingyi-endpoint-e1-20260914'))
    args=parser.parse_args()
    result=execute(args.output)
    print(json.dumps(result,ensure_ascii=False))
    if not result['passed']: raise SystemExit(1)


if __name__=='__main__': main()
