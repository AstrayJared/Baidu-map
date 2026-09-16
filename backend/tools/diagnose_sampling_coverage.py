"""D1: once-only, sequence-exact development replay with read-only observers."""
import argparse
import asyncio
import inspect
import json
import time
from dataclasses import asdict
from pathlib import Path

import numpy as np

from life_circle.coordinates import normalize,LocalProjection
from life_circle.mesh import Mesh,Cell
from life_circle.models import IsochroneRequest,RouteObservation
from tools.audit_adaptive_budget import DecisionObserver,ReplayMissing,VirtualClock,instrument_engine,cell_evidence
from tools.diagnostic_common import (RunLedger,DiagnosticStop,read_json,no_network,write_json,
    audit_old,verify_inputs,runtime_manifest,require_development)


def capture_state(mesh):
    return dict(extent=mesh.extent,leaves=[[c.x,c.y,c.size] for c in sorted(mesh.leaves)],
        samples=[dict(local=list(p),observation=asdict(o)) for p,o in sorted(mesh.samples.items())],
        active_points=[list(p) for p in sorted(mesh.active_points)],
        shared_edges=[dict(cell=[c.x,c.y,c.size],edges=mesh.edges(c)) for c in sorted(mesh.leaves)],
        triangles=[dict(vertices=v,values=values) for _,v,values in mesh.triangles()])


def restore_mesh(state):
    mesh=Mesh(state['extent'],2*state['extent'])
    mesh.leaves={Cell(*c) for c in state['leaves']}
    mesh.samples={tuple(s['local']):RouteObservation(**{**s['observation'],
        'destination':tuple(s['observation']['destination'])}) for s in state['samples']}
    mesh.active_points={tuple(p) for p in state['active_points']}
    # Exact structure, not just a bag of coordinates.
    actual=json.loads(json.dumps(capture_state(mesh)))
    expected=json.loads(json.dumps(state))
    if actual['triangles']!=expected['triangles'] or actual['shared_edges']!=expected['shared_edges']:
        raise DiagnosticStop('Restored evidence structure differs')
    return mesh


class CoverageObserver(DecisionObserver):
    def __init__(self,candidate=False):
        super().__init__()
        self.candidate=candidate
        self.lifecycle={}
        self.final=None

    def capture(self,event,state):
        mesh,scheduler,request=(state[k] for k in ('mesh','scheduler','request'))
        self.mesh,self.scheduler=mesh,scheduler
        if event=='finished':
            self.final=capture_state(mesh)
            for record in self.lifecycle.values():
                record['budget_age_at_end']=scheduler.stats.requests-record['first_consumed']
            return
        if event in ('initial','exploration','explore_action'):
            super().capture(event,state)
            return
        row=dict(id=len(self.decisions),phase=event,consumed=scheduler.stats.requests,remaining=scheduler.remaining,
            selected=cell_evidence(mesh,state['cell'],request.boundary_band),
            action=self.action(state,state['cell']),attempts=[],queue=[],cache_keys=sorted(scheduler.cache),
            observed_wall_clock_wait=None)
        for negative_score,cell in sorted(state['queue']):
            score,candidate,residual=mesh.priority(cell,request.boundary_band)
            action=self.action(state,cell)
            identity=','.join(f'{x:g}' for x in (cell.x,cell.y,cell.size))
            entry=self.lifecycle.setdefault(identity,dict(cell=[cell.x,cell.y,cell.size],first_decision=row['id'],
                first_consumed=scheduler.stats.requests,first_affordable=None,first_selected=None,
                logical_defer=0,budget_block=0,selected=0,observed_wall_clock_wait=None))
            if action['affordable'] and entry['first_affordable'] is None:
                entry['first_affordable']=dict(decision=row['id'],consumed=scheduler.stats.requests,cost=action['cost'])
            if cell==state['cell']:
                entry['selected']+=1
                if entry['first_selected'] is None:
                    entry['first_selected']=dict(decision=row['id'],consumed=scheduler.stats.requests)
            elif action['affordable']:
                entry['logical_defer']+=1
            if action['cost'] and not action['affordable']:
                entry['budget_block']+=1
            row['queue'].append(dict(cell=[cell.x,cell.y,cell.size],raw_score=score,ranking_score=-negative_score,
                candidate=candidate,residual=residual,action=action,
                reason='selected' if cell==state['cell'] else 'ranking_deferred' if action['affordable'] else 'unaffordable_or_settle'))
        self.decisions.append(row)


class RecordedProvider:
    network=False
    identity=('D1-ordered-records',)

    def __init__(self,records,observer=None,consume=None):
        self.records,self.observer,self.consume=records,observer,consume
        self.calls=[]

    async def query_walking_time(self,origin,destination,deadline):
        i=len(self.calls);point=normalize(destination)
        if i>=len(self.records) or tuple(self.records[i]['point'])!=point:
            raise ReplayMissing(f'Record order/missing at {i+1}; no fallback allowed')
        record=self.records[i]
        if self.consume:
            self.consume(dict(point=point,source_ordinal=i+1))
        self.calls.append(point)
        if self.observer:
            self.observer.attempt(i+1,dict(destination=point,outcome=record.get('reason') or 'valid'))
        return RouteObservation(point,record['duration'],record.get('reason'),endpoint_verified=not record.get('reason'))


