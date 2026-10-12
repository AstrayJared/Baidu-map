"""M5 §6.3: the route verification stage and what it refuses to conclude.

The stage asks Baidu for real walking routes for the few facilities the model is
least sure about. Every case here runs offline: the transport is a Python
callable that reserves each attempt through the same pool shape the production
session uses, so the counting is real and no request leaves the machine.

What is under test is the four rules the stage is built on: a straight line only
orders candidates, a route whose endpoint moved is never promoted to strict POI
verification, one route never re-verdicts a whole cell, and "nobody answered" is
not the same answer as "nothing is there".
"""
import asyncio
import math
from dataclasses import asdict
from types import SimpleNamespace

import pytest
from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import RouteObservation

from app.accessibility.service_graph import Entrance
from app.catalog import major_of
from app.checkups.verification_stage import (CANDIDATE_STRAIGHT_LINE_M, NO_FACILITIES,
                                             NO_TRANSPORT, candidate_order, usable_route,
                                             verify_facilities, within_rule)
from app.quota import BudgetExhausted
from app.quota import TaskBudget
from app.checkups.routes import ReplayRoutes

# The sibling module's offline transport and its two responders: the pipeline
# suites use them end to end, these cases drive the stage on its own.
from test_checkup_facilities import (ORIGIN, SyntheticRoutes, offset, offset_routes,
                                     straight_routes)

MAJORS = ("shopping", "medical", "education")


def test_restart_replays_saved_evidence_even_when_lifetime_budget_is_empty():
    known, unknown = facility(0, 100), facility(1, 950)
    destination = tuple(known['location'][key] for key in ('lng', 'lat'))
    observation = straight_routes()(ORIGIN, destination, known['id'])
    store = SimpleNamespace(saved_routes=lambda _: [(
        {'facilityId': known['id'], 'origin': ORIGIN, 'destination': destination}, asdict(observation))])
    # A missing higher-priority candidate must not prevent replaying a later saved route.
    session = SimpleNamespace(attempts=0, stop_reason=None, identity='saved-only')
    budget = TaskBudget(isochrone=200, route=120, spent={'route': 120})
    replay = ReplayRoutes(session, store=store, task_id='restored', budget=budget)
    outcome = asyncio.run(verify_facilities(facilities=[unknown, known], majors=MAJORS,
        zones=[], heatmap={}, entrances={}, session=replay, origin=ORIGIN))
    assert [row['facilityId'] for row in outcome.evidence.facilities] == [known['id']]
    assert budget.spent['route'] == 120
    assert outcome.network_requests == 0


class FastPool:
    """The service pool without pacing: this file tests the stage, not the gate."""

    def __init__(self):
        self.attempts = 0

    def attempt(self, deadline, *, budget=None, pool=None):
        self.attempts += 1
        return _Attempt()


class _Attempt:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def outcome(self, reason):
        pass


def facility(index, meters, category="market", *, east=1.0):
    lng, lat = offset(meters, east=east)
    return {"id": f"f-{index}", "category": category, "location": {"lng": lng, "lat": lat}}


def heatmap_for(facilities, status="covered"):
    """A heat layer whose verdict is ``status`` for the cell of each facility.

    The published heat layer is keyed by **major** category while a facility
    record carries its minor one, and the stage looks the cell up by major — a
    heatmap keyed by minor would silently find nothing and report no conflicts.
    """
    projection = LocalProjection(ORIGIN)
    cells = {}
    for item in facilities:
        x, y = projection.to_local((item["location"]["lng"], item["location"]["lat"]))
        cells.setdefault(major_of(item["category"]), []).append(
            {"cell": f"0:{int(x // 50)}:{int(y // 50)}", "status": status})
    return {"categories": cells}


def run(respond, facilities, *, zones=None, heatmap=None, entrances=None, majors=MAJORS):
    routes = SyntheticRoutes(respond)
    session = routes.session(FastPool(), budget=None, deadline=time_out())
    outcome = asyncio.run(verify_facilities(
        facilities=facilities, majors=majors, zones=zones or [], heatmap=heatmap or {},
        entrances=entrances or {}, session=session, origin=ORIGIN))
    return routes, outcome


def time_out():
    import time
    return time.monotonic() + 60


# -- which facilities get asked, and in what order ---------------------------

