"""Offline reproductions at simulated server receipt, not just gate permits."""
import asyncio

import httpx
import pytest

from app.analyses import LimitedProvider, RateGate
from life_circle.models import RouteObservation
from life_circle.providers import BaiduProvider


class FakeClock:
    def __init__(self):
        self.now = 100.0

    def time(self):
        return self.now

    async def sleep(self, delay):
        self.now += delay


def test_connection_delay_must_not_compress_server_receipt_window():
    async def run():
        clock, receipts = FakeClock(), []

        async def upstream(request):
            # First connection/DNS/TLS takes 200 ms; reused connections do not.
            if not receipts:
                await clock.sleep(.2)
            receipts.append(clock.time())
            count = sum(clock.time() - 1 + 1e-9 < t <= clock.time() for t in receipts)
            return httpx.Response(200, json={'status': 401 if count > 3 else 7})

        gate = RateGate(3, clock=clock.time, sleep=clock.sleep)
        async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
            provider = LimitedProvider(BaiduProvider('offline-fixture', client=client), gate)
            results = [await provider.query_walking_time((121.5, 31.3), (121.51, 31.3), float('inf')) for _ in range(4)]
        assert max(sum(t <= s < t + 1 - 1e-9 for s in receipts) for t in receipts) <= 3, receipts
        assert all(result.reason != 'rate_limit' for result in results)
    asyncio.run(run())


def test_shared_gate_holds_slot_until_response_and_cancels_waiter():
    async def run():
        clock, calls = FakeClock(), []
        entered, release = asyncio.Event(), asyncio.Event()

        class Slow:
            identity = ('slow',)

            async def query_walking_time(self, origin, destination, deadline):
                calls.append(destination)
                entered.set()
                await release.wait()
                return RouteObservation(destination, 10)

        gate = RateGate(3, clock=clock.time, sleep=clock.sleep)
        first = LimitedProvider(Slow(), gate)
        second = LimitedProvider(Slow(), gate)  # Separate jobs share one gate.
        task = asyncio.create_task(first.query_walking_time((0, 0), (1, 1), 110))
        await entered.wait()
        waiter = asyncio.create_task(second.query_walking_time((0, 0), (2, 2), 110))
        try:
            for _ in range(5):
                await asyncio.sleep(0)
            assert calls == [(1, 1)]
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)
            release.set()
            await task
        assert calls == [(1, 1)]
        assert (await second.query_walking_time((0, 0), (3, 3), 110)).duration == 10
    asyncio.run(run())


def test_rate_limit_cools_down_shared_gate_before_retry_or_next_job():
    async def run():
        clock, calls = FakeClock(), []

        class Limited:
            identity = ('limited',)

            async def query_walking_time(self, origin, destination, deadline):
                calls.append(clock.time())
                return RouteObservation(destination, reason='rate_limit')

        gate = RateGate(3, clock=clock.time, sleep=clock.sleep)
        for _ in range(2):
            await LimitedProvider(Limited(), gate).query_walking_time((0, 0), (1, 1), 110)
        assert calls[1] - calls[0] >= 1
    asyncio.run(run())


def test_deadline_during_response_cooldown_prevents_next_attempt():
    async def run():
        clock, calls = FakeClock(), []

        class Slow:
            identity = ('slow',)

            async def query_walking_time(self, origin, destination, deadline):
                calls.append(clock.time())
                await clock.sleep(.25)
                return RouteObservation(destination, 10)

        provider = LimitedProvider(Slow(), RateGate(3, clock=clock.time, sleep=clock.sleep))
        await provider.query_walking_time((0, 0), (1, 1), 110)
        result = await provider.query_walking_time((0, 0), (2, 2), 100.5)
        assert result.reason == 'deadline'
        assert len(calls) == 1
    asyncio.run(run())


@pytest.mark.parametrize('failure', ['cancel', 'exception', 'timeout'])
def test_interrupted_attempt_releases_slot_and_preserves_cooldown(failure):
    async def run():
        clock, calls = FakeClock(), []

        class Unreliable:
            identity = ('unreliable',)

            async def query_walking_time(self, origin, destination, deadline):
                calls.append(clock.time())
                if len(calls) == 1:
                    if failure == 'cancel':
                        raise asyncio.CancelledError
                    if failure == 'exception':
                        raise RuntimeError('offline fixture')
                    return RouteObservation(destination, reason='timeout')
                return RouteObservation(destination, 10)

        gate = RateGate(3, clock=clock.time, sleep=clock.sleep)
        provider = LimitedProvider(Unreliable(), gate)
        try:
            await provider.query_walking_time((0, 0), (1, 1), 110)
        except (asyncio.CancelledError, RuntimeError):
            pass
        assert not gate.attempt_lock.locked()
        await provider.query_walking_time((0, 0), (2, 2), 110)
        assert calls[1] - calls[0] >= 1
    asyncio.run(run())


def test_spacing_uses_precise_clock_without_changing_deadline_epoch():
    async def run():
        precise, coarse = FakeClock(), FakeClock()
        precise.now = 10000.0  # Deliberately different from deadline epoch.

        async def sleep(delay):
            precise.now += delay - .01 if delay > .02 else delay
            coarse.now = 100 + int((precise.now - 10000) / .015625) * .015625

        gate = RateGate(3, clock=coarse.time, spacing_clock=precise.time, sleep=sleep)
        sends = []
        for _ in range(4):
            assert await gate.wait(110)
            sends.append(precise.time())
        assert all(b - a >= 1/3 - 1e-9 for a, b in zip(sends, sends[1:]))
        assert not await gate.wait(coarse.time())
    asyncio.run(run())


def test_scheduler_retry_still_consumes_budget_and_shared_cooldown():
    from life_circle.models import CancelToken, IsochroneRequest
    from life_circle.scheduler import Scheduler

    async def run():
        clock, calls = FakeClock(), []

        class Upstream:
            identity = ('retry',)

            async def query_walking_time(self, origin, destination, deadline):
                calls.append(clock.time())
                return RouteObservation(destination, reason='rate_limit') if len(calls) == 1 else RouteObservation(destination, 10)

        provider = LimitedProvider(Upstream(), RateGate(3, clock=clock.time, sleep=clock.sleep))
        scheduler = Scheduler(IsochroneRequest((121.5, 31.3), 'bd09ll', budget=2, qps=3),
            provider, CancelToken(), clock=clock)
        result = await scheduler.query((121.51, 31.3))
        assert result.duration == 10 and result.attempts == 2
        assert scheduler.stats.requests == scheduler.stats.network_requests == len(calls) == 2
        assert scheduler.stats.retries == 1
        assert calls[1] - calls[0] >= 1
        await scheduler.query((121.52, 31.3))
        assert len(calls) == 2
    asyncio.run(run())
