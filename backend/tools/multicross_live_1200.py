"""One authorized 1200-attempt cap with 30-point boundary batches and checkpoints."""
import argparse
import asyncio
from collections import Counter
from dataclasses import asdict
from datetime import datetime,timezone
from pathlib import Path
import time
import json
import hashlib
import subprocess
import xml.etree.ElementTree as ET

import httpx

from app.config import Settings
from app.baidu import silence_transport_logs
from life_circle.models import IsochroneRequest
from life_circle.providers import BaiduProvider
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.multicross_live import LiveLedger,ConcurrentTransport
from tools.live_smoke import ORIGIN,dump,LiveGuardError
from tools.qps_review import metrics

ROOT=Path(__file__).resolve().parents[2]
OUTPUT=Path('D:/CodexOutputs/guodingyi-e82-live-1200')


class Live1200Ledger(LiveLedger):
    phase_limits={'analysis':1200}
    total_limit=1200


async def run(ledger,settings):
    started=time.perf_counter();ledger.deadline=time.monotonic()+600;snapshots=[]
    async def checkpoint(value):
        value=dict(value,elapsedSeconds=time.perf_counter()-started)
        snapshots.append(value);dump(ledger.root/'checkpoints.json',snapshots)
        print(json.dumps({k:v for k,v in value.items() if k not in ('geometry','unknownRegion')}),flush=True)
    transport=ConcurrentTransport(ledger,httpx.AsyncHTTPTransport(retries=0,trust_env=False,
        limits=httpx.Limits(max_connections=30,max_keepalive_connections=30)))
    async def progress():
        while True:
            await asyncio.sleep(10)
            value=dict(calls=ledger.data['counts']['analysis'],elapsedSeconds=time.perf_counter()-started,halted=ledger.data['halted'])
            dump(ledger.root/'progress.json',value);print(json.dumps(value),flush=True)
    reporter=asyncio.create_task(progress())
    try:
        async with httpx.AsyncClient(transport=transport,timeout=8,trust_env=False,follow_redirects=False) as client:
            provider=BaiduProvider(settings.baidu_map_ak.get_secret_value(),client=client)
            request=IsochroneRequest(ORIGIN,'bd09ll',budget=1200,qps=30,concurrency=30,
                extent=1200,max_extent=1200,expand=False)
            result=await compute_multicross_boundary(request,provider,ledger.token,radial_step=100,
                allow_network=True,parallel_sampling=True,edge_batch_size=30,on_checkpoint=checkpoint)
        result.update(dataSource='baidu_walking',elapsedSeconds=time.perf_counter()-started,
            stopReason=result['completion']['reason'])
        dump(ledger.root/'observations.json',[asdict(o) for o in result['_observations']])
        dump(ledger.root/'result.json',{k:v for k,v in result.items() if not k.startswith('_')})
        summary=dict(calls=result['calls'],reserved=ledger.data['counts']['analysis'],status=result['status'],
            quality=result['quality'],stopReason=result['stopReason'],completion=result['completion'],
            elapsedSeconds=result['elapsedSeconds'],metrics=metrics(ledger.data['events']),
            halted=ledger.data['halted'],phases=result['phases'],
            edgeBatches=result['localRepair'].get('edgeBatches',[]),
            finished_utc=datetime.now(timezone.utc).isoformat())
        dump(ledger.root/'summary.json',summary);print(json.dumps(summary),flush=True)
    finally:
        reporter.cancel();await asyncio.gather(reporter,return_exceptions=True)


def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--execute-live',action='store_true',required=True)
    p.add_argument('--env-file',type=Path,required=True);args=p.parse_args();silence_transport_logs()
    try:
        settings=Settings(_env_file=args.env_file)
        if not settings.ak_configured or settings.analysis_provider!='baidu':raise LiveGuardError('missing_config')
        tests=OUTPUT/'offline.xml';tree=ET.parse(tests).getroot()
        if not list(tree.iter('testcase')) or any(int(s.get('errors',0))+int(s.get('failures',0)) for s in tree.iter('testsuite')):raise LiveGuardError('offline_not_passed')
        sources=[*ROOT.joinpath('backend').rglob('*.py'),*ROOT.joinpath('life-circle-algorithm/src').rglob('*.py')]
        frozen=dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
            source_sha256={x.relative_to(ROOT).as_posix():hashlib.sha256(x.read_bytes()).hexdigest() for x in sources},
            tests_sha256=hashlib.sha256(tests.read_bytes()).hexdigest(),started_utc=datetime.now(timezone.utc).isoformat(),
            config=dict(budget=1200,qps=30,concurrency=30,edge_batch_size=30,origin=ORIGIN,extent=1200,
                threshold=900,target=25,radial_step=100,timeout=8,max_attempts=2,deadline=600,seed=20260911))
        with Live1200Ledger(OUTPUT/'run-01') as ledger:
            ledger.arm();dump(ledger.root/'frozen.json',frozen);asyncio.run(run(ledger,settings))
    except Exception as error:
        print(json.dumps(dict(status='stopped',error_type=type(error).__name__)),flush=True);raise SystemExit(1) from None


if __name__=='__main__':main()
