"""Cancellation wakes blocked work without spending unsent calls or losing locks."""
import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from life_circle.models import CancelToken

from app.accessibility.grid import AssessmentCancelled
from app.algorithms.osm_offline.lazy import resolve
from app.analyses import RateGate
from app.checkups.places import PlaceSession
from app.checkups.routes import RouteSession
from app.checkups.store import CheckupStore
from app.checkups import store as store_module
from app.request_control import RequestStopped, request_slot
from app.quota import AttemptCancelled, PLACE, TaskBudget
from test_quota import make_pool
from test_checkup_progress import new_task
from test_checkup_facilities import body, make_app, terminal


@pytest.mark.parametrize('kind', ['pool', 'slot'])
@pytest.mark.parametrize('waiting', ['lock', 'pacing'])
def test_cancel_wakes_waiters_and_releases_only_their_own_lock(tmp_path, kind, waiting):
    async def run():
        token, entered = CancelToken(), asyncio.Event()
        gate = RateGate(1000)
        pool = make_pool(tmp_path, PLACE, gate=gate)
        budget = TaskBudget(isochrone=200, poi=2)
        if waiting == 'lock':
            await gate.attempt_lock.acquire()
        else:
            async def pacing(*args, **kwargs):
                entered.set()
                await asyncio.Event().wait()
            gate.wait = pacing

        async def blocked():
            context = (pool.attempt(time.monotonic() + 60, budget=budget, pool='poi', token=token)
                       if kind == 'pool' else request_slot(gate, token, time.monotonic() + 60))
            with pytest.raises(AttemptCancelled if kind == 'pool' else RequestStopped):
                async with context:
                    pytest.fail('cancelled waiter dispatched')
        task = asyncio.create_task(blocked())
        if waiting == 'pacing':
            await asyncio.wait_for(entered.wait(), 1)
        else:
            await asyncio.sleep(0)
        token.cancel()
        await asyncio.wait_for(task, .5)
        assert budget.remaining('poi') == 2
        assert pool.ledger.spent(PLACE) == 0
        assert gate.attempt_lock.locked() == (waiting == 'lock')
        if waiting == 'lock':
            gate.attempt_lock.release()
        await asyncio.wait_for(gate.attempt_lock.acquire(), .5)
        gate.attempt_lock.release()
    asyncio.run(run())


def test_already_cancelled_and_simultaneous_acquisition_do_not_leak_locks():
    async def run():
        gate, token = RateGate(1000), CancelToken()
        token.cancel()
        with pytest.raises(RequestStopped):
            async with request_slot(gate, token, time.monotonic() + 60):
                pytest.fail('dispatched')
        assert not gate.attempt_lock.locked()
        token = CancelToken()
        original_acquire = gate.attempt_lock.acquire
        async def acquired():
            await original_acquire()
            token.cancel()
            return True
        gate.attempt_lock.acquire = acquired
        with pytest.raises(RequestStopped):
            async with request_slot(gate, token, time.monotonic() + 60):
                pytest.fail('dispatched')
        assert not gate.attempt_lock.locked()
    asyncio.run(run())


def test_worker_cancellation_during_lock_wait_keeps_the_slot_owned_by_its_holder():
    async def run():
        gate, token = RateGate(1000), CancelToken()
        await gate.attempt_lock.acquire()
        async def waiting():
            async with request_slot(gate, token, time.monotonic() + 60):
                pytest.fail('dispatched')
        work = asyncio.create_task(waiting())
        await asyncio.sleep(0)
        work.cancel()
        with pytest.raises(asyncio.CancelledError):
            await work
        assert gate.attempt_lock.locked()
        gate.attempt_lock.release()
        await asyncio.wait_for(gate.attempt_lock.acquire(), .5)
        gate.attempt_lock.release()
    asyncio.run(run())


@pytest.mark.parametrize('kind', ['place', 'route'])
def test_cancel_interrupts_sent_transport_and_keeps_the_reservation(tmp_path, kind):
    async def run():
        token, sent, cleaned = CancelToken(), asyncio.Event(), asyncio.Event()
        pool = make_pool(tmp_path, PLACE)
        budget = TaskBudget(isochrone=200, poi=2, route=2)
        async def transport(*args):
            sent.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        if kind == 'place':
            session = PlaceSession(SimpleNamespace(page=transport), pool, budget=budget,
                                   deadline=time.monotonic() + 60, token=token)
            work = session({'center': (121.5, 31.3), 'query': 'test', 'radius': 100}, 0)
        else:
            session = RouteSession(SimpleNamespace(route=transport), pool, budget=budget,
                                   deadline=time.monotonic() + 60, token=token)
            work = session('facility', (121.5, 31.3), (121.51, 31.3))
        task = asyncio.create_task(work)
        await asyncio.wait_for(sent.wait(), 1)
        token.cancel()
        answer = await asyncio.wait_for(task, .5)
        assert answer == ((None, 'cancelled') if kind == 'place' else None)
        assert cleaned.is_set()
        assert budget.remaining('poi' if kind == 'place' else 'route') == 1
        assert pool.ledger.spent(PLACE) == 1
        assert not pool.gate.attempt_lock.locked()
    asyncio.run(run())


