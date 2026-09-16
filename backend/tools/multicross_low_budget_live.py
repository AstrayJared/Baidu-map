"""Two fixed independent E8.2 runs: 200 + 300 attempts, no reset or extra arms."""
import argparse
import asyncio
from dataclasses import asdict
from datetime import datetime, timezone
import hashlib
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
from tools.multicross_live import LiveLedger, ConcurrentTransport
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.live_smoke import ORIGIN, dump, LiveGuardError
from tools.qps_review import metrics

ROOT=Path(__file__).resolve().parents[2]
OUTPUT=Path('D:/CodexOutputs/guodingyi-e82-low-budgets')

class LowBudgetLedger(LiveLedger):
    def __init__(self,root,budget):
        if type(budget) is not int or budget not in (200,300):raise ValueError('fixed_budgets_only')
        self.phase_limits={'analysis':budget};self.total_limit=budget
        super().__init__(root)

async def run_arm(ledger,settings,budget):
    start=time.perf_counter();ledger.deadline=time.monotonic()+600
    transport=ConcurrentTransport(ledger,httpx.AsyncHTTPTransport(retries=0,trust_env=False,
        limits=httpx.Limits(max_connections=30,max_keepalive_connections=30)),qps=30,concurrency=30)
    async with httpx.AsyncClient(transport=transport,timeout=8,trust_env=False,follow_redirects=False) as client:
        provider=BaiduProvider(settings.baidu_map_ak.get_secret_value(),client=client)
        request=IsochroneRequest(ORIGIN,'bd09ll',budget=budget,qps=30,concurrency=30,
            extent=1200,max_extent=1200,expand=False)
        result=await compute_multicross_boundary(request,provider,ledger.token,radial_step=100,
            allow_network=True,parallel_sampling=True,edge_batch_size=30)
    elapsed=time.perf_counter()-start
    result.update(dataSource='baidu_walking',elapsedSeconds=elapsed)
    dump(ledger.root/'observations.json',[asdict(o) for o in result['_observations']])
    dump(ledger.root/'result.json',{k:v for k,v in result.items() if not k.startswith('_')})
    summary=dict(budget=budget,calls=result['calls'],reserved=ledger.data['counts']['analysis'],
        observationCount=len(result['_observations']),elapsedSeconds=elapsed,metrics=metrics(ledger.data['events']),
        halted=ledger.data['halted'],status=result['status'],quality=result['quality'],completion=result['completion'],
        phases=result['phases'],finished_utc=datetime.now(timezone.utc).isoformat())
    dump(ledger.root/'summary.json',summary);print(json.dumps(summary),flush=True)
    return summary

def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--execute-live',action='store_true',required=True)
    p.add_argument('--env-file',type=Path,required=True);args=p.parse_args()
    silence_transport_logs()
    try:
        settings=Settings(_env_file=args.env_file)
        if not settings.ak_configured or settings.analysis_provider!='baidu':raise LiveGuardError('missing_config')
        tests=OUTPUT/'offline.xml';tree=ET.parse(tests).getroot()
        if not list(tree.iter('testcase')) or any(int(s.get('errors',0))+int(s.get('failures',0)) for s in tree.iter('testsuite')):raise LiveGuardError('offline_not_passed')
        # Arm both durable ledgers before any transmission; rerunning cannot create a second experiment.
        with LowBudgetLedger(OUTPUT/'200',200) as low,LowBudgetLedger(OUTPUT/'300',300) as high:
            for ledger in (low,high):
                if ledger.data['analysis_started'] or sum(ledger.data['counts'].values()):raise LiveGuardError('existing_run_no_reset')
            sources=[*ROOT.joinpath('backend').rglob('*.py'),*ROOT.joinpath('life-circle-algorithm/src').rglob('*.py')]
            frozen=dict(commit=subprocess.check_output(['git','rev-parse','HEAD'],cwd=ROOT,text=True).strip(),
                source_sha256={x.relative_to(ROOT).as_posix():hashlib.sha256(x.read_bytes()).hexdigest() for x in sources},
                tests_sha256=hashlib.sha256(tests.read_bytes()).hexdigest(),started_utc=datetime.now(timezone.utc).isoformat(),
                config=dict(budgets=[200,300],totalLimit=500,qps=30,concurrency=30,edge_batch_size=30,
                    origin=ORIGIN,extent=1200,threshold=900,target=25,radial_step=100,seed=20260911,timeout=8,max_attempts=2,deadline=600))
            dump(OUTPUT/'frozen.json',frozen)
            low.arm();high.arm()
            a=asyncio.run(run_arm(low,settings,200))
            if a['halted'] or a['status']=='cancelled':raise LiveGuardError('first_arm_halted_no_followup')
            b=asyncio.run(run_arm(high,settings,300))
            dump(OUTPUT/'summary.json',dict(arms=[a,b],totalReserved=a['reserved']+b['reserved'],totalLimit=500))
    except Exception as e:
        print(json.dumps(dict(status='stopped',error_type=type(e).__name__)),flush=True)
        raise SystemExit(1) from None

if __name__=='__main__':main()
