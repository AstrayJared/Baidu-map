"""M2: the facility stage over the computed boundary, end to end and offline.

Both engines run their own boundary first and then exactly one facility
retrieval over it. The transport here answers from a Python callable instead of
HTTP, but it reserves every attempt through the same pool the production session
uses, so pacing, the task budget and the daily ledger are exercised for real and
no request in this file leaves the machine.

What is under test is §4.4's and §3.4's semantics: the counting region is the
boundary and not the search range; a run that retrieved nothing is not a
directory with nothing in it; a refusal is named rather than numbered; a page
served from the cache costs no attempt and keeps its original data time; a
cancelled stage keeps what it retrieved; and a failure is never a zero.
"""
import asyncio
import pytest
import contextlib
import json
import math
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.algorithms.baidu_e82 import EndpointAnalyticProvider
from app.algorithms.hybrid_isochrone.models import Evidence, Validity
from app.catalog import default_analysis_majors, poi_keys
from app.checkups.facilities import collect_facilities
from app.checkups.facility_stage import query_domain
from app.checkups.places import POI_POOL, declared_identity
from app.checkups.routes import ROUTE_POOL
from app.config import Settings
from app.checkups.models import (DEFAULT_POI_REQUESTS, QUERY_PADDING_M, CheckupRequest,
                                 FacilityExtensionRequest)
from app.engines import EngineContext
from app.main import create_app
from app.poi import plan as poi_plan
from app.poi.planner import RULES
from app.quota import PLACE, BudgetExhausted, DeadlineReached
from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken, RouteObservation

ORIGIN = (121.513925, 31.313079)
CENTER = {"lng": ORIGIN[0], "lat": ORIGIN[1]}
SECRET = "checkup-place-secret"
#: The route transport's identity, as the verification and detail evidence report it.
SYNTHETIC_ROUTES = "synthetic:checkup-routes"

# 首轮页数 = 几何确定后的查询分块数（这些夹具的圈面都覆盖 4 块）× 核心三类的检索小类数。
# 断言写在这条关系上，词典增删小类时数字跟着走，而不是把某一次运行的快照钉死。
CORE_MINORS = len(poi_keys(list(default_analysis_majors())))
CORE_PAGES = 4 * CORE_MINORS
# 每个小类各回一条落在中心的记录；十四个小类里 combined_school 与 training 的样本标签
# 不判进本类，所以被接收的是十二处 —— 这三条数字来自词典，不是来自某次运行。
CORE_ACCEPTED = {"shopping": 3, "medical": 5, "education": 4}
CORE_ACCEPTED_TOTAL = sum(CORE_ACCEPTED.values())


class FastGate:
    """No transport pacing: quota pacing is tested in test_rate_gate."""

    interval = 0
    qps = 10000

    def __init__(self):
        self.attempt_lock = asyncio.Lock()

    async def wait(self, deadline, *, cost=1):
        return True

    def completed(self, reason, *, cost=1):
        pass


class OfflineHybrid:
    """Local walking-time evidence; never a network provider."""

    network = False
    identity = ("offline-checkup-hybrid",)

    def __init__(self, projection):
        self.projection = projection

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return None

    async def query_walking_time(self, origin, destination, deadline):
        a, b = self.projection.origin(origin), self.projection.origin(destination)
        duration = math.dist(a, b) / 1.2
        return Evidence(validity=Validity.REACHABLE if duration <= 900 else Validity.UNREACHABLE,
                        reason=None, duration=duration, returned_origin=origin,
                        returned_destination=destination, origin_offset_m=0,
                        destination_offset_m=0)


def analytic(origin):
    return EndpointAnalyticProvider(origin, lambda x, y: math.hypot(x, y) / 1.2)


# -- the synthetic transport -------------------------------------------------

def supported_tag(category):
    """One tag the dictionary classifies into this category, taken from it."""
    return sorted(RULES["supportedTags"][category])[0]


def offset(meters, *, east=1.0, north=0.0):
    """A geographic point that many metres from the request centre."""
    return LocalProjection(ORIGIN).to_geographic((meters * east, meters * north))


class SyntheticPlaces:
    """A ``PlaceTransport`` answered by a Python callable instead of HTTP.

    Every page it sends holds the pool's single in-flight slot behind a reserved
    attempt, exactly as the production session does, and ``sent`` is the one
    record of what a run really asked for.
    """

    identity, api_version, network = "synthetic:checkup-tests", "3.0", False

    def __init__(self, respond, *, delay=0.0):
        self.respond, self.delay, self.sent = respond, delay, []

    def session(self, pool, *, budget, deadline):
        return SyntheticPlaceSession(self, pool, budget=budget, deadline=deadline)


class SyntheticPlaceSession:
    def __init__(self, transport, pool, *, budget, deadline):
        self.transport, self.pool = transport, pool
        self.budget, self.deadline = budget, deadline

    async def __call__(self, sequence, page):
        async with self.pool.attempt(self.deadline, budget=self.budget, pool=POI_POOL) as attempt:
            if self.transport.delay:
                await asyncio.sleep(self.transport.delay)
            self.transport.sent.append((sequence["sequenceId"], page))
            payload, reason = self.transport.respond(sequence, page)
            attempt.outcome(reason)
        return payload, reason


class SyntheticRoutes:
    """A walking-route transport answered by a Python callable instead of HTTP.

    Same shape as ``SyntheticPlaces``: one reservation per attempt through the
    task's ``route`` bucket, so the budget, the pool and the pacing are the real
    ones and no request in this file leaves the machine.
    """

    identity, api_version, network = "synthetic:checkup-routes", "directionlite/v1/walking", False

    def __init__(self, respond):
        self.respond, self.sent = respond, []

    def session(self, pool, *, budget, deadline):
        return SyntheticRouteSession(self, pool, budget=budget, deadline=deadline)


class SyntheticRouteSession:
    def __init__(self, transport, pool, *, budget, deadline):
        self.transport, self.pool = transport, pool
        self.budget, self.deadline = budget, deadline
        self.attempts, self.stop_reason = 0, None

    @property
    def identity(self):
        return self.transport.identity

    async def __call__(self, facility_id, origin, destination, *, pool=ROUTE_POOL):
        try:
            async with self.pool.attempt(self.deadline, budget=self.budget, pool=pool) as attempt:
                self.attempts += 1
                self.transport.sent.append((facility_id, origin, destination))
                observation = self.transport.respond(origin, destination, facility_id)
                attempt.outcome(observation.reason)
        except BudgetExhausted:
            # 桶用尽和到点都不是"这条路走不通"，而是"这次没有结论"。生产会话在这里
            # 返回 None，替身也必须一样，否则测试会因为一个生产代码接住的异常而变红。
            self.stop_reason = 'task_budget_exhausted'
            return None
        except DeadlineReached:
            self.stop_reason = 'deadline'
            return None
        return observation


def straight_routes(*, detour=1.15, speed=1.2):
    """A responder whose route is the straight line, times a detour factor.

    The returned endpoints are the requested ones, so the strict POI mapping is
    the one under test here: this is the "the endpoints really did line up" case.
    """
    def respond(route_origin, destination, facility_id):
        # 上游收到的是归一化之后的坐标，回来的端点就是它 —— 所以这里回显的也是归一化
        # 之后的目标点。回显原始浮点数会让严格映射判定成"端点有偏移"（归一化把坐标截到
        # 6 位小数，折合江南一带约 0.08 米，超过 1e-5 米的阈值），那是测试替身失真。
        destination = normalize(destination)
        projection = LocalProjection(route_origin)
        distance = math.dist(projection.to_local(route_origin),
                             projection.to_local(destination)) * detour
        duration = distance / speed
        return RouteObservation(destination, duration, reason=None,
                                observed_duration=duration, endpoint_verified=True,
                                route_origin=route_origin, route_destination=destination,
                                distance_m=distance, route_path=[route_origin, destination],
                                origin_offset_m=0, destination_offset_m=0)
    return respond


def offset_routes(*, offset_m=80.0, detour=1.15):
    """A responder whose route ends 80 m away from the requested facility.

    That is what a road-snapped endpoint looks like, and §6.3 is explicit about
    what must happen next: the route stays route-endpoint evidence, it is never
    promoted to strict POI verification.
    """
    def respond(route_origin, destination, facility_id):
        projection = LocalProjection(route_origin)
        # 端点确实停在离目标 80 米的地方，而不是停在一个碰巧不等的位置：这样"偏移量"这个
        # 数字本身也被测到，而不只是"不相等"这件事。
        x, y = projection.to_local(normalize(destination))
        end = projection.to_geographic((x + offset_m, y))
        distance = math.dist(projection.to_local(route_origin),
                             projection.to_local(destination)) * detour
        duration = distance / 1.2
        return RouteObservation(destination, duration, reason=None,
                                observed_duration=duration, endpoint_verified=True,
                                route_origin=route_origin, route_destination=end,
                                distance_m=distance, route_path=[route_origin, end],
                                origin_offset_m=0.0, destination_offset_m=offset_m)
    return respond


def row(sequence, index, *, at, uid=None):
    """One provider row: a facility of the query's own category.

    The name and address come from that category's own vocabulary, so a row
    classifies as the category its query asked for and two categories never
    collide on the duplicate rule.
    """
    word = supported_tag(sequence["category"])
    return {"uid": uid or f"{sequence['category']}-{index}", "name": f"{word}样本",
            "address": f"{word}路{index}号", "location": {"lng": at[0], "lat": at[1]},
            "detail_info": {"classified_poi_tag": word, "parent_id": None}}


def every_page(points):
    """A responder that answers every page with one row per point."""
    def respond(sequence, page):
        results = [row(sequence, index, at=point) for index, point in enumerate(points)]
        return {"status": 0, "total": len(results), "result_type": "poi_type",
                "results": results}, None
    return respond


def at_origin(meters=0.0):
    """A responder answering every page with facilities near the request centre."""
    return every_page([offset(meters) if meters else ORIGIN])


def failing(reason="upstream_error"):
    return lambda sequence, page: (None, reason)


def two_page_rows(sequence, page):
    """每页 20 条、两家一共 40 条：一次检索要翻到第二页才算查完。

    这样"首轮"与"查完"就是两件事：56 个首轮页之外还有 56 个第二页，正好用来测
    "额度花在缺页上"和"60 次预算装不装得下一整轮"。
    """
    return {"status": 0, "total": 40, "result_type": "poi_type",
            "results": [row(sequence, index + 20 * page, at=ORIGIN) for index in range(20)]}, None


