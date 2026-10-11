"""A running task reports what it is doing, on one clock, while it is doing it.

The status endpoint is what a client polls for the whole life of a task. These
cases pin down what it may be trusted for: the elapsed time is measured on the
same wall clock the store writes; a stage that lasts minutes still publishes
the step it is in and the counts that have really moved; a stretch with nothing
new stays silent rather than being papered over; and the endpoint keeps
answering while the worker is blocked inside a stage. Everything runs offline.
"""
import asyncio
import sqlite3
import threading
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.checkups import manager as manager_module
from app.checkups import store as store_module
from app.checkups.progress import STEPS, StepReporter, category_label, progress_payload
from app.checkups.places import POI_POOL
from app.checkups.store import CheckupStore

from test_checkup_facilities import SyntheticPlaces, at_origin, body, make_app, terminal

E82_STEPS = ("initializing", "expanding", "exploring", "refining", "reconstructing", "completed")


class Clock:
    def __init__(self, now=1_000.0):
        self.now = now

    def __call__(self):
        return self.now


def new_task(store, task_id="task-1"):
    store.create(task_id=task_id, client_request_id=f"request-{task_id}", engine="baidu_e82",
                 fingerprint="fingerprint", payload=body(clientRequestId=f"request-{task_id}"), budget=200)
    return task_id


# -- the store ---------------------------------------------------------------

def _tasks_table_without_the_later_columns() -> str:
    """``SCHEMA`` 的 tasks 表，但去掉所有"后来才加的列"。

    只在 tasks 这一块里替换：``ADDED_COLUMNS`` 说的就是这张表。早先的写法是在整份 SCHEMA
    上做字符串替换，于是 ``session_id`` 这个既属于新 ``session_tabs`` 表、又是 tasks 的
    后加列的名字，被从 **session_tabs** 里删掉了 —— 造出来的"老库"于是带着一条指向不存在
    的列的索引，而那是这段模拟自己的错，不是迁移的错。
    """
    schema = store_module.SCHEMA
    start = schema.index("CREATE TABLE IF NOT EXISTS tasks (")
    end = schema.index(");", start) + len(");")
    block = schema[start:end]
    for name, declaration in store_module.ADDED_COLUMNS:
        block = block.replace(f",\n    {name} {declaration}", "")
    return schema[:start] + block + schema[end:]


def test_a_store_from_before_the_progress_columns_is_migrated_in_place(tmp_path):
    store = CheckupStore(tmp_path / "checkups")
    old = _tasks_table_without_the_later_columns()
    assert "activity_at" not in old
    with sqlite3.connect(store.path) as connection:
        connection.executescript(old)
        connection.execute(
            "INSERT INTO tasks (task_id, client_request_id, engine, fingerprint, payload, status,"
            " stage, revision, budget, created_at, updated_at, started_at)"
            " VALUES ('old', 'old-request', 'baidu_e82', 'f', '{}', 'running', 'poi', 1, 200,"
            " 10, 10, 10)")
    store.create_schema()
    store.create_schema()  # idempotent: the second pass finds every column present
    record = store.get("old")
    # An old row has no record of these; it reads as "not recorded", not as now.
    assert (record.stage_started_at, record.activity_at, record.progress) == (None, None, None)
    assert record.status == "running" and record.stage == "poi"
    # And it can be written like any other row.
    updated = store.update("old", progress=progress_payload("places", since=11.0, count=2))
    assert updated.progress["count"] == 2 and updated.activity_at is not None


def test_a_stage_change_restarts_its_clock_and_clears_its_step(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=clock))
    store = CheckupStore(tmp_path / "checkups")
    store.create_schema()
    task_id = new_task(store)
    assert store.claim(task_id)
    claimed = store.get(task_id)
    assert (claimed.started_at, claimed.stage_started_at, claimed.activity_at) == (1000.0,) * 3
    assert claimed.stage == "isochrone" and claimed.progress is None

    clock.now = 1010.0
    step = progress_payload("expanding", since=1010.0, count=5, limit=200)
    record = store.update(task_id, progress=step, requests=5, network_requests=0)
    assert record.progress == step and record.activity_at == 1010.0
    assert record.stage_started_at == 1000.0

    # Writing the stage the task is already in is not a new stage.
    clock.now = 1020.0
    record = store.update(task_id, stage="isochrone")
    assert record.stage_started_at == 1000.0 and record.progress == step

    # A new stage starts its own clock and has no step until the worker names one.
    clock.now = 1030.0
    record = store.update(task_id, stage="poi")
    assert record.stage_started_at == 1030.0 and record.progress is None
    assert record.activity_at == 1030.0

    # A publish of the current stage keeps the stage clock and counts as activity.
    clock.now = 1040.0
    store.publish(task_id, stage="poi", snapshot={"any": 1}, result_hash="h")
    record = store.get(task_id)
    assert record.stage_started_at == 1030.0 and record.activity_at == 1040.0


