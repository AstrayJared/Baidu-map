"""Shutdown stops optional graph preload before waiting for its full rebuild."""
import threading
import time

import pytest
from fastapi.testclient import TestClient

from app import bulk_load
from app.algorithms.osm_offline.engine import OsmOfflineEngine
from app.algorithms.osm_offline.lazy import GraphLoadCancelled, LazyOsmOfflineEngine
from test_checkup_v2 import make_app


def test_json_parser_has_shutdown_checkpoints():
    calls = 0
    def checkpoint():
        nonlocal calls
        calls += 1
        if calls == 2:
            raise GraphLoadCancelled()
    with pytest.raises(GraphLoadCancelled):
        bulk_load.loads('[' + ','.join('{}' for _ in range(5000)) + ']', checkpoint=checkpoint)
    assert calls == 2


def test_shutdown_stops_an_in_progress_preload(tmp_path, monkeypatch):
    cache = tmp_path / 'graph.cache'
    cache.touch()
    entered, exited = threading.Event(), threading.Event()
    def loading(cls, settings, progress=None):
        entered.set()
        try:
            while True:
                progress('graph_build', 1, 100)
                time.sleep(.01)
        finally:
            exited.set()
    monkeypatch.setattr(OsmOfflineEngine, 'load', classmethod(loading))
    app = make_app(tmp_path, osm_data_version='test', osm_graph_cache_path=cache)
    with TestClient(app) as client:
        assert entered.wait(5)
        assert client.get('/health').status_code == 200
        started = time.monotonic()
    assert time.monotonic() - started < 1
    assert exited.is_set()
    assert app.state.osm_offline.state == 'unloaded'


def test_stopped_holder_cannot_start_another_load():
    holder = LazyOsmOfflineEngine(None)
    holder.stop()
    with pytest.raises(GraphLoadCancelled):
        holder.get()
