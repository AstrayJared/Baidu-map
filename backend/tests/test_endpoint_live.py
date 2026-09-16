import asyncio
import json
import time

import httpx
import pytest

from life_circle.coordinates import LocalProjection
from life_circle.models import CancelToken
from tools.live_smoke import LiveGuardError


def request(point):
    return httpx.Request('GET','https://api.map.baidu.com/directionlite/v1/walking',params={
        'origin':'31.313077,121.513926','destination':f'{point[1]},{point[0]}',
        'coord_type':'bd09ll','ret_coordtype':'bd09ll','steps_info':'1','ak':'fixture-not-a-key'})


def test_validation_is_frozen_unique_and_independent():
    from tools.endpoint_live import protocol
    a=protocol();b=protocol()
    assert a==b and len(a['validation'])==160
    assert len({tuple(v) for v in a['validation']})==160
    assert a['limits']=={'training':400,'validation':320}


def test_live_ledger_budget_restart_cancel_and_allowlist(tmp_path):
    from tools.endpoint_live import E5Ledger, protocol
    with E5Ledger(tmp_path,protocol()) as ledger:
        point=protocol()['validation'][0]
        ledger.data['counts']['validation']=319
        ledger.reserve('validation',request(point),1)
        with pytest.raises(LiveGuardError):ledger.reserve('validation',request(point),2)
        ledger.token.cancel()
        with pytest.raises(LiveGuardError):ledger.reserve('training',request(point),3)
    with E5Ledger(tmp_path,protocol()) as ledger:
        assert ledger.data['counts']['validation']==320
        with pytest.raises(LiveGuardError):ledger.reserve('validation',request([121.5,31.3]),4)


def test_transport_preserves_offset_and_stops_on_permission(tmp_path):
    from tools.endpoint_live import E5Ledger, EndpointTransport, protocol
    p=protocol()['validation'][0];calls=[]
    def respond(req):
        calls.append(1)
        return httpx.Response(200,json={'status':201})
    async def run():
        with E5Ledger(tmp_path,protocol()) as ledger:
            transport=EndpointTransport(ledger,httpx.MockTransport(respond));transport.phase='validation'
            await transport.handle_async_request(request(p))
            with pytest.raises(LiveGuardError):await transport.handle_async_request(request(p))
            assert ledger.data['halted']=='permission'
            assert len(calls)==1
            assert 'fixture-not-a-key' not in ledger.file.read_text(encoding='utf-8')
    asyncio.run(run())


def test_unknown_counts_as_miss_and_failed_reference_is_not_negative():
    from tools.endpoint_live import classification
    result=classification([(True,None),(True,True),(False,True),(False,False)])
    assert result['FN_unknown']==1 and result['FN_known']==0
    assert result['miss_rate']==.5 and result['false_inclusion_rate']==.5


def test_retry_counts_every_send_and_preserves_offset_seconds(tmp_path):
    from tools.endpoint_live import E5Ledger, EndpointTransport, protocol, ORIGIN
    from app.analyses import RateGate, LimitedProvider
    from life_circle.providers import BaiduProvider
    from life_circle.models import IsochroneRequest
    from life_circle.scheduler import Scheduler
    class Clock:
        now=time.monotonic()
        def time(self):return self.now
        async def sleep(self,n):self.now+=n
    clock=Clock();config=protocol();point=config['validation'][0];calls=[]
    shifted=LocalProjection(ORIGIN).to_geographic((300,0))
    def respond(req):
        calls.append(1)
        return httpx.Response(503) if len(calls)==1 else httpx.Response(200,json={'status':0,'result':{'routes':[
            {'duration':500,'steps':[{'start_location':dict(zip(['lng','lat'],ORIGIN)),
                                    'end_location':dict(zip(['lng','lat'],shifted))}]}]}})
    async def run():
        with E5Ledger(tmp_path,config) as ledger:
            transport=EndpointTransport(ledger,httpx.MockTransport(respond),clock=clock.time);transport.phase='validation'
            async with httpx.AsyncClient(transport=transport) as client:
                provider=LimitedProvider(BaiduProvider('fixture',client=client),RateGate(3,clock=clock.time,sleep=clock.sleep))
                scheduler=Scheduler(IsochroneRequest(ORIGIN,'bd09ll',budget=2,qps=3,concurrency=1),provider,ledger.token,clock)
                result=await scheduler.query(point)
                assert len(calls)==ledger.data['counts']['validation']==2
                assert scheduler.stats.retries==1 and result.observed_duration==500
                assert result.duration is None and result.reason=='endpoint_offset'
                assert ledger.data['events'][-1]['observed_duration']==500
    asyncio.run(run())


def test_failed_durable_write_stops_before_network(tmp_path,monkeypatch):
    from tools.endpoint_live import E5Ledger, EndpointTransport, protocol
    from tools.live_smoke import Ledger
    calls=[]
    async def run():
        with E5Ledger(tmp_path,protocol()) as ledger:
            transport=EndpointTransport(ledger,httpx.MockTransport(lambda req:calls.append(req)))
            transport.phase='validation'
            def failed(self):raise OSError('fixture disk failure')
            monkeypatch.setattr(Ledger,'save',failed)
            with pytest.raises(LiveGuardError):await transport.handle_async_request(request(protocol()['validation'][0]))
            assert not calls and ledger.token.cancelled
    asyncio.run(run())