def test_a_cancel_request_is_not_server_activity(tmp_path, monkeypatch):
    clock = Clock()
    monkeypatch.setattr(store_module, "time", SimpleNamespace(time=clock))
    store = CheckupStore(tmp_path / "checkups")
    store.create_schema()
    task_id = new_task(store)
    store.claim(task_id)
    clock.now = 1050.0
    record = store.update(task_id, cancel_requested=True, activity=False)
    record = store.update(task_id, status="cancelling", activity=False)
    assert record.cancel_requested and record.status == "cancelling"
    assert record.activity_at == 1000.0
    assert record.updated_at == 1050.0


# -- the reporter ------------------------------------------------------------

def test_a_step_keeps_its_start_across_counter_ticks_and_only_cell_ticks_are_throttled():
    clock, written = Clock(), []
    report = StepReporter(lambda **fields: written.append(fields), interval=1.0, clock=clock)

    report("graph")
    assert written[-1] == {"progress": {"step": "graph", "label": STEPS["graph"][0],
                                        "count": None, "limit": None, "unit": None,
                                        "since": 1000.0}}
    # An engine's snapshot carries its attempts into a step that counts none.
    report("guidance", count=0, limit=400, requests=0, network_requests=0)
    assert written[-1]["progress"]["count"] is None and written[-1]["progress"]["limit"] is None
    assert written[-1]["requests"] == 0
    clock.now = 1005.0
    report("sampling", count=0, limit=400, requests=0, network_requests=0)
    clock.now = 1005.1
    report("sampling", count=1, limit=400, requests=1, network_requests=1)
    # Every sample is written at once, and the step still began when it began.
    assert written[-1]["progress"]["since"] == 1005.0
    assert written[-1]["progress"]["count"] == 1 and written[-1]["progress"]["unit"] == "次采样"
    assert written[-1]["network_requests"] == 1

    # Per-cell ticks are throttled; the first write of each category is not.
    clock.now = 1010.0
    medical = dict(key="medical", label=category_label("medical", 1, 2))
    assert report("category", count=0, **medical)
    assert not report("category", count=40, throttle=True, **medical)
    clock.now = 1011.0
    assert report("category", count=90, throttle=True, **medical)
    assert written[-1]["progress"]["label"] == "评估服务覆盖 · 医疗健康（第 1/2 类）"
    assert written[-1]["progress"]["since"] == 1010.0

    # The next category is a new step even though the step code is the same.
    clock.now = 1011.2
    assert report("category", count=0, key="education", label=category_label("education", 2, 2))
    assert written[-1]["progress"]["since"] == 1011.2
    # No percentage anywhere in what is stored.
    assert all(set(item["progress"]) == {"step", "label", "count", "limit", "unit", "since"}
               for item in written)


# -- the view ----------------------------------------------------------------

def test_the_elapsed_time_of_a_running_task_is_measured_on_the_wall_clock(tmp_path):
    app = make_app(tmp_path)
    store = app.state.checkups.store
    store.create_schema()
    task_id = new_task(store)
    store.claim(task_id)
    started = time.time() - 100.0
    store.update(task_id, started_at=started)
    view = app.state.checkups.view(store.get(task_id)).model_dump(mode="json", by_alias=True)
    # The worker writes wall-clock times; a monotonic "now" read against them
    # clamps to zero on most machines. This is the case that used to read 0 s.
    assert 99.0 <= view["elapsedSeconds"] <= 110.0
    assert abs(view["serverTime"] - time.time()) < 5.0
    assert view["startedAt"] == started and view["finishedAt"] is None
    assert view["stageStartedAt"] is not None and view["lastActivityAt"] is not None
    assert view["progress"] is None


