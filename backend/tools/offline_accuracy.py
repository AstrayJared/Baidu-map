"""Frozen single-candidate P1 experiment. Synthetic only; no credentials or HTTP.

prepare freezes all 20 layouts, source hashes and independent 20 m evaluation
arrays before either method runs. Each split may be revealed exactly once.
"""
import argparse
import asyncio
import csv
import hashlib
import importlib.metadata
import json
import os
import platform
import subprocess
import time
from collections import deque
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import Point, box

from life_circle.coordinates import LocalProjection
from life_circle.engine import compute_isochrone
from life_circle.experiments import evaluate, reference_geometry, write_svg
from life_circle.models import IsochroneRequest, RouteObservation
from life_circle.scenarios import accuracy_scenario
from tools.audit_adaptive_budget import sha, VirtualClock

ROOT = Path(__file__).resolve().parents[2]
ORIGIN = (121.513926,31.313077)
FAMILIES = ['smooth_roads','far_residual','detours','pocket_channel','endpoint_gaps']


def layout_definitions():
    definitions = []
    for family, name in enumerate(FAMILIES):
        for variant in range(4):
            identity = f'{name}-{variant}'
            seed = int.from_bytes(hashlib.sha256(f'20260911:P1:{identity}'.encode('ascii')).digest()[:16], 'big')
            rng = np.random.Generator(np.random.PCG64(seed))
            definitions.append(dict(id=identity, family=family, variant=variant,
                split='development' if variant < 2 else 'sealed', substream_seed=str(seed),
                angle=0. if variant==0 else float(rng.uniform(-np.pi,np.pi)),
                speed=1.2 if variant==0 else float(rng.uniform(1.05,1.35)),
                offset=100. if variant==0 else float(rng.uniform(-180,180))))
    return definitions


make_scenario = accuracy_scenario


def confusion(positive, prediction):
    positive, prediction = np.asarray(positive,dtype=bool), np.asarray(prediction)
    if positive.shape != prediction.shape:
        raise ValueError('truth/prediction shapes differ')
    counts = dict(TP=int(np.sum(positive & (prediction==1))), FP=int(np.sum(~positive & (prediction==1))),
        FN_known=int(np.sum(positive & (prediction==0))), FN_unknown=int(np.sum(positive & (prediction==-1))),
        TN=int(np.sum(~positive & (prediction==0))), negative_unknown=int(np.sum(~positive & (prediction==-1))))
    positive_n, negative_n, total = int(positive.sum()), int((~positive).sum()), positive.size
    ratio = lambda a,b: a/b if b else None
    return dict(**counts, positives=positive_n, negatives=negative_n, n=int(total),
        miss_rate=ratio(counts['FN_known']+counts['FN_unknown'],positive_n),
        false_inclusion_rate=ratio(counts['FP'],counts['TP']+counts['FP']),
        auxiliary_fp_rate=ratio(counts['FP'],negative_n),
        unknown_fraction=ratio(counts['FN_unknown']+counts['negative_unknown'],total))


def gate(baseline,candidate,*,development,new_severe,lost_components):
    fn_change = candidate['miss_rate']-baseline['miss_rate']
    fp_change = candidate['auxiliary_fp_rate']-baseline['auxiliary_fp_rate']
    checks = dict(miss_improvement=fn_change <= -.03 if development else fn_change < 0,
        auxiliary_fp_guard=fp_change <= .01, no_new_severe=new_severe==0, no_lost_components=lost_components==0)
    return dict(passed=all(checks.values()), checks=checks, miss_change_pp=100*fn_change,
                auxiliary_fp_change_pp=100*fp_change, new_severe=new_severe,lost_components=lost_components)


def components(mask):
    labels = np.zeros(mask.shape,dtype=np.int32)
    current = 0
    for j,i in zip(*np.where(mask)):
        if labels[j,i]:
            continue
        current += 1
        queue = deque([(j,i)])
        labels[j,i] = current
        while queue:
            y,x = queue.popleft()
            for yy,xx in ((y-1,x),(y+1,x),(y,x-1),(y,x+1)):
                if 0<=yy<mask.shape[0] and 0<=xx<mask.shape[1] and mask[yy,xx] and not labels[yy,xx]:
                    labels[yy,xx] = current
                    queue.append((yy,xx))
    return labels


