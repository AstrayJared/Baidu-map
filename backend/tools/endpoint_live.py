"""E5: one controlled 400-attempt task and 160 independent reference positions."""
import argparse
import asyncio
from collections import Counter, defaultdict
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import random
import time
import xml.etree.ElementTree as ET

import httpx
import numpy as np
from shapely.geometry import Point, shape

from app.analyses import RateGate, LimitedProvider
from app.baidu import silence_transport_logs
from app.config import Settings
from app.main import create_app
from app.endpoint_model import digest
from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.providers import BaiduProvider
from life_circle.scheduler import Scheduler
from tools.live_smoke import Ledger, LiveGuardError, ORIGIN, dump
from tools.qps_review import AuditedTransport, metrics as timing_metrics

ROOT=Path(__file__).resolve().parents[2]
OUTPUT=Path('D:/CodexOutputs/guodingyi-endpoint-e5/run-01')
LIMITS={'training':400,'validation':320}


def protocol():
    seed=int.from_bytes(hashlib.sha256(b'20260911:E5:validation').digest()[:16],'big')
    rng=random.Random(seed);projection=LocalProjection(ORIGIN)
    points=[list(normalize(projection.to_geographic((-1600+200*x+rng.uniform(1,199),
             -1600+320*y+rng.uniform(1,319))))) for x in range(16) for y in range(10)]
    return dict(version='E5-v1',origin=list(ORIGIN),validation=points,seed=20260911,
        validation_subseed=str(seed),limits=LIMITS,total=720,qps=3,concurrency=1,timeout=8,
        max_attempts=2,deadline_seconds=600,extent=1600,radius=200,threshold=900,expand=False)


class E5Ledger(Ledger):
    phase_limits=LIMITS
    total_limit=720

    def __init__(self,root,config):
        super().__init__(root);self.config=config;self.token=CancelToken();self.deadline=math.inf

    def reserve(self,phase,request,monotonic,**kwargs):
        if self.token.cancelled or time.monotonic()>=self.deadline:raise LiveGuardError('cancelled_or_deadline')
        params=request.url.params
        try:
            lat,lng=map(float,params['destination'].split(','));point=normalize((lng,lat))
            lat0,lng0=map(float,params['origin'].split(','))
        except (KeyError,ValueError):raise LiveGuardError('invalid_coordinate_parameters') from None
        if normalize((lng0,lat0))!=ORIGIN:raise LiveGuardError('unexpected_origin')
        if phase=='validation' and list(point) not in self.config['validation']:raise LiveGuardError('unplanned_reference')
        if phase=='training' and any(abs(x)>1600.1 for x in LocalProjection(ORIGIN).to_local(point)):
            raise LiveGuardError('outside_fixed_training_domain')
        return super().reserve(phase,request,monotonic,**kwargs)

    def save(self):
        try:super().save()
        except OSError:
            self.data['halted']='ledger_write_failed';self.token.cancel()
            raise LiveGuardError('ledger_write_failed') from None


class EndpointTransport(AuditedTransport):
    async def handle_async_request(self,request):
        response=await super().handle_async_request(request)
        event=self.ledger.data['events'][-1]
        try:payload=response.json()
        except ValueError:payload=None
        obs=BaiduProvider('offline-parser-only').parse(payload,tuple(event['origin']),tuple(event['destination']))
        event.update(observed_duration=obs.observed_duration,origin_offset_m=obs.origin_offset_m,
                     destination_offset_m=obs.destination_offset_m)
        if event.get('outcome') in ('permission','quota','invalid_parameter','rate_limit'):
            self.ledger.data['halted']=event['outcome']
            self.ledger.token.cancel()
        self.ledger.save()
        return response


def classification(pairs):
    c=Counter(TP=0,FP=0,FN_known=0,FN_unknown=0,TN=0,negative_unknown=0)
    for truth,pred in pairs:
        c['TP' if pred is True else 'FN_known' if pred is False else 'FN_unknown']+=int(truth)
        c['FP' if pred is True else 'TN' if pred is False else 'negative_unknown']+=int(not truth)
    pos=c['TP']+c['FN_known']+c['FN_unknown'];neg=c['TN']+c['FP']+c['negative_unknown']
    ratio=lambda a,b:a/b if b else None
    return dict(c,n=len(pairs),positives=pos,negatives=neg,miss_rate=ratio(c['FN_known']+c['FN_unknown'],pos),
        false_inclusion_rate=ratio(c['FP'],c['TP']+c['FP']),fp_rate=ratio(c['FP'],neg),
        unknown_fraction=ratio(c['FN_unknown']+c['negative_unknown'],len(pairs)))