def test_the_queue_prefers_the_suspected_and_the_unresolved_over_the_close_ones():
    facilities = [facility(0, 300, "market"), facility(1, 950, "market"),
                  facility(2, 1150, "pharmacy"), facility(3, 600, "primary_school")]
    entrances = {"f-3": Entrance(facility_id="f-3", category="education", source="point",
                                 point=offset(600), attachment=None, status="unresolved",
                                 reason="no_usable_edge")}
    zones = [{"id": "zone-1", "nearestFacility": "f-2"}]
    order = candidate_order(facilities, majors=MAJORS, zones=zones, entrances=entrances,
                            origin=ORIGIN)
    # 两个排序叠在一起，缺一不可：档位在**类别内**决定谁先被问（灰区自己报出的
    # "最近设施"第一，入口解析不了的第二，其余按离 1000 米判定线的远近），然后类别
    # 之间轮转交错 —— 所以 950 米的购物设施排在医疗那家疑似灰区之前，而它又是购物类
    # 里第一个：|950 − 1000| 比 |600 − 1000| 和 |300 − 1000| 都近。
    assert [(item["facilityId"], item["priority"]) for item in order] == [
        ("f-1", "decision_edge"), ("f-2", "suspected_zone"),
        ("f-3", "unresolved_entrance"), ("f-0", "decision_edge")]
    assert order[1]["straightLineM"] == pytest.approx(1150, abs=1)
    assert [item["facilityId"] for item in order if item["major"] == "shopping"] == ["f-1", "f-0"]
    assert entrances["f-3"].attachment is None
    assert order[2]["entranceStatus"] == "unresolved"


def test_a_facility_no_walking_route_could_reach_is_not_even_asked():
    facilities = [facility(0, CANDIDATE_STRAIGHT_LINE_M + 50), facility(1, 900)]
    order = candidate_order(facilities, majors=MAJORS, zones=[], entrances={}, origin=ORIGIN)
    assert [item["facilityId"] for item in order] == ["f-1"]
    # 直线只用来排队和保守筛选，它本身不是任何判定。
    assert all("straightLineM" in item for item in order)


def test_categories_take_turns_rather_than_sharing_by_headcount():
    facilities = ([facility(index, 900 - index, "market") for index in range(3)]
                  + [facility(10, 900, "pharmacy")])
    order = candidate_order(facilities, majors=MAJORS, zones=[], entrances={}, origin=ORIGIN)
    # 三类里只有两类有设施：数量差三倍不该让预算也差三倍。
    assert [item["major"] for item in order] == ["shopping", "medical", "shopping", "shopping"]


# -- what one route is allowed to conclude -----------------------------------

def test_a_returned_route_is_strict_evidence_and_the_tolerance_band_stays_undecided():
    facilities = [facility(0, 300), facility(1, 600), facility(2, 950)]
    _routes, outcome = run(straight_routes(), facilities, heatmap=heatmap_for(facilities))
    records = {item["facilityId"]: item for item in outcome.evidence.facilities}
    # 300 m × 1.15 = 345 m：在 1000 米规则内，也在 900 秒阈值内。
    assert records["f-0"]["withinRule"] is True
    assert records["f-0"]["poiStatus"] == "verified_reachable"
    assert records["f-0"]["evidenceGrade"] == "verified"
    # 600 m × 1.15 = 690 m 是"可达"；950 m × 1.15 = 1092 m 落在 1000 ± 100 的判定
    # 误差带里：这一条既不支持覆盖也不支持缺口，报成 None 而不是 False。
    assert records["f-1"]["poiStatus"] == "verified_reachable"
    assert records["f-2"]["withinRule"] is None
    assert records["f-2"]["poiStatus"] == "verified_unreachable"
    assert records["f-2"]["routeDistanceM"] == pytest.approx(1092.4, abs=1.0)