# -- scaffolding -------------------------------------------------------------

def make_app(tmp_path, places=None, routes=None, **overrides):
    """An offline checkup app. ``places=None`` is a deployment without a key.

    ``routes`` is the verification stage's transport; the default answers every
    route with the straight line, so a task that retrieves facilities also
    verifies them without reaching the network. ``overrides`` wins over the
    scratch defaults below: a test that needs a real obstacle layer, a different
    data version or an extra proxy must not have to rebuild the whole fixture.
    """
    routes = SyntheticRoutes(straight_routes()) if routes is None else routes
    configured = dict(
        baidu_map_ak=SECRET if places is not None else "",
        analysis_provider="synthetic", baidu_place_qps=10000, baidu_direction_qps=10000,
        # The fixture's tier must not switch with the calendar mid-suite, and its day
        # ceiling is pinned for the same reason: this file asserts retrieval behaviour,
        # so a number that moves whenever the deployment default moves would fail here
        # for a reason that is not this file's subject. The sections that need a
        # different ceiling pass it explicitly (``FALLBACK_SHAPE``).
        baidu_quota_fallback_at="2099-01-01T00:00:00+08:00",
        baidu_place_daily_budget=1600,
        checkup_dir=tmp_path / "checkups", hybrid_ledger_dir=tmp_path / "ledgers",
        quota_ledger_path=tmp_path / "quota.sqlite3",
        hybrid_obstacle_path=Path("missing-checkup-obstacles"),
        hybrid_risk_path=Path("missing-checkup-risks"))
    configured.update(overrides)
    settings = Settings(_env_file=None, **configured)
    app = create_app(settings, provider_factory=analytic,
                     hybrid_provider_factory=lambda projection, config: OfflineHybrid(projection),
                     place_factory=None if places is None else (lambda _settings: places),
                     route_factory=lambda _settings: routes)
    for engine in app.state.checkups.registry.engines.values():
        engine.gate = FastGate()
    return app


def body(**overrides):
    payload = {"schemaVersion": "checkup-v1", "clientRequestId": "checkup-1",
               "engine": "baidu_e82", "center": dict(CENTER), "coordinateSystem": "bd09ll",
               "isochrone": {"budget": 200},
               "facilities": {"categories": ["shopping", "medical", "education"]}}
    payload.update(overrides)
    return payload


def terminal(client, task_id, timeout=900.0, until_stage=None):
    """Wait for a task to reach a terminal state, or for one stage to start."""
    deadline = time.monotonic() + timeout
    view = client.get(f"/api/v2/checkups/{task_id}").json()
    while time.monotonic() < deadline:
        if until_stage is not None and view["stage"] == until_stage:
            return view
        if until_stage is None and view["status"] in ("completed", "failed", "cancelled"):
            return view
        # Larger rounds take longer; avoid repeatedly parsing the full frozen
        # report at 50 polls/second while the worker journals hundreds of pages.
        time.sleep(0.2 if view.get('completion', {}).get('roundPoiLimit', 60) > 60 else 0.02)
        view = client.get(f"/api/v2/checkups/{task_id}").json()
    raise AssertionError(f"task {task_id} did not finish: {view}")


def run(client, payload):
    """Submit a task and wait for it to reach a terminal state."""
    created = client.post("/api/v2/checkups", json=payload)
    assert created.status_code == 202
    task_id = created.json()["taskId"]
    return task_id, terminal(client, task_id)


def document(client, task_id):
    response = client.get(f"/api/v2/checkups/{task_id}/result")
    assert response.status_code == 200, response.text
    return response.json()


def stored(app, task_id, index):
    """A published revision, read from the file the store froze."""
    revision = app.state.checkups.store.revisions(task_id)[index]
    path = app.state.checkups.store.root / revision["payload"]
    return revision, json.loads(path.read_text(encoding="utf-8")), path


# -- the loop ----------------------------------------------------------------

def test_both_engines_close_the_loop_over_their_own_boundary(tmp_path):
    for engine in ("baidu_e82", "osm_hybrid"):
        places = SyntheticPlaces(at_origin())
        app = make_app(tmp_path / engine, places)
        with TestClient(app) as client:
            task_id, view = run(client, body(engine=engine))
            assert view["status"] == "completed", view
            assert view["stage"] == "ready" and view["revision"] == 5
            # The facility stage sent one page per (block, minor category) and the
            # verification stage then asked for one route per candidate: both are
            # this task's requests, and the counters report the task's own attempts
            # rather than the facility stage's share of them.
            assert len(places.sent) == CORE_PAGES
            assert view["networkRequests"] == CORE_PAGES + CORE_ACCEPTED_TOTAL
            assert view["requests"] >= view["networkRequests"]
            # This deployment has no walking graph, so the assessment refused and
            # the checkup is not called complete on the strength of a boundary and
            # a retrieval: the question it exists for is still unanswered.
            assert view["businessStatus"] == "insufficient"

            boundary_revision, first, _ = stored(app, task_id, 0)
            revision, document_, path = stored(app, task_id, 1)
            assert boundary_revision["stage"] == "isochrone" and revision["stage"] == "poi"
            # Revision 2 repeats the boundary it counted over, unchanged: the two
            # documents differ in the facility group and the hashes, not here.
            assert document_["isochrone"] == first["isochrone"]
            assert document_["trace"]["isochroneHash"] == first["trace"]["isochroneHash"]
            assert document_["trace"]["resultHash"] != first["trace"]["resultHash"]
            assert document_["revision"] == 2 and document_["stage"] == "poi"
            # The published file is what the interface serves.
            assert document(client, task_id) == stored(app, task_id, 4)[1]

            group = document_["facilities"]
            assert group["queryStatus"] == "completed"
            # §4.4: a finished keyword search is still not a verified directory.
            assert group["catalogCompleteness"] == "unverified"
            assert group["dataSource"] == "synthetic" and group["provider"] == places.identity
            assert group["countsByCategory"] == CORE_ACCEPTED
            assert len(group["facilities"]) == CORE_ACCEPTED_TOTAL
            assert {item["classificationStatus"] for item in group["facilities"]} == {"accepted"}
            # One distinct row per minor category, repeated on every block's page.
            assert group["statistics"]["uidMergedRecords"] == CORE_PAGES - CORE_MINORS
            assert group["statistics"]["acceptedRecords"] == CORE_ACCEPTED_TOTAL
            assert group["statistics"]["invalidRecords"] == 0
            assert group["statistics"]["outsideBoundaryRecords"] == 0
            assert group["statistics"]["cachedPages"] == 0
            assert group["statistics"]["livePages"] == CORE_PAGES
            assert group["statistics"]["budgetSpent"] == CORE_PAGES
            assert group["statistics"]["pageAttempts"] == CORE_PAGES
            # 这一轮只发了主关键词（primary_queries_only），并且有一条候选需要人工
            # 复核（classification_needs_review）：两个都记在案，不是"没有警告"。
            assert group["stopReason"] is None
            assert group["warnings"] == ["classification_needs_review", "primary_queries_only"]
            assert group["queryDomain"]["paddingMeters"] == 1300
            assert group["dataObtainedAt"] > 0
            # §5 B2 决策 1：全部小类都查完了，所以"共同完成"就是整个圈面 ——
            # 服务目标 met，而这一条与 queryStatus 是两件事（见下面 partial 的用例）。
            assert group["queryAreaCoverage"]["status"] == "met"
            assert group["queryAreaCoverage"]["sharedCompletionRatio"] == 1.0
            assert group["queryAreaCoverage"]["residualRatio"] == 0.0
            assert group["queryAreaCoverage"]["categories"]
            # 首轮计划与它实际的执行一致：56 页、没有缓存可复用、需要 56 次新增调用。
            assert group["initialPlan"] == {
                "initialPageCount": CORE_PAGES, "reusableInitialPageCount": 0,
                "estimatedNewInitialCalls": CORE_PAGES, "remainingTaskBudget": 240,
                "remainingDailyBudget": 1600, "blocks": 4, "minorCategories": CORE_MINORS,
                "primaryQueriesOnly": True, "isReservation": False}

            assert document_["facilitiesStatus"] == "complete"
            # This is the revision that added the retrieved facilities, and a
            # finished keyword search is still not a verified directory, so
            # completeness is never claimed — not here and not in the revision
            # that follows it.
            assert document_["businessStatus"] == "partial"
            assert app.state.checkups.store.revisions(task_id)[4]["stage"] == "reporting"
            assert document_["trace"]["ruleVersions"]["classification"] == RULES["version"]
            assert document_["trace"]["budgets"]["poi"] == {"limit": 1200, "spent": CORE_PAGES}
            assert document_["trace"]["budgets"]["route"] == {"limit": 120, "spent": 0}
            assert document_["trace"]["budgets"]["detail"] == {"limit": 20, "spent": 0}
            assert document_["trace"]["budgets"]["isochrone"]["limit"] == 200

            # The key is a credential: it may not appear anywhere in a revision.
            assert SECRET not in path.read_text(encoding="utf-8")


def test_the_boundary_decides_what_counts_and_the_search_range_only_reaches(tmp_path):
    places = SyntheticPlaces(every_page([ORIGIN, offset(2000), offset(5000)]))
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, view = run(client, body())
        assert view["status"] == "completed", view
        _revision, document_, _path = stored(app, task_id, 1)
        group = document_["facilities"]
        # Only the point inside the computed boundary is a finding: the two
        # outside it changed the counts not at all.
        assert group["countsByCategory"] == CORE_ACCEPTED
        assert len(group["facilities"]) == CORE_ACCEPTED_TOTAL
        # 词典把"培训机构"判成培训业务而不是教育设施：它是被排除的候选，不是
        # "什么都没查到"。这和 countsByCategory 里少掉的 training 是同一件事。
        assert [item["sourceUid"] for item in group["excludedCandidates"]] == ["training-0"]
        assert group["statistics"]["excludedRecords"] == 1
        # Inside the search range but outside the boundary: kept where it is, as
        # evidence for the region rather than as a finding in it. Outside the
        # range entirely: kept too, and told apart by its reason.
        reasons = {}
        for item in group["quarantine"]:
            reasons[item["reason"]] = reasons.get(item["reason"], 0) + 1
        assert reasons == {"outside_counting_region": CORE_MINORS,
                           "outside_query_domain": len(places.sent)}
        statistics = group["statistics"]
        assert statistics["outsideBoundaryRecords"] == CORE_MINORS
        assert statistics["outsideDomainRecords"] == len(places.sent)
        assert statistics["acceptedRecords"] == CORE_ACCEPTED_TOTAL
        assert "outside_boundary_records" in group["warnings"]
        # The queries completed. A zero would have to be read with this status,
        # which is why the revision reports both axes.
        assert group["queryStatus"] == "completed"
        assert group["catalogCompleteness"] == "unverified"


