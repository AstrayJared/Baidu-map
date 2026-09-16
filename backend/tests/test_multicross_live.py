import asyncio
import json
import time

import httpx
import pytest
from dataclasses import replace

from tools.multicross_live import LiveLedger, ConcurrentTransport
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.endpoint_radial_experiment import Synthetic
from tools.qps_review import metrics
from test_endpoint_radial_boundary import request
from life_circle.models import CancelToken


def route_request(i):
    return httpx.Request('GET','https://api.map.baidu.com/directionlite/v1/walking',params=dict(
        origin='31.313077,121.513926',destination=f'31.313077,{121.514+i*.00001:.6f}',
        coord_type='bd09ll',ret_coordtype='bd09ll',steps_info='1',ak='SECRET_TEST_ONLY'))


def test_transport_actual_concurrency_rate_and_event_identity(tmp_path):
    async def run():
        with LiveLedger(tmp_path) as ledger:
            ledger.arm();ledger.deadline=time.monotonic()+30
            async def handler(req):
                await asyncio.sleep(1.2)
                return httpx.Response(200,json={'status':0,'result':{'routes':[]}})
            transport=ConcurrentTransport(ledger,httpx.MockTransport(handler),qps=30,concurrency=30)
            await asyncio.gather(*(transport.handle_async_request(route_request(i)) for i in range(35)))
            m=metrics(ledger.data['events'])
            assert 2<=m['max_inflight']<=30 and m['max_in_one_second']<=30
            assert m['actual_handoffs']==35 and len({e['id'] for e in ledger.data['events']})==35
            assert all('responsePerf' in e and e['outcome']=='no_result' for e in ledger.data['events'])
            assert 'SECRET_TEST_ONLY' not in ledger.file.read_text()
            assert 'api.map.baidu.com' not in ledger.file.read_text()
    asyncio.run(run())


def test_retry_attempts_use_same_transport_budget_and_rate_gate(tmp_path):
    from life_circle.providers import BaiduProvider
    from life_circle.scheduler import Scheduler
    async def run():
        with LiveLedger(tmp_path) as ledger:
            ledger.arm();ledger.deadline=time.monotonic()+30;attempts=0
            def handler(req):
                nonlocal attempts
                attempts+=1
                return httpx.Response(200,json={'status':1} if attempts==1 else {'status':0,'result':{'routes':[]}})
            t=ConcurrentTransport(ledger,httpx.MockTransport(handler))
            async with httpx.AsyncClient(transport=t) as client:
                p=BaiduProvider('SECRET_TEST_ONLY',client=client)
                s=Scheduler(replace(request(),budget=2,qps=30,concurrency=30),p,ledger.token)
                await s.query((121.514,31.313077))
                assert attempts==s.stats.requests==ledger.data['counts']['analysis']==2
                assert s.stats.retries==1 and metrics(ledger.data['events'])['max_in_one_second']<=30
    asyncio.run(run())


def test_budget_restart_and_cancel_stop_new_transmissions(tmp_path):
    class TinyLedger(LiveLedger):
        phase_limits={'analysis':4};total_limit=4
    async def run():
        with TinyLedger(tmp_path) as ledger:
            ledger.arm();ledger.deadline=time.monotonic()+30;count=0
            async def handler(req):
                nonlocal count
                count+=1
                return httpx.Response(200,json={'status':0,'result':{'routes':[]}})
            t=ConcurrentTransport(ledger,httpx.MockTransport(handler),qps=1000,concurrency=30)
            await asyncio.gather(*(t.handle_async_request(route_request(i)) for i in range(10)),return_exceptions=True)
            assert count==4 and ledger.data['counts']['analysis']==4
            ledger.token.cancel()
            with pytest.raises(httpx.RequestError):await t.handle_async_request(route_request(11))
            assert count==4
        with TinyLedger(tmp_path) as reopened:
            with pytest.raises(httpx.RequestError):reopened.arm()
    asyncio.run(run())


def test_permission_failure_halts_other_waiters(tmp_path):
    async def run():
        with LiveLedger(tmp_path) as ledger:
            ledger.arm();ledger.deadline=time.monotonic()+30
            t=ConcurrentTransport(ledger,httpx.MockTransport(lambda req:httpx.Response(200,json={'status':101})),qps=30,concurrency=30)
            await asyncio.gather(*(t.handle_async_request(route_request(i)) for i in range(30)),return_exceptions=True)
            assert ledger.token.cancelled and ledger.data['halted']=='permission'
            assert ledger.data['counts']['analysis']==1
    asyncio.run(run())


def test_parallel_sampling_deterministic_and_no_real_provider_by_default():
    async def run():
        outputs=[]
        for _ in range(2):
            p=Synthetic('reentry_diagnostic')
            r=await compute_multicross_boundary(replace(request(),concurrency=30),p,CancelToken(),radial_step=100,parallel_sampling=True)
            assert r['calls']==p.calls<=400
            outputs.append(r)
        assert outputs[0]['geometry']==outputs[1]['geometry']
        p=Synthetic('circle');p.network=True
        with pytest.raises(ValueError):await compute_multicross_boundary(request(),p,CancelToken())
        assert p.calls==0
    asyncio.run(run())