def test_an_offset_endpoint_stays_route_evidence_and_is_never_promoted():
    facilities = [facility(0, 950)]
    routes, outcome = run(offset_routes(), facilities, heatmap=heatmap_for(facilities))
    record = outcome.evidence.facilities[0]
    # 端点被吸附到 80 米外：这一条只进"道路端点路线证据"那一层，严格映射不放宽。
    assert record["poiStatus"] == "pending"
    assert record["poiReason"] == "endpoint_mapping_unconfirmed"
    assert record["evidenceGrade"] == "model"
    # 返回的距离留着（端点证据层里的数值），但判定是不作的。
    assert record["withinRule"] is None
    assert record["routeDistanceM"] == pytest.approx(1092.5, abs=1.0)
    # 端点、偏移和原因都留下了，读者能看出它为什么不算数：回来的终点确实停在离设施
    # 80 米的地方，不是"碰巧不相等"。
    assert record["destinationOffsetM"] == 80.0
    projection = LocalProjection(ORIGIN)
    gap = math.dist(projection.to_local(record["routeDestination"]),
                    projection.to_local(offset(950)))
    assert gap == pytest.approx(80.0, abs=0.5)
    assert outcome.evidence.failed == 1 and outcome.status == "partial"
    assert len(routes.sent) == 1


def test_route_to_facility_cannot_contradict_coverage_at_the_facility():
    facilities = [facility(0, 1150, "pharmacy"), facility(1, 300, "market")]
    heatmap = heatmap_for(facilities, status="covered")
    _routes, outcome = run(straight_routes(), facilities, heatmap=heatmap)
    records = {item["facilityId"]: item for item in outcome.evidence.facilities}
    # A destination has nearby services even if the request centre is far away.
    assert records["f-0"]["routeDistanceM"] == pytest.approx(1322.4, abs=1.0)
    assert records["f-0"]["withinRule"] is False
    assert outcome.evidence.conflicts == []
    for record in records.values():
        assert record["modelCell"] is None and record["modelStatus"] is None
        assert record["modelComparison"] == "not_comparable_origin_and_destination"
        assert record["conflict"] is False
    # 中心路线只说明中心这一点：报告必须写明它不核验任何一格的覆盖。
    assert any("不核验任何一格的服务覆盖" in note for note in outcome.evidence.notes)


def test_a_zero_distance_route_counts_only_when_it_is_the_point_that_was_asked():
    requested = offset(500)
    here = RouteObservation(requested, 0.0, observed_duration=0.0, endpoint_verified=True,
                            distance_m=0.0, route_origin=ORIGIN, route_destination=requested)
    elsewhere = RouteObservation(ORIGIN, 0.0, observed_duration=0.0, endpoint_verified=True,
                                 distance_m=0.0, route_origin=ORIGIN, route_destination=ORIGIN)
    # 设施就在问的那一点上：零距离是这条路线自己的结论。
    assert usable_route(here, requested) is True
    # 问的是别处却回来一个零：那是上游把"没走"当成了结论，不算数。
    assert usable_route(elsewhere, requested) is False
    # 端点没核实过、或者根本没有时长，也一样不参与判定。
    assert usable_route(RouteObservation(requested, 1.0, endpoint_verified=False), requested) is False
    assert usable_route(None, requested) is False
    assert within_rule(0.0) is True
    assert within_rule(None) is None and within_rule(-1.0) is None
    assert within_rule(float("inf")) is None
    assert within_rule(1000.0) is True      # 正好在线上，不是误差带
    assert within_rule(1050.0) is None      # 误差带内
    assert within_rule(1100.0) is None      # 带是闭区间：1000 ± 100 都算"判不准"
    assert within_rule(1101.0) is False     # 出带即判定
    assert within_rule(899.0) is True


# -- what the stage refuses to conclude --------------------------------------

def test_a_deployment_without_a_route_service_refuses_by_name():
    outcome = asyncio.run(verify_facilities(
        facilities=[facility(0, 300)], majors=MAJORS, zones=[], heatmap={}, entrances={},
        session=None, origin=ORIGIN))
    assert outcome.status == "not_integrated"
    assert outcome.evidence.status == "not_integrated"
    assert outcome.evidence.reason == NO_TRANSPORT
    assert outcome.network_requests == 0
    assert outcome.issues[0].code == "VERIFICATION_UNAVAILABLE"

    assert asyncio.run(verify_facilities(
        facilities=None, majors=MAJORS, zones=[], heatmap={}, entrances={},
        session=SyntheticRoutes(straight_routes()).session(FastPool(), budget=None,
                                                           deadline=time_out()),
        origin=ORIGIN)).evidence.reason == NO_FACILITIES