def test_a_deployment_without_a_key_names_its_refusal_and_never_a_zero(tmp_path):
    app = make_app(tmp_path, None)
    with TestClient(app) as client:
        task_id, view = run(client, body())
        assert view["status"] == "completed", view
        document_ = document(client, task_id)
        # Null is the absence of a result; an empty group would be a result that
        # says no facility exists, which this run never established.
        assert document_["facilities"] is None
        assert document_["facilitiesStatus"] == "failed"
        assert document_["businessStatus"] == "insufficient"
        refusals = [item for item in document_["warnings"]
                    if item["code"] == "FACILITIES_UNAVAILABLE"]
        assert len(refusals) == 1 and refusals[0]["severity"] == "error"
        assert "AK" in refusals[0]["message"]
        assert document_["trace"]["budgets"]["poi"] == {"limit": 1200, "spent": 0}


def test_a_facility_run_that_fails_every_page_is_a_failed_query(tmp_path):
    places = SyntheticPlaces(failing())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        # 显式钉住 60 次：这一节测的是"额度在重试中途用完时，被拒的页面按名字记下来，
        # 而不是让整轮消失"。它必须是一个会中途用完的预算，不能跟着部署默认值走 ——
        # 默认值现在是 240，足够把 56 个序列的两次尝试都发完，那样这个分支就没人测了。
        task_id, view = run(client, body(facilities={"categories": CORE_MAJORS,
                                                     "maxPoiRequests": 60}))
        assert view["status"] == "completed", view
        document_ = document(client, task_id)
        group = document_["facilities"]
        # The stage ran and reported itself: an empty list here is a failed
        # retrieval, which the status and the page errors both say.
        assert group["queryStatus"] == "failed"
        # §5 B2 决策 1：一页都没查成 → 共同完成 0。必须是 unmet 而不是 unknown：
        # "算不出来"和"算出来没达标"是两件事，用前者描述后者等于替失败开脱。
        assert group["queryAreaCoverage"]["status"] == "unmet"
        assert group["queryAreaCoverage"]["sharedCompletionRatio"] == 0.0
        assert group["queryAreaCoverage"]["residualRatio"] == 1.0
        assert group["facilities"] == [] and group["countsByCategory"] == {
            "shopping": 0, "medical": 0, "education": 0}
        assert "upstream_error" in group["warnings"]
        assert all(record["pageErrors"] for record in group["queryCoverage"])
        assert all(not page["succeeded"]
                   for record in group["queryCoverage"] for page in record["pageRecords"])
        # A retry is an attempt: every one of them reserved its slot, so the
        # revision counts the pages it asked for and the attempts they cost.
        statistics = group["statistics"]
        assert len(places.sent) == statistics["pageAttempts"] == statistics["budgetSpent"] == 60
        # Every first-round page failed, so each of the 56 sequences spent its one
        # bounded retry. The pool stopped dispatching after 60 calls; the pages it
        # refused are recorded by name rather than ending the run, so all 56
        # sequences still reached an outcome and their evidence is not lost with
        # the pages that never went out.
        assert statistics["queryAttempts"] == 2 * CORE_PAGES
        assert statistics["processedPages"] == 2 * CORE_PAGES
        assert statistics["networkCalls"] == 60
        assert statistics["allowanceRefusedPages"] == 2 * CORE_PAGES - 60
        assert statistics["livePages"] == CORE_PAGES  # 56 distinct pages, 60 calls
        assert view["networkRequests"] == 60 and view["requests"] == 2 * CORE_PAGES
        # 首轮本身没超额度，停下来的是"后续重试要的新增调用"：停止原因说的是这一层，
        # 具体到页面的原因仍是池子的那一句。
        assert group["stopReason"] == "network_budget_exhausted"
        assert "task_budget_exhausted" in group["warnings"]
        assert document_["facilitiesStatus"] == "failed"
        assert document_["businessStatus"] == "insufficient"


def test_a_cancelled_facility_stage_keeps_what_it_retrieved(tmp_path):
    places = SyntheticPlaces(at_origin(), delay=0.05)
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        created = client.post("/api/v2/checkups", json=body())
        task_id = created.json()["taskId"]
        assert terminal(client, task_id, until_stage="poi")["stage"] == "poi"
        client.post(f"/api/v2/checkups/{task_id}/cancel")
        view = terminal(client, task_id)
        assert view["status"] == "cancelled", view
        # Both stages are on the record: the boundary was complete before the
        # stop, and the facility stage published what it had.
        assert view["revision"] == 2
        assert len(app.state.checkups.store.revisions(task_id)) == 2
        document_ = document(client, task_id)
        assert document_["facilitiesStatus"] == "partial"
        assert document_["businessStatus"] == "partial"
        group = document_["facilities"]
        assert group["queryStatus"] == "cancelled" and group["stopReason"] == "cancelled"
        # Whatever it did retrieve is still reported, and the queries it never
        # sent are visible as such.
        assert 0 <= group["statistics"]["queryAttempts"] < CORE_PAGES
        assert len(places.sent) <= group["statistics"]["queryAttempts"]
        assert group["statistics"]["acceptedRecords"] == len(group["facilities"])
        assert all(item["classificationStatus"] == "accepted" for item in group["facilities"])
        # The boundary revision still says what it said before the stage began.
        first = stored(app, task_id, 0)[1]
        assert document_["isochrone"] == first["isochrone"]


def test_a_cached_page_costs_no_attempt_and_keeps_its_data_time(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places, cache_freshness_seconds=60)
    with TestClient(app) as client:
        first_id, _view = run(client, body(clientRequestId="checkup-a"))
        first = document(client, first_id)
        assert len(places.sent) == CORE_PAGES
        second_id, view = run(client, body(clientRequestId="checkup-b"))
        assert view["status"] == "completed", view
        second = document(client, second_id)
        # §3.4: a hit is served outside the pool, so it spends nothing. The
        # route verification is a separate matter — no route is cached, so the
        # second task still asked once per candidate, and the counter reports
        # exactly those.
        assert len(places.sent) == CORE_PAGES
        assert view["networkRequests"] == CORE_ACCEPTED_TOTAL
        group = second["facilities"]
        assert group["statistics"]["cachedPages"] == CORE_PAGES
        assert group["statistics"]["livePages"] == 0
        assert group["statistics"]["budgetSpent"] == 0
        assert group["statistics"]["pageAttempts"] == 0
        # The planner still worked through every first-round query; it just did
        # not have to ask for any of them again.
        assert group["statistics"]["queryAttempts"] == CORE_PAGES
        assert group["statistics"]["networkCalls"] == 0
        assert group["stopReason"] is None
        # The same evidence, and the original data time rather than a new one.
        assert group["facilities"] == first["facilities"]["facilities"]
        assert group["countsByCategory"] == first["facilities"]["countsByCategory"]
        assert group["dataObtainedAt"] == first["facilities"]["dataObtainedAt"]
        # The page record's own source is the data source of the run; where the
        # bytes came from is a different question with a different key.
        record = group["queryCoverage"][0]["pageRecords"][0]
        assert record["source"] == "synthetic" and record["fetchSource"] == "cache"
        live = first["facilities"]["queryCoverage"][0]["pageRecords"][0]
        assert live["fetchSource"] == "live"


# -- the layers and the route placeholder ------------------------------------

def test_the_facility_layer_draws_findings_and_stays_tied_to_its_revision(tmp_path):
    places = SyntheticPlaces(every_page([ORIGIN, offset(2000)]))
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        layer = client.get(f"/api/v2/checkups/{task_id}/layers/facilities")
        boundary = client.get(f"/api/v2/checkups/{task_id}/layers/isochrone")
        assert layer.status_code == boundary.status_code == 200
        assert layer.json()["revision"] == 5
        assert layer.json()["resultHash"] == document_["trace"]["resultHash"]
        assert boundary.json()["resultHash"] == document_["trace"]["resultHash"]
        # Two layers of one revision are different representations: a validator
        # for one must not tell a client its copy of the other is current.
        assert layer.headers["etag"] != boundary.headers["etag"]
        assert layer.json()["displayGeometry"] is None

        collection = layer.json()["geometry"]
        assert collection["type"] == "FeatureCollection"
        assert collection["coordinateSystem"] == "bd09ll"
        assert len(collection["features"]) == CORE_ACCEPTED_TOTAL  # the refused point is not drawn
        properties = collection["features"][0]["properties"]
        assert collection["features"][0]["geometry"]["type"] == "Point"
        assert set(properties) == {"id", "name", "category", "majorCategory", "address",
                                  "classificationStatus", "possibleDuplicateGroup"}
        # 画出来的正是被接收的那些设施，且每条都属于本次查过的大类；第一张属于
        # 哪一类由词典顺序决定，不把它钉在某个小类上。
        assert {feature["properties"]["id"] for feature in collection["features"]} == {
            item["id"] for item in document_["facilities"]["facilities"]}
        assert properties["majorCategory"] in CORE_ACCEPTED and properties["category"]
        assert properties["id"].startswith("synthetic:") and properties["name"]

        # The boundary revision has no facilities to draw, and says so.
        assert client.get(f"/api/v2/checkups/{task_id}/layers/facilities?revision=1").json()[
            "code"] == "checkup_facilities_not_ready"
        # This deployment has no walking graph, so the assessment measured no
        # distance at all. The heat layer refuses by name instead of serving an
        # empty collection, which would read as "nothing is far from anything".
        assert client.get(f"/api/v2/checkups/{task_id}/layers/heatmap").json()["code"] == \
            "checkup_heatmap_not_ready"


