import asyncio
import threading
import time

import pytest

from app.poi.online import OnlinePlanner, QueryDomain
from life_circle.models import CancelToken
from test_poi_online import ORIGIN


def test_slow_checkpoint_does_not_block_event_loop_or_dispatch_early():
    async def run():
        entered, release = threading.Event(), threading.Event()
        written, sent = [], []
        token = CancelToken()

        def save(state):
            entered.set()
            assert release.wait(5)
            written.append(state)

        async def fetch(sequence, page):
            assert written
            sent.append(page)
            return {'status': 0, 'total': 0, 'results': []}, None

        planner = OnlinePlanner(domain=QueryDomain.circle(300), origin=ORIGIN,
            categories=['pharmacy'], budget=1, source='synthetic', token=token,
            on_checkpoint=save)
        task = asyncio.create_task(planner.run(fetch))
        deadline = time.monotonic() + 5
        try:
            while not entered.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(.005)
            # The loop remains available to status handlers and cancellation.
            assert not written and not sent
            token.cancel()
        finally:
            release.set()
        result = await asyncio.wait_for(task, 5)
        assert result.status == 'cancelled'
        assert not sent and len(written) == 2

    asyncio.run(run())


def test_failed_pre_dispatch_checkpoint_prevents_request():
    sent = []

    def fail(state):
        raise OSError('checkpoint unavailable')

    async def fetch(sequence, page):
        sent.append(page)

    planner = OnlinePlanner(domain=QueryDomain.circle(300), origin=ORIGIN,
        categories=['pharmacy'], budget=1, source='synthetic', on_checkpoint=fail)
    with pytest.raises(OSError, match='checkpoint unavailable'):
        asyncio.run(planner.run(fetch))
    assert sent == []


def test_cancelling_worker_waits_for_in_flight_durable_write():
    async def run():
        entered, release = threading.Event(), threading.Event()
        written = []

        def save(state):
            entered.set()
            assert release.wait(5)
            written.append(state)

        async def fetch(sequence, page):
            pytest.fail('cancelled worker must not send a request')

        planner = OnlinePlanner(domain=QueryDomain.circle(300), origin=ORIGIN,
            categories=['pharmacy'], budget=1, source='synthetic', on_checkpoint=save)
        task = asyncio.create_task(planner.run(fetch))
        try:
            deadline = time.monotonic() + 5
            while not entered.is_set():
                assert time.monotonic() < deadline
                await asyncio.sleep(.005)
            task.cancel()
            await asyncio.sleep(.01)
            assert not task.done() and not written
        finally:
            release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 5)
        assert len(written) == 1

    asyncio.run(run())


def test_checkpoint_mutations_do_not_change_live_pagination_or_rows():
    from test_poi_online import Synthetic

    planner = OnlinePlanner(domain=QueryDomain.circle(300), origin=ORIGIN,
        categories=['pharmacy'], budget=1, source='synthetic')
    asyncio.run(planner.run(Synthetic()))
    frozen = planner.checkpoint()
    first = next(iter(planner.sequences.values()))
    frozen['sequences'][0]['mapping']['category'] = 'changed'
    frozen['sequences'][0]['pagination']['warnings'].append('changed')
    frozen['observations'][0]['row']['location']['lng'] = 0
    assert first.mapping['category'] == 'pharmacy'
    assert 'changed' not in first.pagination.warnings
    assert planner.observations[0].row['location']['lng'] == ORIGIN[0]


def test_shutdown_exits_when_a_stage_consumes_worker_cancellation(tmp_path, monkeypatch):
    from test_checkup_facilities import make_app

    async def run():
        manager = make_app(tmp_path).state.checkups
        entered = asyncio.Event()

        async def stage(task_id):
            entered.set()
            try:
                await asyncio.Event().wait()
            except asyncio.CancelledError:
                # The POI planner converts cancellation into a reported outcome.
                return

        monkeypatch.setattr(manager, '_run', stage)
        manager.queue.put_nowait('shutdown-test')
        manager.start()
        await asyncio.wait_for(entered.wait(), 1)
        await asyncio.wait_for(manager.close(), 1)
        assert manager.worker.done()

    asyncio.run(run())
