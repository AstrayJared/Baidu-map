"""Thread-safe lazy holder for the optional city-wide OSM graph."""
from __future__ import annotations

import threading


class LazyOsmOfflineEngine:
    def __init__(self, settings):
        self.settings = settings
        self.state = "unloaded"
        self._engine = None
        self._lock = threading.Lock()

    def get(self):
        if self._engine is not None:
            return self._engine
        with self._lock:
            if self._engine is not None:
                return self._engine
            self.state = "loading"
            try:
                from .engine import OsmOfflineEngine
                engine = OsmOfflineEngine.load(self.settings)
                self._engine = engine
                self.state = "ready" if engine.store is not None else "unavailable"
                return engine
            except Exception:
                self.state = "unavailable"
                raise

    @property
    def store(self):
        return self.get().store

    @property
    def coverage(self):
        return self.get().coverage

    def compute(self, request):
        return self.get().compute(request)