def test_a_route_detail_is_only_offered_for_a_facility_of_this_checkup(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        # 取一个确定属于购物大类、且本次体检确实查过的小类，别把"第一条"当成 market。
        known = next(item["id"] for item in document_["facilities"]["facilities"]
                     if item["category"] == "market")
        before = app.state.checkups.store.revisions(task_id)
        # 服务池是唯一的分派点（§9.2）：记下每次通过它的调用，看点击是不是真的走了它。
        pool = app.state.quota.direction
        seen, budget = [], app.state.checkups.detail_budget(task_id, app.state.checkups.get(task_id))
        original = pool.attempt

        @contextlib.asynccontextmanager
        async def counted(deadline, **kwargs):
            seen.append({"pool": kwargs.get("pool"), "budget": kwargs.get("budget")})
            async with original(deadline, **kwargs) as handle:
                yield handle

        pool.attempt = counted

        detail = client.post(f"/api/v2/checkups/{task_id}/routes/{known}")
        assert detail.status_code == 200, detail.text
        answer = detail.json()
        # 详情证据回答的是已经发布的那一版，而不是产生一个新版本：§3.3 要的是独立
        # 证据，重新评估才生成新修订。
        assert answer["revision"] == document_["revision"] == 5
        assert answer["facilityId"] == known and answer["category"] == "market"
        assert answer["majorCategory"] == "shopping"
        assert answer["provider"] == SYNTHETIC_ROUTES
        assert answer["network"] is False and answer["attempts"] == 1
        # 这一家就在请求中心上，所以一次零距离的路线是这条路线本身的结论，不是"没走"。
        assert answer["straightLineM"] == answer["routeDistanceM"] == 0.0
        assert answer["withinRule"] is True and answer["evidenceGrade"] == "verified"
        assert answer["poiStatus"] == "verified_reachable" and answer["poiReason"] is None
        assert answer["budget"] == {"pool": "detail", "limit": 20, "spent": 1, "remaining": 19}
        assert answer["notes"] and "不改变已发布的评分" in answer["notes"][0]

        # 点击走的是核验阶段同一条闸门：同一个 DIRECTION 服务池，并且从详情桶里扣。
        # 账户保护靠的是这个池子（配额、限速、单飞行位），所以测试要看到它被经过。
        assert [(item["pool"], item["budget"]) for item in seen] == [("detail", budget)]
        again = client.post(f"/api/v2/checkups/{task_id}/routes/{known}").json()
        assert again["budget"]["spent"] == 2 and again["budget"]["remaining"] == 18
        assert len(seen) == 2

        # 没有任何一版被改写：点击不发布、不重算、不覆盖已冻结的结论。
        assert app.state.checkups.store.revisions(task_id) == before
        assert document(client, task_id)["trace"]["resultHash"] == \
            document_["trace"]["resultHash"]

        other = client.post(f"/api/v2/checkups/{task_id}/routes/baidu_place:nothing")
        assert other.status_code == 404 and other.json()["code"] == "checkup_facility_not_found"
        missing = client.post("/api/v2/checkups/nope/routes/some-facility")
        assert missing.status_code == 404 and missing.json()["code"] == "checkup_task_not_found"


def test_the_detail_pool_is_shared_between_clicks_and_named_when_it_runs_out(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        known = document(client, task_id)["facilities"]["facilities"][0]["id"]
        for spent in range(1, 21):
            answer = client.post(f"/api/v2/checkups/{task_id}/routes/{known}")
            assert answer.status_code == 200, answer.text
            assert answer.json()["budget"]["spent"] == spent
        # 21 次是具名拒绝，不是一个 200 带着空结果：本任务的详情额度用完了。
        refused = client.post(f"/api/v2/checkups/{task_id}/routes/{known}")
        assert refused.status_code == 429
        assert refused.json()["code"] == "checkup_detail_budget_exhausted"


# -- the verification layer --------------------------------------------------

def test_the_verification_layer_draws_what_was_asked_and_what_came_back(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places, routes=SyntheticRoutes(offset_routes()))
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        layer = client.get(f"/api/v2/checkups/{task_id}/layers/verification")
        assert layer.status_code == 200, layer.text
        body_ = layer.json()
        assert body_["revision"] == 5 and body_["geometry"] is not None
        properties = body_["geometry"]["properties"]
        assert properties["status"] == "partial" and properties["checked"] == CORE_ACCEPTED_TOTAL
        assert properties["provider"] == SYNTHETIC_ROUTES
        collection = body_["geometry"]
        assert collection["coordinateSystem"] == "bd09ll"
        assert len(collection["features"]) == CORE_ACCEPTED_TOTAL
        feature = collection["features"][0]
        # 画的是设施自己的位置，不是被吸附到 80 米外的那条路线端点。
        assert feature["geometry"]["coordinates"] == [ORIGIN[0], ORIGIN[1]]
        assert feature["properties"]["destinationOffsetM"] == 80.0
        assert feature["properties"]["poiStatus"] == "pending"
        assert feature["properties"]["poiReason"] == "endpoint_mapping_unconfirmed"
        # 端点偏移的这一条留在道路端点证据层，绝不提升为严格核验。
        assert feature["properties"]["evidenceGrade"] == "model"
        assert feature["properties"]["routeDestination"] is not None
        # 每一家都画出来了：图上少一个点就等于少报一次核验。
        assert {item["properties"]["facilityId"] for item in collection["features"]} == \
            {item["id"] for item in document(client, task_id)["facilities"]["facilities"]}


# -- 按需补查：十大类里的扩展类别 ---------------------------------------------
#
# 十类设施按需加出来，而不是把有业务依据的三类口径换掉：补查复用原体检已经算出的
# 圈面，只再跑一次设施检索，不发布修订、不改评分。下面验的就是这条边界 —— 复核、
# 预算、幂等和"取消不碰报告"都在这条链上。

#: 两个扩展大类，五个检索小类：足够证明补查只发自己那几类。
EXTENDED = ("dining", "leisure")
EXTENDED_MINORS = poi_keys(list(EXTENDED))


def extension_body(client_request_id, categories, **overrides):
    payload = {"schemaVersion": "checkup-v1", "clientRequestId": client_request_id,
               "categories": list(categories)}
    payload.update(overrides)
    return payload


def submit_extension(client, task_id, client_request_id, categories, **overrides):
    created = client.post(f"/api/v2/checkups/{task_id}/facility-extensions",
                          json=extension_body(client_request_id, categories, **overrides))
    assert created.status_code == 202, created.text
    return created.json()


def extension_of(client, task_id, extension_id, timeout=120.0):
    """等一次补查走到终态。``partial`` 也是终态：它不会自己继续。"""
    deadline = time.monotonic() + timeout
    view = client.get(f"/api/v2/checkups/{task_id}/facility-extensions/{extension_id}").json()
    while time.monotonic() < deadline:
        if view["status"] in ("completed", "partial", "failed", "cancelled"):
            return view
        time.sleep(0.02)
        view = client.get(f"/api/v2/checkups/{task_id}/facility-extensions/{extension_id}").json()
    raise AssertionError(f"extension {extension_id} did not finish: {view}")


def test_a_facility_extension_reuses_the_boundary_and_leaves_the_report_alone(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        before = document(client, task_id)
        revisions_before = len(app.state.checkups.store.revisions(task_id))
        sent_before = len(places.sent)
        minors = poi_keys(list(EXTENDED))
        assert len(minors) == 5

        created = submit_extension(client, task_id, "ext-1", EXTENDED)
        assert created["taskId"] == task_id
        # 绑定的是补查开始那一刻的最新修订：之后原任务再发版也不会改写这次补查的圈面。
        assert created["baseRevision"] == before["revision"]
        assert created["categories"] == list(EXTENDED)
        assert created["budget"]["limit"] >= 4 * len(minors)
        view = extension_of(client, task_id, created["extensionId"])
        assert view["status"] == "completed", view
        assert view["facilitiesStatus"] == "completed"
        assert set(view["countsByCategory"]) == set(EXTENDED)

        result = client.get(f"/api/v2/checkups/{task_id}/facility-extensions/"
                            f"{created['extensionId']}/result")
        assert result.status_code == 200, result.text
        document_ = result.json()
        assert document_["status"] == "completed"
        assert document_["baseRevision"] == before["revision"]
        group = document_["group"]
        assert group is not None and group["queryStatus"] == "completed"
        assert set(group["countsByCategory"]) == set(EXTENDED)
        assert {item["category"] for item in group["facilities"]} <= set(minors)
        assert group["queryDomain"]["origin"] is not None

        # 只发扩展类别：原任务那 14 个小类一条都没有重发，也没有重新成圈。
        resent = places.sent[sent_before:]
        assert {sequence_id.split(":")[1] for sequence_id, _page in resent} == set(minors)
        assert len(resent) == 4 * len(minors)  # 每个小类、每个查询分块各首查一次

        # 补查不发布修订、不改评分：报告逐字节不变，"十类设施"是加出来的。
        assert len(app.state.checkups.store.revisions(task_id)) == revisions_before
        assert document(client, task_id) == before
        assert any("不进入核心综合分" in note for note in document_["notes"])
        # 列表只列这一个任务自己的补查。
        listed = client.get(f"/api/v2/checkups/{task_id}/facility-extensions").json()
        assert [item["extensionId"] for item in listed] == [created["extensionId"]]


def test_an_extension_budget_too_small_for_its_categories_is_refused_with_numbers(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        sent_before = len(places.sent)
        minors = poi_keys(list(EXTENDED))
        refused = client.post(f"/api/v2/checkups/{task_id}/facility-extensions",
                              json=extension_body("ext-small", EXTENDED, maxPoiRequests=1))
        assert refused.status_code == 422, refused.text
        answer = refused.json()
        assert answer["code"] == "checkup_extension_budget_too_small"
        # 拒绝要把"首轮要多少页、缓存帮了多少、给了多少次"一起说出来，而不是提交后
        # 才给一个失败。"页"不是"次"：首轮页数不等于新增网络调用数。
        assert f"首轮需要 {4 * len(minors)} 页" in answer["message"]
        assert "缓存可复用 0 页" in answer["message"]
        assert f"还需新增 {4 * len(minors)} 次网络调用" in answer["message"]
        assert "只给了 1 次" in answer["message"]
        assert len(places.sent) == sent_before
        assert client.get(f"/api/v2/checkups/{task_id}/facility-extensions").json() == []


def test_an_extension_the_days_allowance_cannot_cover_is_refused_with_numbers(tmp_path):
    places = SyntheticPlaces(at_origin())
    # 三大类首轮 56 次要花掉当天额度的大部分；剩下的不够扩展类别就当场拒绝。
    app = make_app(tmp_path, places, baidu_place_daily_budget=60)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        remaining = app.state.quota.remaining("place")
        needed = 4 * len(poi_keys(list(EXTENDED)))
        assert remaining is not None and remaining < needed
        sent_before = len(places.sent)
        refused = client.post(f"/api/v2/checkups/{task_id}/facility-extensions",
                              json=extension_body("ext-daily", EXTENDED))
        assert refused.status_code == 429, refused.text
        answer = refused.json()
        assert answer["code"] == "checkup_extension_daily_budget"
        # 这一批类别一条缓存都没有，所以首轮页数与新增调用数相同；两个数字都要在，
        # 否则"页"和"次"又会被读成一回事。
        assert f"首轮 {needed} 页里缓存可复用 0 页" in answer["message"]
        assert f"还需新增 {needed} 次网络调用" in answer["message"]
        assert f"还剩 {remaining} 次" in answer["message"]
        assert len(places.sent) == sent_before


def test_the_same_extension_request_id_is_one_extension_and_a_different_one_conflicts(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        first = submit_extension(client, task_id, "ext-same", EXTENDED)
        extension_of(client, task_id, first["extensionId"])
        again = submit_extension(client, task_id, "ext-same", EXTENDED)
        assert again["extensionId"] == first["extensionId"]
        assert len(client.get(f"/api/v2/checkups/{task_id}/facility-extensions").json()) == 1
        # 同一个标识换一套参数是冲突，不是"悄悄按新的类别再查一次"。
        conflict = client.post(f"/api/v2/checkups/{task_id}/facility-extensions",
                               json=extension_body("ext-same", ("finance", "life")))
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["code"] == "checkup_extension_request_id_conflict"


def test_an_extension_is_readable_only_through_its_own_task(tmp_path):
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        other_id, _other = run(client, body(clientRequestId="checkup-2"))
        unknown = client.post("/api/v2/checkups/no-such-task/facility-extensions",
                              json=extension_body("ext-x", EXTENDED))
        assert unknown.status_code == 404, unknown.text
        assert unknown.json()["code"] == "checkup_task_not_found"
        assert client.get(f"/api/v2/checkups/{task_id}/facility-extensions/nope").status_code == 404
        created = submit_extension(client, task_id, "ext-own", EXTENDED)
        extension_of(client, task_id, created["extensionId"])
        # 另一个任务读不到这次补查：这是越权访问，不是"没有结果"。
        cross = client.get(f"/api/v2/checkups/{other_id}/facility-extensions/"
                           f"{created['extensionId']}")
        assert cross.status_code == 404, cross.text
        assert cross.json()["code"] == "checkup_extension_not_found"
        assert client.get(f"/api/v2/checkups/{other_id}/facility-extensions").json() == []


def test_an_extension_before_any_boundary_is_a_named_refusal(tmp_path):
    app = make_app(tmp_path, SyntheticPlaces(at_origin()))
    with TestClient(app) as client:
        # 一行还没有任何修订的任务：补查没有可复用的圈面。
        app.state.checkups.store.create(task_id="t-no-boundary", client_request_id="no-boundary",
                                        engine="baidu_e82", fingerprint="fp",
                                        payload=body(), budget=200)
        refused = client.post("/api/v2/checkups/t-no-boundary/facility-extensions",
                              json=extension_body("ext-none", EXTENDED))
        assert refused.status_code == 409, refused.text
        answer = refused.json()
        assert answer["code"] == "checkup_extension_boundary_unavailable"
        assert "圈面" in answer["message"]


def test_a_cancelled_extension_keeps_what_it_retrieved_and_never_touches_the_report(tmp_path):
    places = SyntheticPlaces(at_origin(), delay=0.1)
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        before = document(client, task_id)
        created = submit_extension(client, task_id, "ext-cancel", EXTENDED)
        extension_id = created["extensionId"]
        # 等它真的开始花钱再取消 —— 取消是在配额入口逐次生效的，不是事后改状态。
        deadline = time.monotonic() + 60
        while time.monotonic() < deadline:
            view = client.get(f"/api/v2/checkups/{task_id}/facility-extensions/"
                              f"{extension_id}").json()
            if view["status"] == "running" and view["networkRequests"] > 0:
                break
            time.sleep(0.02)
        client.post(f"/api/v2/checkups/{task_id}/facility-extensions/{extension_id}/cancel")
        view = extension_of(client, task_id, extension_id)
        assert view["status"] == "cancelled", view
        result = client.get(f"/api/v2/checkups/{task_id}/facility-extensions/"
                            f"{extension_id}/result").json()
        # 已取到的页面照常留下；缺的部分说成"取消"，绝不读成"没有设施"。
        assert result["status"] == "cancelled"
        assert result["group"] is not None and result["group"]["queryStatus"] == "cancelled"
        assert document(client, task_id) == before


# -- §三/§四：幂等先于预算，预检看得见缓存 ------------------------------------
#
# 这一节对着两件容易写反的事：一次已经受理过的补查不该因为"今天余额少了"而回不去；
# 一次不需要联网的补查也不该因为"余额不足"而开不了头。两者都靠同一个办法解决 ——
# 先认请求，再看首轮真正要发多少次新调用。

def drain_place(app):
    """把本应用的当日地点检索额度精确清零：不建 transport、不改配置。"""
    quota = app.state.quota
    limit = quota.tiers.active().place_daily_budget
    left = quota.ledger.remaining("place", limit)
    if left:
        quota.ledger.reserve("place", limit, cost=left)
    assert quota.remaining("place") == 0
    return quota.ledger.spent("place")


def extension_post(client, task_id, client_request_id, categories, **overrides):
    """提交一次补查并原样返回响应：拒绝路径要能看清状态码与错误码。"""
    return client.post(f"/api/v2/checkups/{task_id}/facility-extensions",
                       json=extension_body(client_request_id, categories, **overrides))


def extension_ids(client, task_id):
    return [item["extensionId"]
            for item in client.get(f"/api/v2/checkups/{task_id}/facility-extensions").json()]


def test_shutting_down_while_an_extension_runs_still_returns(tmp_path):
    """关闭应用时正在跑的补查不能把关闭流程卡住。

    检索器把"取消"当成一种结果：它带着已经取到的证据返回，而不是把 CancelledError
    抛出去。于是工作循环必须自己看 ``closing`` 退出 —— 只依赖那个异常的话，``close()``
    会一直等一个再也不会醒来的 worker。这条用例的失败方式是**挂住**（卡在退出 with
    的地方），不是断言失败：回归时它会让整套测试超时。
    """
    places = SyntheticPlaces(at_origin(), delay=0.05)
    app = make_app(tmp_path, places)
    manager = app.state.checkups
    with TestClient(app) as client:
        task_id, _view = run(client, body(facilities={"categories": ["dining"]}))
        created = submit_extension(client, task_id, "ext-at-shutdown", EXTENDED)
        assert created["status"] == "queued"
        # 故意不等它跑完。
    # 走到了这里就说明关闭返回了；补查这一行也必须落到终态，而不是留在 running。
    assert manager.store.extension(created["extensionId"]).status in (
        "completed", "partial", "failed", "cancelled")


def test_shutting_down_while_a_checkup_runs_still_returns(tmp_path):
    """主任务 worker 同一个道理：取消被阶段吸收之后，循环要能靠 closing 退出。"""
    places = SyntheticPlaces(at_origin(), delay=0.02)
    app = make_app(tmp_path, places)
    manager = app.state.checkups
    with TestClient(app) as client:
        created = client.post("/api/v2/checkups", json=body(clientRequestId="shutdown-task"))
        assert created.status_code == 202, created.text
        task_id = created.json()["taskId"]
        # 不等它完成就离开。
    assert manager.store.get(task_id).status in ("completed", "partial", "failed", "cancelled")


def test_a_replay_after_the_days_allowance_is_gone_returns_the_original(tmp_path):
    """§三.1：补查成功之后余额清零，同一个请求标识仍然取回原来那一次。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        first = submit_extension(client, task_id, "ext-replay", EXTENDED)
        finished = extension_of(client, task_id, first["extensionId"])
        assert finished["status"] == "completed"

        sent_before = len(places.sent)
        drain_place(app)
        # 清零本身就是一次记账：基线要在清零之后取，否则比的是"清零前后"。
        spent_before = app.state.quota.ledger.spent(PLACE)

        again = submit_extension(client, task_id, "ext-replay", EXTENDED)
        assert again["extensionId"] == first["extensionId"]
        assert again["status"] == finished["status"]
        assert again["baseRevision"] == first["baseRevision"]
        # 不入队、不发请求、不扣额度：账本与合成传输都停在原地。
        assert len(places.sent) == sent_before
        assert app.state.quota.ledger.spent(PLACE) == spent_before
        assert extension_ids(client, task_id) == [first["extensionId"]]


def test_a_conflict_beats_an_exhausted_allowance(tmp_path):
    """§三.2：余额为 0 也不许让"额度不足"盖掉"你换了参数"。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        first = submit_extension(client, task_id, "ext-conflict", EXTENDED)
        extension_of(client, task_id, first["extensionId"])
        drain_place(app)

        conflict = extension_post(client, task_id, "ext-conflict", ("finance",))
        assert conflict.status_code == 409, conflict.text
        assert conflict.json()["code"] == "checkup_extension_request_id_conflict"

        # 新的标识仍然是新请求：完全冷的那几类照样按额度拒绝，额度检查没有被删掉。
        refused = extension_post(client, task_id, "ext-cold-new", ("finance",))
        assert refused.status_code == 429, refused.text
        assert refused.json()["code"] == "checkup_extension_daily_budget"
        assert "还需新增" in refused.json()["message"]
        assert extension_ids(client, task_id) == [first["extensionId"]]


def test_a_fully_cached_extension_is_accepted_with_no_allowance_left(tmp_path):
    """§四：新标识、所需页面全在有效缓存里、日余额为 0 —— 照样完成，且不花钱。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        first = submit_extension(client, task_id, "ext-warm", EXTENDED)
        extension_of(client, task_id, first["extensionId"])

        sent_before = len(places.sent)
        drain_place(app)
        spent_before = app.state.quota.ledger.spent(PLACE)

        warm = submit_extension(client, task_id, "ext-warm-again", EXTENDED)
        view = extension_of(client, task_id, warm["extensionId"])
        assert view["status"] == "completed", view
        assert view["networkRequests"] == 0
        assert view["requests"] == 4 * len(poi_keys(list(EXTENDED)))
        plan = view["initialPlan"]
        assert plan["reusableInitialPageCount"] == plan["initialPageCount"] == 4 * len(EXTENDED_MINORS)
        assert plan["estimatedNewInitialCalls"] == 0
        assert plan["remainingDailyBudget"] == 0
        assert plan["isReservation"] is False
        # 一次网络调用都没有，账本一分不减。
        assert len(places.sent) == sent_before
        assert app.state.quota.ledger.spent(PLACE) == spent_before


def test_a_partly_cached_extension_spends_its_allowance_on_the_missing_pages(tmp_path):
    """§四/§五：部分命中时按缺页执行，不按完整冷启动页数拒绝 —— 额度只花在缺页上。

    父任务只查 dining，它的 8 页进了同一个任务的缓存；补查 dining+leisure 首轮共 20 页。
    先给"比缺页还少一次"的预算：受理（缓存那 8 页是可取得的证据，不该被冷启动口径挡掉），
    缺页只能发出 11 次，结果是 partial 且逐页记名。再用新标识给刚好够缺页的预算：只需
    最后 1 次就查完 —— 前一次已经把 11 页缺页取回来了。若还按冷启动 20 页来卡，第二次
    会被要求 20 次，这正是要修掉的行为。
    """
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body(facilities={"categories": ["dining"]}))
        shared = 4 * len(poi_keys(["dining"]))
        total = 4 * len(EXTENDED_MINORS)
        missing = total - shared
        assert 0 < shared < total
        spent_before = app.state.quota.ledger.spent(PLACE)

        created = submit_extension(client, task_id, "ext-partial-short", EXTENDED,
                                   maxPoiRequests=missing - 1)
        short = extension_of(client, task_id, created["extensionId"])
        assert short["status"] == "partial", short
        assert short["requests"] == total                 # 20 页都处理了
        assert short["networkRequests"] == missing - 1    # 只有缺页花了网络
        assert short["stopReason"] == "network_budget_exhausted"
        plan = short["initialPlan"]
        assert (plan["initialPageCount"], plan["reusableInitialPageCount"],
                plan["estimatedNewInitialCalls"]) == (total, shared, missing)
        assert app.state.quota.ledger.spent(PLACE) == spent_before + missing - 1

        # 上一次把 11 页缺页取回来了，所以这次只差 1 页：额度用在新增检索上。
        created = submit_extension(client, task_id, "ext-partial", EXTENDED,
                                   maxPoiRequests=missing)
        view = extension_of(client, task_id, created["extensionId"])
        assert view["status"] == "completed", view
        assert view["networkRequests"] == 1
        assert view["initialPlan"]["reusableInitialPageCount"] == total - 1
        assert view["initialPlan"]["estimatedNewInitialCalls"] == 1
        assert app.state.quota.ledger.spent(PLACE) == spent_before + missing


def test_a_replay_keeps_its_original_revision_after_the_parent_publishes_another(tmp_path):
    """§三.4：父任务发了新修订，原请求的合法重试仍绑在原来的 baseRevision 上。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        first = submit_extension(client, task_id, "ext-revision", EXTENDED)
        extension_of(client, task_id, first["extensionId"])
        bound = first["baseRevision"]

        store = app.state.checkups.store
        latest = store.revision(task_id)
        snapshot = dict(latest["snapshot"])
        snapshot["revision"] = latest["revision"] + 1
        store.publish(task_id, stage="accessibility", snapshot=snapshot,
                      result_hash="fixture-later-revision")
        assert store.revision(task_id)["revision"] == bound + 1

        replay = submit_extension(client, task_id, "ext-revision", EXTENDED)
        assert replay["extensionId"] == first["extensionId"]
        assert replay["baseRevision"] == bound          # 悄悄改绑新修订才是错的

        # 新标识才允许绑定新的可用修订。
        fresh = submit_extension(client, task_id, "ext-revision-new", EXTENDED)
        assert fresh["baseRevision"] == bound + 1


def test_a_replay_survives_a_latest_revision_whose_geometry_is_unusable(tmp_path):
    """§三.1：合法重试不该需要"最新修订的圈面还能用"。

    幂等查找发生在读取最新修订之前，所以父任务后来发了一版没有几何的修订时，已经受理过的
    那一次补查仍要能取回；只有真正的新请求才该拿到"圈面不可用"的具名拒绝。
    """
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    manager = app.state.checkups
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        first = submit_extension(client, task_id, "ext-replay-geometry", EXTENDED)
        extension_of(client, task_id, first["extensionId"])

        store = manager.store
        latest = store.revision(task_id)
        snapshot = dict(latest["snapshot"])
        snapshot["revision"] = latest["revision"] + 1
        snapshot["isochrone"] = {**snapshot["isochrone"], "geometry": None}
        store.publish(task_id, stage="isochrone", snapshot=snapshot,
                      result_hash="fixture-geometry-less-revision")

        replay = submit_extension(client, task_id, "ext-replay-geometry", EXTENDED)
        assert replay["extensionId"] == first["extensionId"]
        assert replay["baseRevision"] == first["baseRevision"]

        # 真正的新请求仍然按既有规则被拒，而且是具名的。
        refused = extension_post(client, task_id, "ext-after-geometry", EXTENDED)
        assert refused.status_code == 409, refused.text
        assert refused.json()["code"] == "checkup_extension_boundary_unavailable"


def test_an_omitted_budget_replay_survives_a_changed_default(tmp_path, monkeypatch):
    """§三.3/.5：身份不依赖"现在算出来的默认值"，显式等于默认值仍等价。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        first = submit_extension(client, task_id, "ext-default", EXTENDED)
        extension_of(client, task_id, first["extensionId"])
        assert first["budget"]["limit"] == 240          # 省略 → 取部署默认值

        raised = DEFAULT_POI_REQUESTS * 2              # 一个比当前默认更大的新默认
        monkeypatch.setattr("app.checkups.manager.DEFAULT_POI_REQUESTS", raised)
        replay = submit_extension(client, task_id, "ext-default", EXTENDED)
        assert replay["extensionId"] == first["extensionId"]

        # 同一个新默认下，新标识按新默认解析；缓存已满，所以照样受理。
        fresh = submit_extension(client, task_id, "ext-default-new", EXTENDED)
        assert fresh["budget"]["limit"] == raised

        # 显式给少了照样当场拒绝：修的是身份口径，不是把校验删了。默认值只保证
        # 首轮够用，它不会把一个显式的过小预算悄悄改大。
        first_round = 4 * len(poi_keys(["finance"]))
        refused = extension_post(client, task_id, "ext-default-tiny", ("finance",),
                                 maxPoiRequests=1)
        assert refused.status_code == 422, refused.text
        answer = refused.json()
        assert answer["code"] == "checkup_extension_budget_too_small"
        assert f"首轮需要 {first_round} 页" in answer["message"]


def test_a_replay_never_enqueues_a_second_run(tmp_path):
    """§三.6：重放只回记录，不入队；执行只有第一次那一次。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    manager = app.state.checkups
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        first = submit_extension(client, task_id, "ext-queue", EXTENDED)
        extension_of(client, task_id, first["extensionId"])

        queued = []
        monkeypatch_puts = manager.extension_queue.put_nowait
        manager.extension_queue.put_nowait = queued.append
        try:
            again = submit_extension(client, task_id, "ext-queue", EXTENDED)
        finally:
            manager.extension_queue.put_nowait = monkeypatch_puts
        assert again["extensionId"] == first["extensionId"]
        assert queued == []


def test_a_request_that_lost_the_creation_race_is_returned_not_enqueued(tmp_path, monkeypatch):
    """§三.6：早查没看到、事务里才发现已有行的一方只是取回那一行。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    manager = app.state.checkups
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        store = manager.store
        revision = store.revision(task_id)["revision"]
        record, created = store.create_extension(
            extension_id="race-winner", task_id=task_id, client_request_id="ext-race",
            identity={"categories": sorted(EXTENDED), "maxPoiRequests": None},
            fingerprint="fp", base_revision=revision, categories=list(EXTENDED), budget=60)
        assert created is True

        missed = []
        monkeypatch.setattr(store, "find_extension",
                            lambda task, request: missed.append((task, request)) or None)
        queued = []
        monkeypatch.setattr(manager.extension_queue, "put_nowait", queued.append)

        answer = submit_extension(client, task_id, "ext-race", EXTENDED)
        assert missed == [(task_id, "ext-race")]      # 早查确实"没看到"那一行
        assert answer["extensionId"] == "race-winner"
        assert queued == []
        assert extension_ids(client, task_id) == ["race-winner"]


def test_a_row_written_before_the_identity_column_is_still_replayable(tmp_path):
    """§三.6：旧版本写下的行仍然按它被创建时的等价规则重放。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        store = app.state.checkups.store
        revision = store.revision(task_id)["revision"]
        # 旧行冻的是"当时按默认值解析出来的那个数"，所以这里必须写当前默认真正解析出的
        # 预算，而不是一个字面量：等价规则比的是这个数，不是默认值本身。默认值从 60 变成
        # 240 之后，写死 60 会让这一行看起来"预算不同"，测的就不再是旧行的重放了。
        frozen = app.state.checkups._extension_boundary(
            task_id, FacilityExtensionRequest(client_request_id="probe",
                                              categories=EXTENDED)).requested
        now = time.time()
        with store._connection() as connection:
            connection.execute(
                "INSERT INTO facility_extensions (extension_id, task_id, client_request_id,"
                " fingerprint, base_revision, categories, budget, status, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,'failed',?,?)",
                ("legacy-row", task_id, "ext-legacy", "old-fingerprint", revision,
                 json.dumps(list(reversed(EXTENDED)), ensure_ascii=False), frozen, now, now))

        # 类别原序不同但集合相同、预算省略：仍然是同一次请求。
        replayed = submit_extension(client, task_id, "ext-legacy", EXTENDED)
        assert replayed["extensionId"] == "legacy-row"
        assert replayed["status"] == "failed"          # 取回终态，不偷偷续跑

        # 换了类别，或者显式给了一个与冻结预算不同的数：都是冲突。
        assert extension_post(client, task_id, "ext-legacy", ("finance",)).status_code == 409
        assert extension_post(client, task_id, "ext-legacy", EXTENDED,
                              maxPoiRequests=160).status_code == 409
        assert extension_ids(client, task_id) == ["legacy-row"]


def test_a_fully_cached_retrieval_runs_with_an_exhausted_task_bucket(tmp_path):
    """§四：首轮页面全在有效缓存里，任务桶为 0 也不该被挡在门外。"""
    places = SyntheticPlaces(at_origin())
    app = make_app(tmp_path, places)
    manager = app.state.checkups
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        stored = manager.store.revision(task_id)
        isochrone = stored["snapshot"]["isochrone"]
        payload = CheckupRequest(**manager.store.get(task_id).payload)
        snapshot = SimpleNamespace(geometry=isochrone["geometry"], quality=isochrone["quality"])
        # 桶一点不剩：这在旧实现里就是"本任务的 POI 预算已用尽"。
        budget = app.state.quota.task_budget(isochrone=0, poi=0)
        context = EngineContext(task_id=task_id, token=CancelToken(),
                                deadline=time.monotonic() + 60,
                                artifact_dir=tmp_path / "artifacts")
        sent_before = len(places.sent)
        spent_before = app.state.quota.ledger.spent(PLACE)
        outcome = asyncio.run(collect_facilities(
            payload, snapshot, settings=Settings(_env_file=None, baidu_map_ak=""),
            context=context, quota=app.state.quota, budget=budget, cache=manager.cache,
            places_factory=lambda _settings: places))
        assert outcome.group is not None, [issue.message for issue in outcome.issues]
        assert outcome.status == "complete"
        assert outcome.requests == CORE_PAGES        # 页面都处理了
        assert outcome.network_requests == 0         # 一次网络调用都没有
        assert outcome.group.initial_plan["reusableInitialPageCount"] == CORE_PAGES
        assert len(places.sent) == sent_before
        assert app.state.quota.ledger.spent(PLACE) == spent_before


def test_an_allowance_that_runs_out_after_the_precheck_still_bounds_every_dispatch(tmp_path):
    """§四：预检通过之后余额被用掉，派发前仍由服务池拦下 —— 不超额、不伪成功。

    父任务只够取回首轮第一页，于是缓存里正好是"可复用 > 0"的那种检索：预检放行，
    但缺页一页都发不出去。要求是三条同时成立：一条都不多发、缓存里的证据留住、
    结果说 partial 而不是 completed。
    """
    places = SyntheticPlaces(two_page_rows)
    app = make_app(tmp_path, places)
    manager = app.state.checkups
    with TestClient(app) as client:
        task_id, _view = run(client, body(
            facilities={"categories": list(default_analysis_majors()),
                        "maxPoiRequests": CORE_PAGES}))
        first_group = document(client, task_id)["facilities"]
        assert first_group["queryStatus"] == "partial"
        assert first_group["statistics"]["networkCalls"] == CORE_PAGES

        stored = manager.store.revision(task_id)
        isochrone = stored["snapshot"]["isochrone"]
        payload = CheckupRequest(**manager.store.get(task_id).payload)
        snapshot = SimpleNamespace(geometry=isochrone["geometry"], quality=isochrone["quality"])
        sent_before = len(places.sent)
        spent_before = app.state.quota.ledger.spent(PLACE)
        outcome = asyncio.run(collect_facilities(
            payload, snapshot, settings=Settings(_env_file=None, baidu_map_ak=""),
            context=EngineContext(task_id=task_id, token=CancelToken(),
                                  deadline=time.monotonic() + 60, artifact_dir=tmp_path / "later"),
            quota=app.state.quota, budget=app.state.quota.task_budget(isochrone=0, poi=0),
            cache=manager.cache, places_factory=lambda _settings: places))
        assert outcome.group is not None, [issue.message for issue in outcome.issues]
        # 预检放行（可复用 > 0），但一次也没派发出去：池子仍然在派发前说了不。
        assert outcome.group.initial_plan["reusableInitialPageCount"] == CORE_PAGES
        assert outcome.network_requests == 0
        assert len(places.sent) == sent_before
        assert app.state.quota.ledger.spent(PLACE) == spent_before
        # 缺页没取到就说缺页没取到：不是 completed，也不是"这里没有设施"。
        assert outcome.status == "partial"
        assert outcome.group.stop_reason == "network_budget_exhausted"
        assert outcome.group.facilities
        assert outcome.group.statistics["allowanceRefusedPages"] == CORE_PAGES


# -- 同位置、每次 60 次：两条能接着查的路径 ------------------------------------
#
# "同一个地理位置再来一次"有两件事，缓存行为完全不同，必须分开读：
#
# * **重新体检**是一次新任务（新 task_id）。页面缓存按任务归属，默认没有跨任务窗口，
#   所以它一页也复用不到，要从头花额度。
# * **原任务补查**（新补查标识）沿用原任务的圈面与缓存，首轮 56 页是重放，额度只花在
#   缺页上 —— 这是把已有额度真正用在新增检索上的那条路径。
#
# 下面把这四种情形钉住：默认档位（一天 80 次）、额度充足、跨日，以及完全命中缓存。
# 每次请求都显式写 maxPoiRequests=60，设施是否查完一律读 queryStatus，不读任务状态。

CORE_MAJORS = list(default_analysis_majors())
#: 单任务请求上限的默认值（60）。首轮 56 页只是"至少"，密集数据下整轮要 112 页 ——
#: 这个差距正是本节要测的东西，所以每次请求都显式写它，不靠默认值恰好也是 60。
#: 单任务请求上限。这一节要的是"预算会在中途用完"的那种情形（首轮 56 页、整轮 112 页、
#: 每次只给 60），所以显式钉住 60，而不是读部署默认值 —— 默认值现在是 240，够把整轮一次
#: 跑完，"跨日推进"这一节就不再成立了。
REQUEST_LIMIT = 60


class FixedClock:
    """受控时钟：档位切换、跨日与缓存有效期都读它，测试不依赖真实日期。"""

    def __init__(self, now):
        self.now = now

    def __call__(self):
        return self.now


#: 生产档位的形状：切换时间已过 → 生效的是 fallback 档，应用日额度 80。QPS 调高只是不去
#: 测限速（限速本身由 test_rate_gate 负责），它不影响预算语义。
FALLBACK_SHAPE = dict(baidu_quota_fallback_at="2026-09-30T00:00:00+08:00",
                      baidu_fallback_place_qps=10000, baidu_fallback_direction_qps=10000,
                      baidu_fallback_place_daily_budget=80)
#: 默认档位的应用日额度（80），与 FALLBACK_SHAPE 里的那个数必须一致。
DAILY_LIMIT = FALLBACK_SHAPE["baidu_fallback_place_daily_budget"]
#: 受控时钟的起点：2026-10-08 18:00（北京时间），落在切换时间之后。
PROBE_MOMENT = datetime.fromisoformat("2026-10-08T18:00:00+08:00").timestamp()


def fallback_app(tmp_path, places, **overrides):
    """默认档位下的离线应用，外加一个受控时钟。``overrides`` 可以覆盖档位形状。"""
    shape = dict(FALLBACK_SHAPE)
    shape.update(overrides)
    app = make_app(tmp_path, places, **shape)
    clock = FixedClock(PROBE_MOMENT)
    app.state.quota.tiers.clock = clock
    app.state.quota.ledger.clock = clock
    app.state.checkups.cache.clock = clock
    return app, clock


def facilities_body(client_request_id, **overrides):
    """一类核心口径、每次 60 次的体检请求。"""
    facilities = {"categories": CORE_MAJORS, "maxPoiRequests": REQUEST_LIMIT}
    facilities.update(overrides.pop("facilities", {}))
    return body(clientRequestId=client_request_id, facilities=facilities, **overrides)


def successful_pages(group):
    """这一类检索真正取到页面的首轮页数（重放与联网都算，因为证据是一样的）。"""
    return sum(item["pages"] for item in group["queryCoverage"])


def extension_after(client, task_id, client_request_id, categories=None, **overrides):
    """提交一次补查并等它到终态。"""
    created = submit_extension(client, task_id, client_request_id,
                               CORE_MAJORS if categories is None else categories, **overrides)
    return extension_of(client, task_id, created["extensionId"])


def extension_result(client, task_id, view):
    response = client.get(f"/api/v2/checkups/{task_id}/facility-extensions/"
                          f"{view['extensionId']}/result")
    assert response.status_code == 200, response.text
    return response.json()["group"]


def reusable_pages(app, task_id):
    """这一任务在预检眼里能复用多少首轮页面：真实页键、真实有效性规则。"""
    manager = app.state.checkups
    stored = manager.store.revision(task_id)
    isochrone = stored["snapshot"]["isochrone"]
    payload = manager.store.get(task_id).payload
    origin = normalize((payload["center"]["lng"], payload["center"]["lat"]))
    domain, _widened = query_domain(isochrone["geometry"], origin, QUERY_PADDING_M)
    provider, api_version = declared_identity(manager.settings, manager.place_factory)
    plan = poi_plan.initial_plan(domain, origin, poi_keys(CORE_MAJORS),
                                 provider=provider, api_version=api_version)
    estimate = poi_plan.estimate(plan, manager.cache, task_id=task_id,
                                 remaining_task_budget=REQUEST_LIMIT,
                                 remaining_daily_budget=app.state.quota.remaining(PLACE))
    return estimate.reusable_initial_page_count


def test_the_same_coordinates_in_a_new_task_do_not_reuse_the_first_tasks_pages(tmp_path):
    """§六：同位置不等于同任务。默认没有跨任务窗口，所以重新体检要重新花额度。

    这是部署的取舍，不是缓存失效：下面同时断言"第一次取的页面确实还在缓存里"和
    "第二次在预检眼里一页都复用不到"，让这条边界有据可查；第二次得到的是具名拒绝，
    不是"这里没有设施"。
    """
    places = SyntheticPlaces(two_page_rows)
    app, _clock = fallback_app(tmp_path, places)
    with TestClient(app) as client:
        first_id, first_view = run(client, facilities_body("same-place-1"))
        first_group = document(client, first_id)["facilities"]
        assert first_view["status"] == "completed"
        assert first_group["queryStatus"] == "partial"        # 60 次只够首轮加 4 页
        assert first_group["statistics"]["networkCalls"] == REQUEST_LIMIT
        assert successful_pages(first_group) == REQUEST_LIMIT
        assert app.state.quota.ledger.spent(PLACE) == REQUEST_LIMIT
        assert app.state.quota.remaining(PLACE) == DAILY_LIMIT - REQUEST_LIMIT

        second_id, second_view = run(client, facilities_body("same-place-2"))
        second_doc = document(client, second_id)
        # 任务按规则跑完了，只是设施阶段被拒 —— 这两件事必须分开读。
        assert second_view["status"] == "completed"
        assert second_doc["facilities"] is None
        assert second_doc["facilitiesStatus"] == "failed"
        refusal = next(item for item in second_doc["warnings"] if item["scope"] == "facilities")
        assert refusal["code"] == "FACILITIES_UNAVAILABLE"
        assert f"首轮需要 {CORE_PAGES} 页" in refusal["message"]
        assert "缓存可复用 0 页" in refusal["message"]
        assert f"本应用今天还剩 {DAILY_LIMIT - REQUEST_LIMIT} 次" in refusal["message"]
        # 一条都没有多发，账本也没有再动。
        assert len(places.sent) == app.state.quota.ledger.spent(PLACE) == REQUEST_LIMIT

        # 第一次取的页面还在缓存里，只是归第一次那个任务：换个 task_id 就看不到了。
        assert reusable_pages(app, first_id) == CORE_PAGES
        assert reusable_pages(app, second_id) == 0
        # 第二次的请求也没有被受理成补查：它是体检，不是补查。
        assert client.get(f"/api/v2/checkups/{second_id}/facility-extensions").json() == []


def test_each_extension_advances_only_as_far_as_the_days_balance_allows(tmp_path):
    """§六/§八：补查把额度花在缺页上；能推进到哪一步由当日余额决定，跨日再推进。

    密集数据下整轮要 112 页（56 个首轮页 + 56 个第二页），每次上限 60。默认档位一天 80 次：

    * 首轮：发 60 次，取到 56 个首轮页 + 4 个第二页，partial；
    * 第一次补查：56 页重放不要额度，剩下 20 次全花在缺页上，推到 80 页，日余额归零；
    * 同一天第二次补查：预检放行（首轮 56 页全在缓存里），但 0 次可用，一页也推不动；
    * 跨日第三次补查：把最后 32 页取回来，第二次才真正 completed。

    全程原任务的报告与修订逐字不变，仍是 partial。
    """
    places = SyntheticPlaces(two_page_rows)
    app, clock = fallback_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, view = run(client, facilities_body("advance-1"))
        parent = document(client, task_id)
        parent_group = parent["facilities"]
        assert view["status"] == "completed"
        assert parent_group["queryStatus"] == "partial"
        assert successful_pages(parent_group) == REQUEST_LIMIT
        assert app.state.quota.ledger.spent(PLACE) == REQUEST_LIMIT
        revisions = app.state.checkups.store.revisions(task_id)

        first = extension_after(client, task_id, "advance-ext-1")
        assert first["status"] == "partial", first
        assert first["networkRequests"] == DAILY_LIMIT - REQUEST_LIMIT
        assert successful_pages(extension_result(client, task_id, first)) == DAILY_LIMIT
        assert extension_result(client, task_id, first)["stopReason"] == "network_budget_exhausted"
        assert app.state.quota.remaining(PLACE) == 0

        # 同一天、余额 0：预检看得见缓存所以放行，但缺页一页也发不出去，进度不动。
        second = extension_after(client, task_id, "advance-ext-2")
        assert second["initialPlan"]["reusableInitialPageCount"] == CORE_PAGES
        assert second["initialPlan"]["estimatedNewInitialCalls"] == 0
        assert second["networkRequests"] == 0
        assert successful_pages(extension_result(client, task_id, second)) == DAILY_LIMIT
        assert app.state.quota.ledger.spent(PLACE) == DAILY_LIMIT
        assert app.state.quota.remaining(PLACE) == 0

        # 北京时间跨日：日账本按新的日期从零开始，第三次补查把剩下的缺页取回来。
        clock.now += 86400
        assert app.state.quota.remaining(PLACE) == DAILY_LIMIT
        third = extension_after(client, task_id, "advance-ext-3")
        assert third["status"] == "completed", third
        assert third["networkRequests"] == 2 * CORE_PAGES - DAILY_LIMIT
        assert successful_pages(extension_result(client, task_id, third)) == 2 * CORE_PAGES
        assert app.state.quota.ledger.spent(PLACE) == 2 * CORE_PAGES - DAILY_LIMIT

        # 补查不碰原任务：修订数、报告、评分与设施状态一个字节都没变。
        assert len(app.state.checkups.store.revisions(task_id)) == len(revisions)
        assert document(client, task_id) == parent
        assert parent["facilities"]["queryStatus"] == "partial"
        assert parent["facilities"]["statistics"]["networkCalls"] == REQUEST_LIMIT


def test_a_fully_cached_retrieval_at_the_same_budget_costs_no_new_calls(tmp_path):
    """§八：首轮页面已经全在缓存里时，60 次预算一次也不该花 —— 走补查接口验证。"""
    places = SyntheticPlaces(at_origin())
    app, _clock = fallback_app(tmp_path, places)
    with TestClient(app) as client:
        task_id, _view = run(client, facilities_body("warm-1"))
        group = document(client, task_id)["facilities"]
        assert group["queryStatus"] == "completed"            # 稀疏数据一页就查完
        assert group["statistics"]["networkCalls"] == CORE_PAGES
        dispatched = len(places.sent)
        spent = app.state.quota.ledger.spent(PLACE)

        view = extension_after(client, task_id, "warm-ext")
        assert view["status"] == "completed", view
        assert view["requests"] == CORE_PAGES                 # 页面都处理了
        assert view["networkRequests"] == 0                   # 一次网络调用都没有
        assert view["initialPlan"]["reusableInitialPageCount"] == CORE_PAGES
        assert view["initialPlan"]["estimatedNewInitialCalls"] == 0
        assert successful_pages(extension_result(client, task_id, view)) == CORE_PAGES
        assert len(places.sent) == dispatched
        assert app.state.quota.ledger.spent(PLACE) == spent


def test_replaying_a_checkup_request_id_never_starts_a_second_retrieval(tmp_path):
    """§三：同一个请求标识的重复提交取回原任务，不重新检索、不再花额度。

    即使第一次的结果是 partial（终态），重放也不是续跑：要接着查得用新的补查标识。
    """
    places = SyntheticPlaces(two_page_rows)
    app, _clock = fallback_app(tmp_path, places)
    request = facilities_body("replay-1")
    with TestClient(app) as client:
        task_id, _view = run(client, request)
        before = document(client, task_id)
        assert before["facilities"]["queryStatus"] == "partial"
        dispatched = len(places.sent)
        spent = app.state.quota.ledger.spent(PLACE)

        again = client.post("/api/v2/checkups", json=request)
        assert again.status_code == 202, again.text
        assert again.json()["taskId"] == task_id
        assert terminal(client, task_id)["status"] == "completed"
        # 重放不改写已经发布的报告，也不再发一条请求、不再扣一次额度。
        assert document(client, task_id) == before
        assert len(places.sent) == dispatched
        assert app.state.quota.ledger.spent(PLACE) == spent
        assert client.get(f"/api/v2/checkups/{task_id}/facility-extensions").json() == []


def test_cross_task_reuse_is_the_deployment_option_that_lets_a_new_task_continue(tmp_path):
    """同一个位置重新体检，能不能接着用上一次的页面 —— 取决于部署有没有开跨任务窗口。

    这一条把两种部署并排放在一起，同一个合成数据、同一份 60 次预算：

    * 默认（未配 CACHE_FRESHNESS_SECONDS）：第二次一页都复用不到，重新花 60 次，仍 partial；
    * 显然的对照（配 3600 秒、且日额度足够）：第二次首轮 56 页全部复用，只花 52 次就把
      112 页查完。

    两种都合法：跨任务复用是一项数据使用决定，默认关着是保守值，不是缺陷。写在这里是为了
    让"重新体检查不完"有确定的开关，而不是让人以为缓存坏了。
    """
    # 默认：没有跨任务窗口，同位置的新任务重新花一次额度。
    places = SyntheticPlaces(two_page_rows)
    app, _clock = fallback_app(tmp_path / "default", places)
    with TestClient(app) as client:
        first_id, _view = run(client, facilities_body("no-window-1"))
        assert document(client, first_id)["facilities"]["queryStatus"] == "partial"
        dispatched, spent = len(places.sent), app.state.quota.ledger.spent(PLACE)
        assert (dispatched, spent) == (REQUEST_LIMIT, REQUEST_LIMIT)

        second_id, _view = run(client, facilities_body("no-window-2"))
        group = document(client, second_id)["facilities"]
        assert group is None                                    # 余额不够，设施阶段没开始
        assert len(places.sent) == dispatched                   # 一条都没多发

    # 对照：开了跨任务窗口，且日额度足够 —— 新任务的重放不花额度，缺页才花。
    other = SyntheticPlaces(two_page_rows)
    window_app, _clock = fallback_app(tmp_path / "window", other, cache_freshness_seconds=3600,
                                      baidu_place_daily_budget=1600,
                                      baidu_quota_fallback_at="2099-01-01T00:00:00+08:00")
    with TestClient(window_app) as client:
        first_id, _view = run(client, facilities_body("window-1"))
        assert document(client, first_id)["facilities"]["queryStatus"] == "partial"
        dispatched, spent = len(other.sent), window_app.state.quota.ledger.spent(PLACE)

        second_id, _view = run(client, facilities_body("window-2"))
        group = document(client, second_id)["facilities"]
        assert group["queryStatus"] == "completed"
        assert group["initialPlan"]["reusableInitialPageCount"] == CORE_PAGES
        assert successful_pages(group) == 2 * CORE_PAGES
        assert len(other.sent) - dispatched == 2 * CORE_PAGES - REQUEST_LIMIT
        assert window_app.state.quota.ledger.spent(PLACE) - spent == 2 * CORE_PAGES - REQUEST_LIMIT