# -- a task held inside a stage ----------------------------------------------

class HeldPlaces:
    """Places answered at once, except one page that waits until released.

    The hold is an ``await``, as a slow upstream would be: the event loop is free,
    and nothing about the task changes until the page returns.
    """

    identity, api_version, network = "synthetic:held-places", "3.0", False

    def __init__(self, respond, *, hold_at=2):
        self.respond, self.hold_at, self.sent = respond, hold_at, []
        self.held, self.release = threading.Event(), threading.Event()

    def session(self, pool, *, budget, deadline):
        transport = self

        async def page(sequence, number):
            async with pool.attempt(deadline, budget=budget, pool=POI_POOL) as attempt:
                if len(transport.sent) + 1 == transport.hold_at:
                    transport.held.set()
                    while not transport.release.is_set():
                        await asyncio.sleep(0.01)
                transport.sent.append((sequence["sequenceId"], number))
                payload, reason = transport.respond(sequence, number)
                attempt.outcome(reason)
            return payload, reason
        return page


def status(client, task_id):
    started = time.perf_counter()
    response = client.get(f"/api/v2/checkups/{task_id}")
    assert response.status_code == 200
    return response.json(), time.perf_counter() - started


def test_a_task_held_inside_a_stage_keeps_answering_and_reports_only_what_happened(tmp_path):
    places = HeldPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id = client.post("/api/v2/checkups", json=body()).json()["taskId"]
        assert places.held.wait(60), "the facility stage never reached its second page"
        first, _ = status(client, task_id)
        assert first["status"] == "running" and first["stage"] == "poi"
        # One page came back before the hold; the second is reserved but not answered.
        progress = first["progress"]
        assert progress["step"] == "places" and progress["label"] == "检索设施 · 购物消费"
        assert progress["count"] == 1 and progress["unit"] == "次请求"
        assert progress["limit"] >= 2
        assert first["networkRequests"] == first["requests"] == 2
        assert first["stageStartedAt"] <= progress["since"] <= first["lastActivityAt"]

        polls = []
        for _ in range(8):
            time.sleep(0.15)
            polls.append(status(client, task_id))
        views = [view for view, _ in polls]
        # The endpoint answers promptly while the worker is blocked in the stage.
        assert max(seconds for _, seconds in polls) < 1.0
        # Time moves on every answer ...
        elapsed = [view["elapsedSeconds"] for view in views]
        assert elapsed == sorted(elapsed) and elapsed[-1] - first["elapsedSeconds"] >= 1.0
        assert views[-1]["serverTime"] > first["serverTime"]
        # ... and nothing else does: no invented count, no new activity, same step.
        for view in views:
            assert view["progress"] == progress
            assert view["lastActivityAt"] == first["lastActivityAt"]
            assert view["stageStartedAt"] == first["stageStartedAt"]
            assert view["revision"] == first["revision"]
            assert view["networkRequests"] == 2
        silence = views[-1]["serverTime"] - views[-1]["lastActivityAt"]
        assert silence >= 1.0

        places.release.set()
        done = terminal(client, task_id)
        assert done["status"] == "completed", done
        # The live counter never ran ahead of the stage's own final account.
        assert done["networkRequests"] >= 2
        assert done["progress"] is None and done["stage"] == "ready"
        assert done["finishedAt"] >= done["startedAt"]


def test_the_assessment_thread_reports_its_steps_while_the_endpoint_answers(tmp_path, monkeypatch):
    real = manager_module.assess_accessibility
    held, release = threading.Event(), threading.Event()

    def slow_assessment(**kwargs):
        progress = kwargs["progress"]
        progress("domain")
        progress("category", major="medical", index=1, total=2, cells=0)
        time.sleep(1.1)  # past the reporter's throttle, so the next tick is written
        progress("category", major="medical", index=1, total=2, cells=7)
        held.set()
        release.wait(30)
        return real(**kwargs)

    monkeypatch.setattr(manager_module, "assess_accessibility", slow_assessment)
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        task_id = client.post("/api/v2/checkups", json=body()).json()["taskId"]
        assert held.wait(60), "the assessment never started"
        view, seconds = status(client, task_id)
        assert seconds < 1.0
        assert view["stage"] == "accessibility"
        progress = view["progress"]
        assert progress["step"] == "category" and progress["count"] == 7
        assert progress["unit"] == "格" and progress["limit"] is None
        assert progress["label"] == "评估服务覆盖 · 医疗健康（第 1/2 类）"
        time.sleep(0.5)
        later, seconds = status(client, task_id)
        assert seconds < 1.0 and later["progress"] == progress
        assert later["elapsedSeconds"] > view["elapsedSeconds"]
        release.set()
        assert terminal(client, task_id)["status"] == "completed"