async def replay_one(stored,records,*,candidate=False,consume=None):
    import life_circle.engine as engine
    observer=CoverageObserver(candidate)
    provider=RecordedProvider(records,observer,consume)
    compute=instrument_engine(inspect.getsource(engine),vars(engine),observer)
    started=time.perf_counter()
    result=await compute(IsochroneRequest(**stored['config']),provider,clock=VirtualClock(),
        experimental_far_discount=candidate)
    elapsed=time.perf_counter()-started
    wire=json.loads(json.dumps(result.to_dict(),allow_nan=False))
    checks=dict(sequence=provider.calls==[tuple(r['point']) for r in records],
        geometry=wire['geometry']==stored['geometry'],unknown=wire['unknownRegion']==stored['unknownRegion'],
        terminal=all(wire[k]==stored[k] for k in ('quality','stopReason','warnings')),
        reservation=wire['statistics']['requests']==stored['statistics']['requests'])
    if not all(checks.values()):
        raise DiagnosticStop(f'Strict replay diverged: {checks}')
    state=observer.final
    owners=observer.owners
    mesh=observer.mesh
    projection=LocalProjection(result.config.origin)
    shared=[dict(cell=[c.x,c.y,c.size],owners=sorted({owners[key]
        for p in {c.center,*c.corners,*(p for edge in mesh.edges(c) for p in edge)}
        if (key:=normalize(projection.to_geographic(p))) in owners})) for c in sorted(mesh.leaves)]
    return result,dict(checks=checks,state=state,result=wire,decisions=observer.decisions,
        lifecycle=list(observer.lifecycle.values()),exploration=observer.exploration,shared_benefit=shared,
        requests=len(provider.calls),experimental_far_discount=candidate,observed_wall_clock_wait=None,
        replay_seconds=elapsed,observer_seconds=observer.seconds)


def component_hits(reference,records):
    """Post-replay diagnostic only; no object here is passed to sampling."""
    labels,axis=reference['components'],reference['axis']
    rows=[]
    for label in np.unique(labels):
        if not label:
            continue
        mask=labels==label
        ys,xs=np.where(mask)
        hits=[]
        for ordinal,record in enumerate(records,1):
            x,y=record['local']
            i=int(np.clip(np.rint((x-axis[0])/(axis[1]-axis[0])),0,len(axis)-1))
            j=int(np.clip(np.rint((y-axis[0])/(axis[1]-axis[0])),0,len(axis)-1))
            # Truth-grid component membership is nearest-cell approximation,
            # while the <=900 observation test is exact at the requested point.
            if labels[j,i]==label and record['duration'] is not None and not record.get('reason'):
                hits.append(dict(ordinal=ordinal,duration=record['duration'],local=record['local']))
        positives=[h for h in hits if h['duration']<=900]
        negatives=[h for h in hits if h['duration']>900]
        rows.append(dict(component=int(label),grid_points=int(mask.sum()),bounds=[float(axis[xs.min()]),
            float(axis[ys.min()]),float(axis[xs.max()]),float(axis[ys.max()])],
            first_valid=hits[0] if hits else None,first_positive=positives[0] if positives else None,
            first_negative=negatives[0] if negatives else None,positive_hits=len(positives),valid_hits=len(hits),
            membership='nearest 20m truth-grid component; not a road-entry guarantee'))
    return rows


async def run_d1(old,output):
    ledger=RunLedger.open(output)
    audit=read_json(output/'D0.json')
    if audit.get('passed') is not True:
        raise DiagnosticStop('D0 must pass')
    verify_inputs(audit)
    directory=output/'D1';directory.mkdir(exist_ok=False)
    rows=read_json(old/'development/metrics.json')
    summaries=[]
    for row in rows:
        require_development(row['layout'])
        stem=f"{row['layout']}-{row['budget']}-{row['method']}"
        ledger.start('D1',stem,row['budget'])
        stored=read_json(old/f'development/{stem}.json')
        records=read_json(old/f'development/{stem}-samples.json')
        try:
            result,payload=await replay_one(stored,records,candidate=row['method']=='C1',
                consume=lambda e:ledger.consume('D1',stem,e))
            with np.load(old/f"{row['layout']}-evaluation.npz") as ref:
                hits=component_hits(ref,records)
            payload['component_hits']=hits
            write_json(directory/f'{stem}.json',payload)
            ledger.finish(stem,'completed',checks=payload['checks'])
        except BaseException:
            ledger.finish(stem,'failed',stop_reason='integrity_or_execution_failure')
            raise
        summary=dict(layout=row['layout'],budget=row['budget'],method=row['method'],requests=payload['requests'],
            replay_seconds=payload['replay_seconds'],component_hits=hits,checks=payload['checks'])
        summaries.append(summary)
        write_json(directory/'summary.json',summaries)
        print(json.dumps({k:v for k,v in summary.items() if k not in ('component_hits','checks')}),flush=True)
    verify_inputs(audit)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['audit','replay'])
    parser.add_argument('--old',type=Path,required=True)
    parser.add_argument('--output',type=Path,required=True)
    parser.add_argument('--snapshot',type=Path)
    args=parser.parse_args()
    if args.action=='audit':
        ledger=RunLedger.create(args.output)
        audit=audit_old(args.old,args.snapshot)
        write_json(args.output/'D0.json',audit)
        print(json.dumps({k:v for k,v in audit.items() if k not in ('inputs','runtime')}),flush=True)
    else:
        # Event loop creation may need an OS socketpair; block external sends
        # inside the running loop, not loop initialization on Windows.
        async def offline():
            with no_network():
                await run_d1(args.old,args.output)
        asyncio.run(offline())


if __name__=='__main__':
    main()