def evaluate(model,baseline,observations):
    groups=defaultdict(list);excluded=Counter();rows=[]
    training={tuple(r['xy']) for r in model.records}
    for index,obs in enumerate(observations):
        row=dict(index=index,request=list(obs.destination),actual=obs.route_destination,
                 observed_duration=obs.observed_duration,reason=obs.reason,origin_offset_m=obs.origin_offset_m,
                 destination_offset_m=obs.destination_offset_m);rows.append(row)
        reason=None
        if obs.observed_duration is None or obs.reason not in (None,'endpoint_offset'):reason='failed_reference'
        elif not obs.endpoint_verified or obs.route_origin is None or obs.route_destination is None:reason='missing_endpoints'
        elif [list(ORIGIN),list(obs.route_origin)]!=model.origin_key:reason='different_actual_origin'
        else:
            xy=tuple(model.projection.to_local(obs.route_destination));row['local']=xy
            if not model.domain.covers(Point(xy)):reason='outside_domain'
            elif xy in training:reason='training_endpoint_overlap'
        if reason:row['excluded']=reason;excluded[reason]+=1
        else:groups[tuple(row['actual'])].append(row)
    new=[];old=[];errors=[]
    legacy_geom=shape(baseline['geometry']) if baseline['geometry'] else None
    legacy_unknown=shape(baseline['unknownRegion'])
    for point,items in groups.items():
        if len({r['observed_duration'] for r in items})!=1:
            excluded['reference_time_conflict']+=len(items)
            for row in items:row['excluded']='reference_time_conflict'
            continue
        row=items[0]
        for extra in items[1:]:extra['excluded']='duplicate_actual_endpoint';excluded['duplicate_actual_endpoint']+=1
        result=model.query_bd09(point);row['prediction']=result
        truth=row['observed_duration']<=900
        pred=None if legacy_geom is None or legacy_unknown.covers(Point(point)) else legacy_geom.covers(Point(point))
        row['baseline_prediction']=pred;row['truth_within_threshold']=truth
        new.append((truth,result['within_threshold']));old.append((truth,pred))
        if result['duration_seconds'] is not None:errors.append(result['duration_seconds']-row['observed_duration'])
    e=np.asarray(errors);candidate=classification(new);legacy=classification(old)
    time_error=dict(known_n=len(errors),mae_seconds=float(np.mean(abs(e))) if len(e) else None,
        median_absolute_seconds=float(np.median(abs(e))) if len(e) else None,
        p95_absolute_seconds=float(np.quantile(abs(e),.95)) if len(e) else None,
        bias_seconds=float(np.mean(e)) if len(e) else None)
    enough=candidate['n']>=120 and candidate['positives']>=20 and candidate['negatives']>=20
    return dict(candidate=candidate,legacy=legacy,time_error=time_error,excluded=dict(excluded),
                requested_reference_positions=len(observations),sample_sufficient=enough,
                scope='independent Baidu actual-endpoint observations; not field walking truth'),rows


def sha(path):return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def prepare(output=OUTPUT):
    output=Path(output);p=output/'live';p.mkdir(exist_ok=False)
    config=protocol()
    files=[f for d in ['backend/app','backend/tools','life-circle-algorithm/src','backend/tests']
           for f in (ROOT/d).rglob('*.py')]
    files+=[ROOT/'backend/docs/当前算法版本_E8.3.md',ROOT/'backend/requirements.lock.txt']
    config['sources']={str(f):sha(f) for f in files}
    config['createdAt']=datetime.now(timezone.utc).isoformat()
    dump(p/'protocol.json',config)
    print(json.dumps(dict(status='prepared',total_limit=720,validation_positions=160,protocol_sha256=sha(p/'protocol.json'))))