def source_manifest():
    # Include tracked sources and the new experimental files; never read .env or caches.
    files = [p for directory in ('life-circle-algorithm/src','life-circle-algorithm/tests','backend/tools','backend/tests')
             for p in (ROOT/directory).rglob('*.py')]
    files += [ROOT/'backend/requirements.lock.txt',ROOT/'life-circle-algorithm/uv.lock']
    return {p.relative_to(ROOT).as_posix():sha(p) for p in sorted(files)}


def write_json(path, data):
    temporary = path.with_name(path.name+'.pending')
    with temporary.open('w',encoding='utf-8') as stream:
        stream.write(json.dumps(data,ensure_ascii=False,indent=2,allow_nan=False))
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary,path)


class RecordingFailed(BaseException):
    """A recording fault must bypass the scheduler's provider-error conversion."""


def validate_sealed_entry(output, protocol, digest):
    try:
        summary = json.loads((output/'development/summary.json').read_text(encoding='utf-8'))
        checks = summary['gate']['checks']
        admitted = (summary['split']=='development' and summary['status']=='completed'
            and summary['gate']['passed'] is True and bool(checks) and all(v is True for v in checks.values())
            and summary['source_unchanged'] is True and summary['failed_runs']==0
            and summary['completed_runs']==summary['expected_runs']==40
            and summary['protocol_sha256']==digest and summary['protocol_version']==protocol['version']
            and summary['candidate_version']==protocol['git_head'])
    except (OSError,ValueError,KeyError,TypeError):
        admitted = False
    if not admitted:
        raise ValueError('Development admission missing, failed, incomplete or version mismatched')


async def run_method(directory, stem, request, case, method, *, compute=None):
    """Checkpoint before observation; preserve failures and never reuse a run ID.

    Counts distinguish scheduler reservations from calls that entered the synthetic
    provider. A crash can leave the former larger; no attempt is credited back.
    """
    with (directory/f'{stem}-started.marker').open('x',encoding='ascii') as stream:
        stream.write('No restart, including after interruption')
        stream.flush(); os.fsync(stream.fileno())
    inner = SyntheticObservations(case)
    record = dict(status='running',reserved_attempts=0,actual_requests=0,provider_started=0,
        retries=0,stop_reason=None,elapsed_seconds=0)
    stopped = False

    def checkpoint():
        nonlocal stopped
        if stopped:
            raise RecordingFailed('recording already failed')
        record['actual_requests']=len(inner.calls)
        record['retries']=len(inner.calls)-len({tuple(c['point']) for c in inner.calls})
        try:
            write_json(directory/f'{stem}-samples.json',inner.calls)
            write_json(directory/f'{stem}-status.json',record)
        except Exception:
            stopped=True
            raise RecordingFailed('recording failed; no further observations') from None

    class Provider:
        network=False
        identity=inner.identity

        async def query_walking_time(self,origin,destination,deadline):
            if stopped or record['status']!='running':
                raise RecordingFailed('provider closed')
            record['provider_started']+=1
            checkpoint()
            observation=await inner.query_walking_time(origin,destination,deadline)
            checkpoint()
            return observation

    def progress(snapshot):
        record['reserved_attempts']=max(record['reserved_attempts'],snapshot.requests)
        checkpoint()

    checkpoint()
    result=None
    started=time.perf_counter()
    try:
        result=await (compute or compute_isochrone)(request,Provider(),
            clock=VirtualClock(),on_progress=progress,experimental_far_discount=method=='C1')
        record.update(status='completed',stop_reason=result.stop_reason,
            reserved_attempts=result.statistics.requests)
        if result.stop_reason=='cancelled':
            record['status']='cancelled'
            result=None
    except asyncio.CancelledError:
        record.update(status='cancelled',stop_reason='cancelled')
    except Exception:
        record.update(status='failed',stop_reason='execution_error')
    finally:
        record['elapsed_seconds']=time.perf_counter()-started
        checkpoint()
    return result,record