def test_cancel_graph_waiter_leaves_shared_load_available_to_other_waiters():
    release, started = threading.Event(), threading.Event()
    graph = object()
    class SlowGraph:
        loading = ('graph', 1, 10)
        def get(self):
            started.set()
            assert release.wait(5)
            return graph
    async def run():
        token, progress = CancelToken(), []
        first = asyncio.create_task(resolve(SlowGraph(), lambda *args: progress.append(args),
                                            every=.01, token=token))
        second = asyncio.create_task(resolve(SlowGraph(), every=.01))
        try:
            while not started.is_set():
                await asyncio.sleep(.01)
            token.cancel()
            with pytest.raises(RequestStopped):
                await asyncio.wait_for(first, .5)
            count = len(progress)
            await asyncio.sleep(.03)
            assert len(progress) == count
            assert not second.done()
        finally:
            release.set()
        assert await asyncio.wait_for(second, 1) is graph
    asyncio.run(run())


def test_cancelled_report_cannot_publish_a_revision(tmp_path):
    store = CheckupStore(tmp_path / 'checkups')
    store.create_schema()
    task_id = new_task(store)
    store.claim(task_id)
    token = CancelToken()
    token.cancel()
    with pytest.raises(AssessmentCancelled):
        store.publish(task_id, stage='reporting', snapshot={}, result_hash='hash',
                      cancelled=lambda: token.cancelled)
    assert store.get(task_id).revision == 0
    assert store.revisions(task_id) == []


def test_cancel_during_report_serialization_cannot_commit(tmp_path, monkeypatch):
    store = CheckupStore(tmp_path / 'checkups')
    store.create_schema()
    task_id = new_task(store)
    store.claim(task_id)
    token = CancelToken()
    original_dump = store_module.atomic_dump
    def dump_then_cancel(*args, **kwargs):
        original_dump(*args, **kwargs)
        token.cancel()
    monkeypatch.setattr(store_module, 'atomic_dump', dump_then_cancel)
    with pytest.raises(AssessmentCancelled):
        store.publish(task_id, stage='reporting', snapshot={'late': True}, result_hash='late',
                      cancelled=lambda: token.cancelled)
    assert store.get(task_id).revision == 0
    assert store.revisions(task_id) == []
    monkeypatch.setattr(store_module, 'atomic_dump', original_dump)
    store.publish(task_id, stage='reporting', snapshot={'current': True}, result_hash='current')
    assert store.revision(task_id)['snapshot'] == {'current': True}


def test_cancel_during_report_finishes_before_thread_and_ignores_late_report(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    manager = app.state.checkups
    started, release, exited = threading.Event(), threading.Event(), threading.Event()
    original = manager._publish_reporting
    def reporting(*args, **kwargs):
        started.set()
        try:
            assert release.wait(10)
            return original(*args, **kwargs)
        finally:
            exited.set()
    monkeypatch.setattr(manager, '_publish_reporting', reporting)
    with TestClient(app) as client:
        task_id = client.post('/api/v2/checkups', json=body()).json()['taskId']
        try:
            assert started.wait(30)
            revision = client.get(f'/api/v2/checkups/{task_id}').json()['revision']
            before = time.monotonic()
            client.post(f'/api/v2/checkups/{task_id}/cancel')
            view = terminal(client, task_id)
            assert view['status'] == 'cancelled'
            assert time.monotonic() - before < 1.5
            assert not exited.is_set()
        finally:
            release.set()
        assert exited.wait(5)
        view = client.get(f'/api/v2/checkups/{task_id}').json()
        assert view['status'] == 'cancelled'
        assert view['revision'] == revision


def test_cancel_engine_releases_worker_for_the_next_queued_task(tmp_path, monkeypatch):
    app = make_app(tmp_path)
    engine = app.state.checkups.registry.get('baidu_e82')
    original = engine.compute
    started, cleaned = threading.Event(), threading.Event()
    calls = 0
    async def compute(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                cleaned.set()
        return await original(*args, **kwargs)
    monkeypatch.setattr(engine, 'compute', compute)
    with TestClient(app) as client:
        first = client.post('/api/v2/checkups', json=body()).json()['taskId']
        assert started.wait(5)
        second = client.post('/api/v2/checkups', json=body(clientRequestId='next-task')).json()['taskId']
        before = time.monotonic()
        client.post(f'/api/v2/checkups/{first}/cancel')
        assert terminal(client, first, timeout=2)['status'] == 'cancelled'
        assert time.monotonic() - before < 1.5
        assert cleaned.is_set()
        assert terminal(client, second, timeout=30)['status'] == 'completed'
