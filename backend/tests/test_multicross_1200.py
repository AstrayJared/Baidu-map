import asyncio
import time
from dataclasses import replace

import httpx

from life_circle.models import CancelToken
from test_endpoint_radial_boundary import request
from test_multicross_live import route_request
from tools.multicross_live import ConcurrentTransport
from tools.multicross_live_1200 import Live1200Ledger
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.endpoint_radial_experiment import Synthetic
from tools.qps_review import metrics


def test_transport_can_reach_thirty_real_mock_inflight_without_thirty_one(tmp_path):
    async def run():
        with Live1200Ledger(tmp_path) as ledger:
            ledger.arm();ledger.deadline=time.monotonic()+20
            gate=asyncio.Event();entered=0;active=0;peak=0
            async def handler(req):
                nonlocal entered,active,peak
                entered+=1;active+=1;peak=max(peak,active)
                if entered==30:gate.set()
                await gate.wait()
                active-=1
                return httpx.Response(200,json={'status':0,'result':{'routes':[]}})
            t=ConcurrentTransport(ledger,httpx.MockTransport(handler),qps=30,concurrency=30)
            await asyncio.wait_for(asyncio.gather(*(t.handle_async_request(route_request(i)) for i in range(35))),10)
            assert peak==30 and metrics(ledger.data['events'])['max_inflight']==30
            assert metrics(ledger.data['events'])['max_in_one_second']<=30
    asyncio.run(run())


def test_budget_is_cap_circle_stops_early_and_batched_repair_has_diagnostics():
    async def run():
        p=Synthetic('circle')
        r=await compute_multicross_boundary(replace(request(1200),concurrency=30),p,CancelToken(),
            radial_step=100,parallel_sampling=True,edge_batch_size=30)
        assert r['calls']==112<1200
        assert r['completion']['budgetExhausted'] is False
        assert r['completion']['scope']=='observed_local_boundary_only'
        outputs=[]
        for _ in range(2):
            p=Synthetic('reentry_diagnostic')
            r=await compute_multicross_boundary(replace(request(400),concurrency=30),p,CancelToken(),
                radial_step=100,parallel_sampling=True,edge_batch_size=30)
            assert r['calls']==p.calls<=400
            assert r['localRepair']['edgeBatches']
            assert all(b['points']<=30 for b in r['localRepair']['edgeBatches'])
            assert r['completion']['unresolvedEdges']>=0
            assert r['completion']['pendingUnattemptedEdges']>=0
            outputs.append(r)
        assert outputs[0]['geometry']==outputs[1]['geometry']
    asyncio.run(run())


def test_sampled_boundary_exhaustion_is_not_reported_as_resolution_success():
    async def run():
        p=Synthetic('reentry_diagnostic')
        r=await compute_multicross_boundary(replace(request(160),concurrency=30),p,CancelToken(),
            radial_step=100,parallel_sampling=True,edge_batch_size=30)
        assert r['completion']['budgetExhausted']
        assert not r['completion']['resolutionReached']
    asyncio.run(run())


def test_last_successful_batch_is_ingested_when_budget_is_exhausted():
    from test_endpoint_boundary import session,Provider
    from tools.endpoint_multicross_boundary import measure_batch
    async def run():
        s,p,_=session(Provider(),budget=2)
        records=await measure_batch(s,[(-100,0),(100,0),(150,0)],'batch')
        assert p.calls==2
        assert len(s.observations)==2
        assert records[0][0] is not None and records[1][0] is not None
        assert records[2]==(None,'budget')
    asyncio.run(run())