def prepare(output):
    if output.exists():
        raise ValueError('Experiment directory already exists; no overwrite/reset')
    output.mkdir(parents=True)
    axis = np.arange(-1600,1601,20.)
    xx,yy = np.meshgrid(axis,axis)
    definitions = layout_definitions()
    protocol = dict(version='P1-C1-v1', seed=20260911, definitions=definitions,
        source_sha256=source_manifest(), git_head=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        python=platform.python_version(), packages={p:importlib.metadata.version(p) for p in ('numpy','shapely','contourpy','httpx','pytest')},
        origin=ORIGIN, threshold=900,extent=1600,expand=False,budgets=[400,800],methods=['baseline','C1'],
        primary='latent truth, all 25921 points; failure does not change truth',
        generator='PCG64; SHA256 ASCII 20260911:P1:<layout-id>, first 16 bytes big endian',
        max_logical_attempts=48000, live_requests=0, independent_hashes={})
    for definition in definitions:
        case = make_scenario(definition)
        truth = np.asarray(case.truth(xx,yy),dtype=float)
        positive = truth<=900
        if not np.isfinite(truth).all() or not positive.any() or positive.all():
            raise ValueError('Invalid latent model or zero class denominator; stop before candidate')
        valid = np.array([not case.failure(x,y) if case.failure else True for x,y in zip(xx.ravel(),yy.ravel())]).reshape(xx.shape)
        target = output/f"{definition['id']}-evaluation.npz"
        np.savez_compressed(target,axis=axis,truth=truth,contract_valid=valid,components=components(positive))
        protocol['independent_hashes'][target.name] = sha(target)
    write_json(output/'protocol.json',protocol)
    (output/'protocol.sha256').write_text(sha(output/'protocol.json'),encoding='ascii')
    print(json.dumps(dict(prepared=str(output),layouts=20,reference_points_per_layout=25921,live_requests=0)))


class SyntheticObservations:
    network = False
    identity = ('P1-latent-model',)

    def __init__(self,case):
        self.case,self.calls = case,[]
        self.projection = LocalProjection(ORIGIN)

    async def query_walking_time(self,origin,destination,deadline):
        x,y = self.projection.to_local(destination)
        failed = bool(self.case.failure and self.case.failure(x,y))
        duration = None if failed else float(self.case.truth(x,y))
        self.calls.append(dict(point=destination,local=[x,y],duration=duration,reason='endpoint_offset' if failed else None))
        return RouteObservation(destination,duration,'endpoint_offset' if failed else None,endpoint_verified=not failed)


def predict(result,xx,yy):
    prediction = np.zeros(xx.shape,dtype=np.int8)
    if result.local_geometry is None:
        prediction.fill(-1)
        return prediction
    points = shapely.points(xx,yy)
    prediction[shapely.covers(result.local_geometry,points)] = 1
    if result.local_unknown is not None:
        prediction[shapely.covers(result.local_unknown,points)] = -1
    return prediction


def paired_guards(truth, labels, baseline, candidate):
    severe = np.flatnonzero(((truth>1020)&(candidate==1)&(baseline!=1)).ravel()).tolist()
    lost = [int(label) for label in np.unique(labels) if label and np.any((labels==label)&(baseline==1))
            and not np.any((labels==label)&(candidate==1))]
    return dict(new_severe_ids=severe,lost_component_ids=lost)


def macro(rows):
    keys = ('miss_rate','false_inclusion_rate','auxiliary_fp_rate','unknown_fraction')
    result, families = {}, {}
    for family in range(5):
        group = [r for r in rows if r['family']==family]
        if len(group)!=2:
            raise ValueError('Every family must have exactly two layouts; never discard failures')
        families[FAMILIES[family]] = {key:float(np.mean([r[key] for r in group]))
            if all(r[key] is not None for r in group) else None for key in keys}
    for key in keys:
        values = [v[key] for v in families.values()]
        result[key] = float(np.mean(values)) if all(v is not None for v in values) else None
    return dict(**result, families=families)


