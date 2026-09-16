"""One 400-attempt E8.2 live run, with transport-level 30 QPS / 30 slots."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
from pathlib import Path
import subprocess
import time
import xml.etree.ElementTree as ET

import httpx

from app.baidu import silence_transport_logs
from app.config import Settings
from life_circle.models import IsochroneRequest
from life_circle.providers import BaiduProvider
from tools.endpoint_boundary_surface_live import SurfaceLedger
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.live_smoke import LiveGuardError, ORIGIN, dump
from tools.qps_review import metrics

ROOT=Path(__file__).resolve().parents[2]
OUTPUT=Path('D:/CodexOutputs/guodingyi-e82-live')


class LiveLedger(SurfaceLedger):
    phase_limits={'analysis':400}
    total_limit=400


class ConcurrentTransport(httpx.AsyncBaseTransport):
    """Every retry passes through admission; each response owns its own event."""
    def __init__(self,ledger,inner,*,qps=30,concurrency=30):
        self.ledger,self.inner=ledger,inner
        self.interval=1/qps+0.0001;self.qps=qps
        self.lock=asyncio.Lock();self.slots=asyncio.Semaphore(concurrency)
        self.next_send=0;self.sent=[];self.streak=0

    def check(self):
        if self.ledger.token.cancelled or self.ledger.data['halted'] or time.monotonic()>=self.ledger.deadline:
            raise LiveGuardError('stopped_before_transport')

    async def pause(self,seconds):
        if seconds<=0:return
        try:await asyncio.wait_for(self.ledger.token.event.wait(),timeout=seconds)
        except asyncio.TimeoutError:pass
        self.check()

    async def handle_async_request(self,request):
        async with self.slots:
            async with self.lock:
                self.check()
                while True:
                    now=time.perf_counter()
                    self.sent=[t for t in self.sent if now-t<1]
                    when=max(self.next_send,(self.sent[0]+1.0001) if len(self.sent)>=self.qps else now)
                    if now>=when:break
                    await self.pause(when-now)
                self.check()
                event=self.ledger.reserve('analysis',request,time.monotonic())
                now=time.perf_counter()
                event.update(dispatchPerf=now,batch=1,outcome='dispatched')
                self.sent.append(now);self.next_send=now+self.interval
            # No shared "last event" lookup: concurrent responses can finish in any order.
            try:
                response=await self.inner.handle_async_request(request)
                await response.aread()
                try:payload=response.json()
                except ValueError:payload=None
                obs=BaiduProvider('offline-parser-only').parse(payload,tuple(event['origin']),tuple(event['destination']))
                reason=obs.reason
                if response.status_code!=200:
                    reason='permission' if response.status_code in (401,403) else 'invalid_parameter' if response.status_code==400 else 'rate_limit' if response.status_code==429 else 'temporary' if response.status_code>=500 else 'http_error'
                event.update(http_status=response.status_code,
                    baidu_status=payload.get('status') if isinstance(payload,dict) and type(payload.get('status')) is int else None,
                    outcome=reason or 'success',observed_duration=obs.observed_duration,
                    endpoint_verified=obs.endpoint_verified,route_origin=obs.route_origin,route_destination=obs.route_destination,
                    origin_offset_m=obs.origin_offset_m,destination_offset_m=obs.destination_offset_m)
                self.streak=self.streak+1 if reason in ('temporary','timeout','network_error') else 0
                if reason in ('permission','quota','invalid_parameter','rate_limit') or self.streak>=10:
                    self.ledger.data['halted']=reason if self.streak<10 else 'continuous_upstream_failure'
                    self.ledger.token.cancel()
                return response
            except asyncio.CancelledError:
                event['outcome']='cancelled_in_flight';raise
            except httpx.RequestError as error:
                event['outcome']='timeout' if isinstance(error,httpx.TimeoutException) else 'network_error'
                self.streak+=1
                if self.streak>=10:
                    self.ledger.data['halted']='continuous_upstream_failure';self.ledger.token.cancel()
                raise
            finally:
                event['responsePerf']=time.perf_counter()
                self.ledger.save()

    async def aclose(self):await self.inner.aclose()


def freeze():
    tests=OUTPUT/'offline.xml'
    tree=ET.parse(tests).getroot()
    if not list(tree.iter('testcase')) or any(int(s.get('failures',0))+int(s.get('errors',0)) for s in tree.iter('testsuite')):
        raise LiveGuardError('offline_tests_not_passed')
    sources=[*ROOT.joinpath('backend').rglob('*.py'),*ROOT.joinpath('life-circle-algorithm/src').rglob('*.py')]
    return dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
        source_sha256={p.relative_to(ROOT).as_posix():hashlib.sha256(p.read_bytes()).hexdigest() for p in sources},
        tests_sha256=hashlib.sha256(tests.read_bytes()).hexdigest(),
        dependencies={n:importlib.metadata.version(n) for n in ('numpy','shapely','contourpy','httpx','pytest')},
        started_utc=datetime.now(timezone.utc).isoformat(),
        config=dict(origin=ORIGIN,coordinateSystem='bd09ll',budget=400,qps=30,concurrency=30,
            radial_step=100,parallel_sampling=True,target=25,extent=1200,timeout=8,max_attempts=2,
            deadlineSeconds=600,seed=20260911,real_provider_explicitly_enabled=True))


async def run(ledger,settings):
    started=time.perf_counter();ledger.deadline=time.monotonic()+600
    transport=ConcurrentTransport(ledger,httpx.AsyncHTTPTransport(retries=0,trust_env=False,
        limits=httpx.Limits(max_connections=30,max_keepalive_connections=30)))
    async def progress():
        while True:
            await asyncio.sleep(5)
            events=ledger.data['events']
            value=dict(calls=ledger.data['counts']['analysis'],completed=sum('responsePerf' in e for e in events),
                elapsedSeconds=time.perf_counter()-started,halted=ledger.data['halted'])
            dump(ledger.root/'progress.json',value);print(json.dumps(value),flush=True)
    reporter=asyncio.create_task(progress())
    try:
        async with httpx.AsyncClient(transport=transport,trust_env=False,follow_redirects=False,timeout=8) as client:
            provider=BaiduProvider(settings.baidu_map_ak.get_secret_value(),client=client)
            request=IsochroneRequest(ORIGIN,'bd09ll',budget=400,qps=30,concurrency=30,extent=1200,max_extent=1200,expand=False)
            result=await compute_multicross_boundary(request,provider,ledger.token,radial_step=100,
                allow_network=True,parallel_sampling=True)
        result.update(dataSource='baidu_walking',elapsedSeconds=time.perf_counter()-started)
        dump(ledger.root/'observations.json',[asdict(o) for o in result['_observations']])
        dump(ledger.root/'result.json',{k:v for k,v in result.items() if not k.startswith('_')})
        summary=dict(status=result['status'],quality=result['quality'],stopReason=result['stopReason'],
            calls=result['calls'],reserved=ledger.data['counts']['analysis'],metrics=metrics(ledger.data['events']),
            elapsedSeconds=result['elapsedSeconds'],halted=ledger.data['halted'],
            observationReasons=dict(Counter(e.get('reason') for e in result['_evidence'])),
            phases=result['phases'],patches=result['localRepair']['patches'],geometry_available=result['geometry'] is not None,
            finished_utc=datetime.now(timezone.utc).isoformat())
        dump(ledger.root/'summary.json',summary);print(json.dumps(summary),flush=True)
    finally:
        reporter.cancel();await asyncio.gather(reporter,return_exceptions=True)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute-live',action='store_true',required=True)
    parser.add_argument('--env-file',type=Path,required=True)
    args=parser.parse_args();silence_transport_logs()
    try:
        settings=Settings(_env_file=args.env_file)
        if not settings.ak_configured or settings.analysis_provider!='baidu':raise LiveGuardError('missing_real_configuration')
        frozen=freeze();frozen['existing_service_qps']=settings.analysis_qps
        with LiveLedger(OUTPUT/'run-01') as ledger:
            ledger.arm();dump(ledger.root/'frozen.json',frozen)
            asyncio.run(run(ledger,settings))
    except Exception as error:
        print(json.dumps(dict(status='stopped',error_type=type(error).__name__)),flush=True)
        raise SystemExit(1) from None


if __name__=='__main__':main()
