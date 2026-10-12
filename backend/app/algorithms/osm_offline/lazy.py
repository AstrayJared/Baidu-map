"""Thread-safe lazy holder for the optional city-wide OSM graph."""
from __future__ import annotations

import asyncio
import threading

from ... import bulk_load
from ...request_control import RequestStopped, wait_with_cancel


class GraphLoadCancelled(BaseException):
    """Process shutdown interrupted an optional graph load, not corrupt data."""


class LazyOsmOfflineEngine:
    def __init__(self, settings):
        self.settings = settings
        self.state = "unloaded"
        self._engine = None
        self._lock = threading.Lock()
        self._stopping = threading.Event()
        #: How far a load in progress has got, ``(step, done, total)``; None
        #: otherwise. Whoever waits for the graph reads it (see ``resolve``).
        self.loading = None

    def _report(self, step, done=None, total=None):
        if self._stopping.is_set():
            raise GraphLoadCancelled()
        self.loading = (step, done, total)

    def stop(self):
        """Wake the loading thread at its next parse or graph checkpoint."""
        self._stopping.set()

    def begin_preload(self):
        """Mark a configured graph as loading before its worker is scheduled."""
        with self._lock:
            if self._engine is not None or self.state == "loading":
                return False
            self.state = "loading"
            return True

    def get(self):
        if self._stopping.is_set():
            raise GraphLoadCancelled()
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._stopping.is_set():
                raise GraphLoadCancelled()
            if self._engine is not None:
                return self._engine
            self.state = "loading"
            try:
                from .engine import OsmOfflineEngine
                # Millions of objects, for the life of the process.
                with bulk_load.long_lived():
                    engine = OsmOfflineEngine.load(self.settings, progress=self._report)
                self._engine = engine
                self.state = "ready" if engine.store is not None else "unavailable"
                return engine
            except GraphLoadCancelled:
                self.state = 'unloaded'
                raise
            except Exception:
                self.state = "unavailable"
                raise
            finally:
                self.loading = None

    @property
    def store(self):
        return self.get().store

    @property
    def coverage(self):
        return self.get().coverage

    def compute(self, request):
        return self.get().compute(request)


async def resolve(offline, report=None, *, every=1.0, token=None):
    """``offline.get()`` off the event loop, telling ``report`` how a load is going.

    The first use of the city graph takes minutes, and the load may be another
    caller's (the one before, a warm-up): every waiter reads the same account,
    ``report(step, done, total)``, once per ``every`` seconds when it has moved.
    """
    if token is not None and token.cancelled:
        raise RequestStopped('cancelled')
    pending = asyncio.ensure_future(asyncio.to_thread(offline.get))
    shown = None
    try:
        while not (await wait_with_cancel(
                asyncio.wait({pending}, timeout=every), token))[0]:
            if token is not None and token.cancelled:
                raise RequestStopped('cancelled')
            now = getattr(offline, "loading", None)
            if report is not None and now is not None and now != shown:
                shown = now
                report(*now)
    finally:
        # The city graph belongs to the process. A cancelled waiter leaves its
        # load running, but stops progress callbacks for this task immediately.
        pending.add_done_callback(lambda task: None if task.cancelled() else task.exception())
    if token is not None and token.cancelled:
        raise RequestStopped('cancelled')
    return pending.result()