async def run(output,split):
    protocol = json.loads((output/'protocol.json').read_text(encoding='utf-8'))
    if sha(output/'protocol.json') != (output/'protocol.sha256').read_text(encoding='ascii'):
        raise ValueError('Protocol changed')
    if source_manifest()!=protocol['source_sha256']:
        raise ValueError('Source changed after freezing; no candidate runs allowed')
    if split == 'sealed':
        validate_sealed_entry(output,protocol,sha(output/'protocol.json'))
    for name,digest in protocol['independent_hashes'].items():
        if sha(output/name)!=digest:
            raise ValueError('Evaluation set changed')
    directory = output/split
    directory.mkdir(exist_ok=False)
    (directory/'started.marker').write_text('Do not rerun this split, including after failure.',encoding='utf-8')
    rows, paired, total = [], [], 0
    for definition in protocol['definitions']:
        if definition['split'] != split:
            continue
        case = make_scenario(definition)
        for budget in (400,800):
            predictions = {}
            for method in ('baseline','C1'):
                request = IsochroneRequest(ORIGIN,'bd09ll',budget=budget,expand=False,seed=20260911)
                stem = f"{definition['id']}-{budget}-{method}"
                result,record = await run_method(directory,stem,request,case,method)
                elapsed = record['elapsed_seconds']
                total += record['reserved_attempts']
                if record['actual_requests']>record['reserved_attempts'] or record['reserved_attempts']>budget or total>24000:
                    raise ValueError('Simulated budget/accounting violated')
                if result is not None and result.local_geometry is not None and (not result.local_geometry.is_valid or result.local_geometry.intersection(result.local_unknown).area>1e-7):
                    raise ValueError('Geometry/support invariant violated')
                # No sampler/provider holds the independent validation array. Load after computation.
                with np.load(output/f"{definition['id']}-evaluation.npz") as independent:
                    axis,truth,valid,labels = (independent[k] for k in ('axis','truth','contract_valid','components'))
                xx,yy = np.meshgrid(axis,axis)
                prediction = predict(result,xx,yy) if result is not None else np.full(xx.shape,-1,dtype=np.int8)
                predictions[method] = prediction
                row = dict(layout=definition['id'],family=definition['family'],split=split,budget=budget,method=method,
                    **confusion(truth<=900,prediction), contract_subset=confusion((truth<=900)[valid],prediction[valid]),
                    **record, quality=result.quality if result is not None else 'execution_failed',
                    unknown_area_fraction=result.local_unknown.area/(3200**2) if result is not None else 1.,
                    components=0 if result is None or result.local_geometry is None else len(result.local_geometry.geoms),
                    holes=0 if result is None or result.local_geometry is None else sum(len(p.interiors) for p in result.local_geometry.geoms))
                rows.append(row)
                stem = f"{definition['id']}-{budget}-{method}"
                write_json(directory/f'{stem}.json',result.to_dict() if result is not None else dict(geometry=None,**record))
                np.savez_compressed(directory/f'{stem}-predictions.npz',prediction=prediction)
                write_json(directory/'metrics.json',rows)
                if record['status']=='cancelled':
                    raise asyncio.CancelledError()
                if result is not None:
                    write_svg(directory/f'{stem}.svg',result.local_geometry,reference_geometry(case),result.local_unknown,stem)
                print(json.dumps(dict(layout=definition['id'],budget=budget,method=method,requests=record['actual_requests'],
                    miss_rate=row['miss_rate'],fp=row['FP'],seconds=elapsed)),flush=True)
            a,b = predictions['baseline'],predictions['C1']
            paired.append(dict(layout=definition['id'],budget=budget,**paired_guards(truth,labels,a,b)))
    base = macro([r for r in rows if r['budget']==800 and r['method']=='baseline'])
    candidate = macro([r for r in rows if r['budget']==800 and r['method']=='C1'])
    guards = paired  # Hard safety protections apply to both budgets; main numerical gain is 800 only.
    verdict = gate(base,candidate,development=split=='development',new_severe=sum(len(p['new_severe_ids']) for p in guards),
        lost_components=sum(len(p['lost_component_ids']) for p in guards))
    summary = dict(split=split,logical_attempts=total,live_requests=0,baseline=base,C1=candidate,gate=verdict,
        paired=paired,protocol_sha256=sha(output/'protocol.json'), source_unchanged=source_manifest()==protocol['source_sha256'],
        status='completed',protocol_version=protocol['version'],candidate_version=protocol['git_head'],
        completed_runs=len(rows),expected_runs=40,failed_runs=sum(r['status']!='completed' for r in rows))
    if not summary['source_unchanged']:
        raise ValueError('Source changed while running')
    write_json(directory/'metrics.json',rows)
    with (directory/'metrics.csv').open('w',encoding='utf-8-sig',newline='') as f:
        writer = csv.DictWriter(f,fieldnames=list(rows[0]));writer.writeheader()
        writer.writerows({k:json.dumps(v,ensure_ascii=False) if isinstance(v,dict) else v for k,v in r.items()} for r in rows)
    write_json(directory/'summary.json',summary)
    print(json.dumps(summary,ensure_ascii=False),flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action',choices=['prepare','development','sealed'])
    parser.add_argument('--output',type=Path,required=True)
    args = parser.parse_args()
    if args.action=='prepare':
        prepare(args.output)
    else:
        asyncio.run(run(args.output,args.action))


if __name__=='__main__':
    main()