def test_a_candidate_that_could_not_be_answered_is_unverified_not_unreachable():
    attempts = []

    def respond(route_origin, destination, facility_id):
        attempts.append(facility_id)
        if facility_id == "f-0":
            return straight_routes()(route_origin, destination, facility_id)
        # 上游超时：没有距离、没有时长。它不是"走不通"，它是"没有结论"。
        return RouteObservation(destination, None, reason="timeout")

    facilities = [facility(0, 300), facility(1, 600), facility(2, 900)]
    _routes, outcome = run(respond, facilities, heatmap=heatmap_for(facilities))
    records = {item["facilityId"]: item for item in outcome.evidence.facilities}
    assert records["f-1"]["withinRule"] is None
    assert records["f-1"]["routeDistanceM"] is None
    assert records["f-1"]["reason"] == "timeout" and records["f-1"]["conflict"] is False
    assert outcome.evidence.failed == 2 and outcome.evidence.checked == 3
    assert outcome.status == "partial"
    # 三次都是中心可达性那一层的尝试；这个热力层没有位置，覆盖抽检一次也没问。
    assert outcome.evidence.queries == {"routeAttempts": 3, "candidates": 3, "checked": 3,
                                        "unverified": 0, "stopReason": None,
                                        "centerAttempts": 3, "spotAttempts": 0}


def test_a_pool_that_runs_out_leaves_the_rest_unverified_and_says_so():
    class Exhausting(FastPool):
        def attempt(self, deadline, *, budget=None, pool=None):
            if self.attempts >= 1:
                raise BudgetExhausted(pool, 1)
            self.attempts += 1
            return _Attempt()

    facilities = [facility(0, 300), facility(1, 600), facility(2, 900)]
    routes = SyntheticRoutes(straight_routes())
    outcome = asyncio.run(verify_facilities(
        facilities=facilities, majors=MAJORS, zones=[], heatmap=heatmap_for(facilities),
        entrances={}, session=routes.session(Exhausting(), budget=None, deadline=time_out()),
        origin=ORIGIN))
    # 桶用尽不是"走不通"：一条有结论，两条没有结论，而"没有结论"要写在证据里。
    assert outcome.evidence.checked == 1
    assert outcome.evidence.queries["unverified"] == 2
    assert outcome.evidence.queries["stopReason"] == "task_budget_exhausted"
    assert len(routes.sent) == 1
    assert any("未核验不等于走不通" in note for note in outcome.evidence.notes)
    assert outcome.status == "partial"


def test_a_run_that_retrieved_nothing_says_that_rather_than_reporting_zero_checked():
    _routes, outcome = run(straight_routes(), [], heatmap={})
    assert outcome.evidence.checked == 0 and outcome.status == "partial"
    assert any("没有可核验的对象" in note for note in outcome.evidence.notes)


def test_a_straight_line_never_answers_the_question_it_only_orders():
    # 一个永远不回答的传输：直线 900 米的候选走了 1200 米也没有结论 —— 任何
    # "在服务范围内"都只能来自返回的路线距离。
    def silent(route_origin, destination, facility_id):
        return RouteObservation(destination, None, reason="no_route_found")

    _routes, outcome = run(silent, [facility(0, 900)], heatmap={})
    record = outcome.evidence.facilities[0]
    assert record["straightLineM"] == pytest.approx(900, abs=1)
    assert record["withinRule"] is None and record["routeDistanceM"] is None


def test_the_duration_threshold_and_the_distance_rule_are_both_reported():
    # 一条 690 米的路线用 1000 秒走完：距离在规则内，时长超过 900 秒阈值。两个判定
    # 回答的是两个问题，所以两个都留下，谁也不覆盖谁。
    def slow(route_origin, destination, facility_id):
        # 上游收到的是归一化之后的坐标，回来的端点就是它（见 test_checkup_facilities）：
        # 回显原始浮点数会被严格映射判成"端点有偏移"，那样测的就不是时长阈值了。
        destination = normalize(destination)
        projection = LocalProjection(route_origin)
        distance = math.dist(projection.to_local(route_origin),
                             projection.to_local(destination)) * 1.15
        return RouteObservation(destination, 1000.0, observed_duration=1000.0,
                                endpoint_verified=True, route_origin=route_origin,
                                route_destination=destination, distance_m=distance)

    _routes, outcome = run(slow, [facility(0, 600)], heatmap={})
    record = outcome.evidence.facilities[0]
    assert record["withinRule"] is True
    assert record["poiStatus"] == "verified_unreachable"
    assert record["durationS"] == 1000.0
