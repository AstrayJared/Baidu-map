"""Response-paced slots; existing algorithm limiter stays unchanged."""
from contextlib import asynccontextmanager
import asyncio
import time


class RequestStopped(Exception):
    def __init__(self, reason):
        self.reason = reason
        super().__init__(reason)


async def wait_with_cancel(awaitable, token, *, keep_running=False, on_abandon=None):
    """Wake a token-bound waiter; shared, read-only thread work may finish later."""
    if token is None:
        return await awaitable
    work = asyncio.ensure_future(awaitable)
    cancelled = asyncio.create_task(token.event.wait())
    abandoned = False
    try:
        if not token.cancelled:
            done, _ = await asyncio.wait((work, cancelled), return_when=asyncio.FIRST_COMPLETED)
            # Return a completed acquisition so its caller can release the lock,
            # including when acquisition and cancellation happen in the same turn.
            if work in done:
                return work.result()
        raise RequestStopped("cancelled")
    except BaseException:
        abandoned = True
        raise
    finally:
        cancelled.cancel()
        cancelled.add_done_callback(_consume_result)
        if keep_running:
            # Cancelling to_thread cannot stop the thread. It may be a shared
            # graph load, but must never publish back to the cancelled task.
            work.add_done_callback(_consume_result)
        else:
            if not work.done():
                work.cancel()
                await asyncio.gather(work, return_exceptions=True)
            if abandoned and on_abandon is not None and not work.cancelled() and work.exception() is None:
                on_abandon(work.result())


def _consume_result(work):
    if not work.cancelled():
        work.exception()


@asynccontextmanager
async def request_slot(gate, token, deadline):
    await wait_with_cancel(gate.attempt_lock.acquire(), token,
                           on_abandon=lambda acquired: gate.attempt_lock.release() if acquired else None)
    try:
        if token.cancelled:
            raise RequestStopped("cancelled")
        if not await wait_with_cancel(gate.wait(deadline), token):
            raise RequestStopped("deadline")
        if token.cancelled:
            raise RequestStopped("cancelled")
        if time.monotonic() >= deadline:
            raise RequestStopped("deadline")
        outcome = {"reason": "interrupted"}
        try:
            yield outcome
        finally:
            gate.completed(outcome["reason"])
    finally:
        gate.attempt_lock.release()