async def run(ledger,settings,config,root):
    gate=RateGate(3);transport=EndpointTransport(ledger,httpx.AsyncHTTPTransport(retries=0,trust_env=False))
    transport.phase='training'
    async with httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False) as route_client:
        provider=LimitedProvider(BaiduProvider(settings.baidu_map_ak.get_secret_value(),client=route_client),gate)
        app=create_app(settings.model_copy(update={'endpoint_snapshot_dir':root/'models'}),provider_factory=lambda _:provider)
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://e5-local') as api:
            response=await api.post('/api/analyses',json=dict(center=dict(lng=ORIGIN[0],lat=ORIGIN[1]),
                coordinateSystem='bd09ll',budget=400,clientRequestId='e5-training-once',reconstruction='actual_endpoints',radiusM=200))
            response.raise_for_status();task_id=response.json()['taskId'];manager=app.state.analyses;job=manager.get(task_id)
            ledger.token=job.token;ledger.deadline=time.monotonic()+600
            states=[]
            try:
                while True:
                    state=(await api.get(f'/api/analyses/{task_id}')).json();states.append(state)
                    if state['status'] in ('completed','failed','cancelled'):break
                    if len(states)%20==1:print(json.dumps(dict(stage='training',calls=ledger.data['counts']['training'],taskStatus=state['status'])),flush=True)
                    await asyncio.sleep(1)
                dump(root/'task-states.json',states)
                if state['status']!='completed' or ledger.data['halted'] or job.endpoint_model is None:
                    return dict(status='stopped_before_validation',task=state,halted=ledger.data['halted'])
                result=(await api.get(f'/api/analyses/{task_id}/result')).json()
                dump(root/'task-result.json',result);dump(root/'legacy-result.json',job.baseline_result)
                if result['isochrone']['quality']=='insufficient':return dict(status='insufficient_training')
                dump(root/'training-frozen.json',dict(task_result_sha256=sha(root/'task-result.json'),
                    legacy_result_sha256=sha(root/'legacy-result.json'),model_id=job.endpoint_model.model.model_id))
                transport.phase='validation';transport.batch=2;ledger.token=CancelToken();ledger.deadline=time.monotonic()+600
                scheduler=Scheduler(IsochroneRequest(ORIGIN,'bd09ll',budget=320,qps=3,concurrency=1,expand=False),provider,ledger.token)
                scheduler.on_progress=lambda: print(json.dumps(dict(stage='validation',calls=ledger.data['counts']['validation'])),flush=True) if scheduler.stats.requests%20==0 else None
                obs=await scheduler.observe_many(config['validation'])
                dump(root/'validation-observations.json',[asdict(o) for o in obs])
                metrics,rows=evaluate(job.endpoint_model,job.baseline_result,obs)
                dump(root/'validation-predictions.json',rows)
                # Exercise the integrated coordinate query route on a reference point, no extra Baidu calls.
                point=next((o.route_destination for o in obs if o.route_destination),ORIGIN)
                check=await api.post(f'/api/analyses/{task_id}/query',json=dict(coordinateSystem='bd09ll',points=[dict(lng=point[0],lat=point[1])]))
                check.raise_for_status();dump(root/'query-http-result.json',check.json())
                metrics.update(status='completed' if not ledger.data['halted'] else 'partial_stopped',halted=ledger.data['halted'],
                    source_unchanged=all(sha(p)==h for p,h in config['sources'].items()),
                    task_id=task_id,validation_stop=scheduler.stop_reason)
                return metrics
            finally:await manager.close()


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--prepare',action='store_true');parser.add_argument('--execute-live',action='store_true')
    parser.add_argument('--env-file',type=Path,help='Existing server configuration file; never copied into artifacts')
    args=parser.parse_args()
    if args.prepare and not args.execute_live:return prepare()
    if not args.execute_live or args.prepare:raise SystemExit('Choose --prepare or --execute-live')
    root=OUTPUT/'live';silence_transport_logs()
    try:
        config=json.loads((root/'protocol.json').read_text(encoding='utf-8'))
        if not all(sha(p)==h for p,h in config['sources'].items()):raise LiveGuardError('frozen_source_changed')
        tests=ET.parse(OUTPUT/'python-final.xml').getroot()
        if not list(tests.iter('testsuite')) or any(int(t.get('failures','0'))+int(t.get('errors','0')) for t in tests.iter('testsuite')):
            raise LiveGuardError('offline_checks_failed')
        settings=Settings(_env_file=args.env_file) if args.env_file else Settings()
        if not settings.ak_configured or settings.analysis_provider!='baidu' or settings.analysis_qps!=3:
            raise LiveGuardError('baidu_configuration_incomplete')
        with E5Ledger(root,config) as ledger:
            if sum(ledger.data['counts'].values()) or ledger.data.get('e5_started'):raise LiveGuardError('no_budget_reset_or_auto_resume')
            with (root/'started.marker').open('x',encoding='utf-8') as marker:marker.write(datetime.now(timezone.utc).isoformat())
            ledger.data['e5_started']=True;ledger.save()
            result=asyncio.run(run(ledger,settings,config,root))
            result.update(counts=ledger.data['counts'],timing=timing_metrics(ledger.data['events']),finishedAt=datetime.now(timezone.utc).isoformat())
            dump(root/'summary.json',result)
            print(json.dumps(result,ensure_ascii=False))
    except Exception as exc:
        # Exception text and HTTP URLs can contain credentials; type only.
        dump(root/'failure.json',dict(status='stopped',error_type=type(exc).__name__))
        raise SystemExit('E5 stopped; inspect sanitized ledger and failure type. No automatic retry.') from None


if __name__=='__main__':main()