def test_the_first_load_of_the_graph_is_counted_edge_by_edge(tmp_path, monkeypatch):
    from app.algorithms.osm_offline import engine as offline_engine

    real = offline_engine.OsmOfflineEngine.load.__func__
    reached, onwards, again, release = (threading.Event() for _ in range(4))

    def slow_load(cls, settings, progress=None):
        progress("graph_read")
        progress("graph_check", 20_000, 90_000)
        reached.set()
        onwards.wait(30)
        progress("graph_check", 40_000, 90_000)
        again.set()
        release.wait(30)
        return real(cls, settings, progress)

    monkeypatch.setattr(offline_engine.OsmOfflineEngine, "load", classmethod(slow_load))
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))

    def until(client, task_id, check):
        for _ in range(100):
            view, seconds = status(client, task_id)
            assert seconds < 1.0
            if check(view["progress"] or {}):
                return view
            time.sleep(0.05)
        raise AssertionError(view)

    with TestClient(app) as client:
        created = client.post("/api/v2/checkups", json=body(engine="osm_hybrid",
                                                            isochrone={"budget": 200}))
        task_id = created.json()["taskId"]
        assert reached.wait(60), "the graph load never started"
        first = until(client, task_id, lambda p: p.get("count") == 20_000)
        progress = first["progress"]
        assert progress["step"] == "graph_check" and progress["limit"] == 90_000
        assert progress["unit"] == "条边" and progress["label"].startswith("载入 OSM 步行路网")
        # Loading is not an attempt against the tier.
        assert first["requests"] == first["networkRequests"] == 0
        onwards.set()
        assert again.wait(30)
        later = until(client, task_id, lambda p: p.get("count") == 40_000)
        # The same step, the same start; the count moved, and with it the activity.
        assert later["progress"]["since"] == progress["since"]
        assert later["lastActivityAt"] > first["lastActivityAt"]
        release.set()
        assert terminal(client, task_id)["status"] == "completed"


def recorded_steps(app, monkeypatch, payloads=None):
    """Every step name the worker writes, in order, without consecutive repeats."""
    store, steps = app.state.checkups.store, []
    original = store.update

    def update(task_id, **fields):
        step = (fields.get("progress") or {}).get("step")
        if step is not None and (not steps or steps[-1] != step):
            steps.append(step)
        if step is not None and payloads is not None:
            payloads.append(fields["progress"])
        return original(task_id, **fields)
    monkeypatch.setattr(store, "update", update)
    return steps


def test_the_hybrid_engine_names_its_preparation_before_its_first_sample(tmp_path, monkeypatch):
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    payloads = []
    steps = recorded_steps(app, monkeypatch, payloads)
    with TestClient(app) as client:
        created = client.post("/api/v2/checkups", json=body(engine="osm_hybrid",
                                                            isochrone={"budget": 200}))
        view = terminal(client, created.json()["taskId"])
        assert view["status"] == "completed", view
    # Graph, guidance and obstacles take place before any sample is asked for.
    assert steps[:4] == ["graph", "guidance", "obstacles", "sampling"]
    # ... and none of them has anything to count: no "0 attempts" that never moves.
    assert all(p["count"] is None and p["unit"] is None for p in payloads
               if p["step"] in ("graph", "guidance", "obstacles"))
    assert steps.index("places") > steps.index("sampling")
    assert steps[-1] == "report"
    assert all(step in STEPS for step in steps)


def test_the_e82_engine_names_its_own_sub_stages(tmp_path, monkeypatch):
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    steps = recorded_steps(app, monkeypatch)
    with TestClient(app) as client:
        view = terminal(client, client.post("/api/v2/checkups", json=body()).json()["taskId"])
        assert view["status"] == "completed", view
    boundary = steps[:steps.index("places")]
    assert boundary and all(step in E82_STEPS for step in boundary)
    assert steps[-1] == "report"
    assert all(step in STEPS for step in steps)
