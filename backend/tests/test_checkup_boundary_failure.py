"""端到端：成圈没有立住时，报告说的是"没有范围"，不是"没有设施"。

两次真实体检（2026-10-10 10:27:00 与 10:29:17，中心 31.299213,121.50847，预算 200）的
修订是同一个形状：成圈花掉全部 200 次、``geometry: null``、``quality: insufficient``，
设施检索一次也没派发（POI 桶 spent 0／本轮默认 1200），而可达性与核验把每个类别的原因写成
``facility_stage_unavailable``。那份报告读起来像"设施检索失败了" —— 真正失败的却是成圈，
读者会去查一个没有出错的阶段。成圈内部的拒收原因当时也没有留下，修订里只剩一个 budget。

这里用一条**从不返回端点**的路线替身重现那个形状：所有观测都被求解器拒收，圈面立不起来。
路网与设施传输都是离线替身，运行不出机器。断言的是整条链上的七件事：可达性与核验的原因
指向边界、设施与核验零派发、额度一次不动、评分与灰区都不给、成圈诊断写进修订、环面装配的
具名原因与"没量过"的计数也在这一版里、逐条观测证据落在任务明细目录里且不含密钥或原始报文。
"""
import json
from dataclasses import replace

from fastapi.testclient import TestClient

from app.algorithms.baidu_e82 import EndpointAnalyticProvider
from app.checkups import accessibility_stage as stage
from app.checkups.verification_stage import NO_BOUNDARY as VERIFICATION_NO_BOUNDARY
from app.engines.baidu_e82 import EVIDENCE_NAME, EVIDENCE_SCHEMA
from test_checkup_facilities import (SECRET, SyntheticPlaces, SyntheticRoutes, body, document,
                                     every_page, make_app, run, straight_routes)
# 与 ``test_checkup_report`` 同一套路网替身：可达性阶段先看路网在不在，再看圈面。
from test_checkup_report import VERSION, OfflineGraph, road_grid


class Endpointless(EndpointAnalyticProvider):
    """上游每次都回时长，却一次也不回端点：求解器一条观测都用不上。"""

    def __init__(self, origin):
        super().__init__(origin, lambda x, y: (x * x + y * y) ** .5 / 1.2)

    async def query_walking_time(self, origin, destination, deadline):
        observation = await super().query_walking_time(origin, destination, deadline)
        return replace(observation, route_origin=None, route_destination=None,
                       endpoint_verified=False)


def endpointless(origin):
    """引擎的路线传输替身：它取代 ``provider_factory``，不让任何请求出机器。"""
    return Endpointless(origin)


def test_a_boundary_that_never_stood_up_is_not_reported_as_a_facility_failure(tmp_path):
    places = SyntheticPlaces(every_page([(121.50847, 31.299213)]))
    routes = SyntheticRoutes(straight_routes())
    app = make_app(tmp_path, places, routes=routes, osm_data_version=VERSION)
    app.state.checkups.offline = OfflineGraph(road_grid())
    app.state.checkups.registry.get("baidu_e82").provider_factory = endpointless

    with TestClient(app) as client:
        task_id, view = run(client, body(center={"lng": 121.50847, "lat": 31.299213}))
        assert view["status"] == "completed", view
        assert view["stage"] == "ready" and view["revision"] == 5
        frozen = document(client, task_id)

    # 成圈这一趟：几何是空的，质量因此是"没有建立"。
    assert frozen["isochrone"]["geometry"] is None
    assert frozen["isochrone"]["quality"] == "insufficient"
    assert frozen["businessStatus"] == "insufficient"
    # 设施检索一次也没派发：它连查询范围都没有。额度一分未动是关键 —— 上一条把它读成
    # "成圈花光了设施预算"的猜测在这里就被证伪了。
    assert frozen["facilities"] is None
    assert frozen["facilitiesStatus"] == "failed"
    assert frozen["trace"]["budgets"]["poi"] == {"limit": 1200, "spent": 0}
    assert places.sent == []
    # 原因写边界，不写下游的设施阶段。
    assert frozen["accessibility"]["status"] == "failed"
    assert {item["unavailableReason"] for item in frozen["accessibility"]["categories"]} \
        == {stage.NO_BOUNDARY}
    assert frozen["verification"]["reason"] == VERIFICATION_NO_BOUNDARY
    assert frozen["verification"]["checked"] == 0
    assert routes.sent == []
    # 没有评估域就没有分数、灰区与热力：不是 0%，是不给结论。
    assert frozen["scores"] is None and frozen["serviceGaps"] is None
    assert frozen["heatmap"] is None
    assert frozen["report"]["gaps"]["status"] == "failed"

    # 设施那一版自己的说明仍然准确：它说的是"成圈结果没有几何"。
    poi_revision = app.state.checkups.store.revision(task_id, 2)["snapshot"]
    message = next(issue["message"] for issue in poi_revision["warnings"]
                   if issue["code"] == "FACILITIES_UNAVAILABLE")
    assert "成圈结果没有几何" in message

    # 成圈诊断：为什么这条圈面立不起来，写在这一版里，不再只剩一个 budget。
    diagnostics = frozen["isochrone"]["statistics"]["boundaryDiagnostics"]
    assert diagnostics["observations"]["accepted"] == 0
    assert diagnostics["observations"]["rejectedReasons"] == {
        "missing_endpoints": diagnostics["observations"]["rejected"]}
    assert diagnostics["directions"]["byReason"] == {"missing_endpoints": 16}
    assert diagnostics["completion"]["resolutionReached"] is False
    # 环面根本没合起来，原因是具名的：一条观测都用不上时连三个方向都凑不齐。
    ring = diagnostics["ringAssembly"]
    assert ring["assembled"] is False and ring["reason"] == "insufficient_brackets"
    assert ring["localizedNodes"] == 0 and ring["maxGapRadians"] is None
    # 没有环面就没有补丁窗口，那两个计数没有被量过：这里必须是 null，不能是 0 ——
    # 0 会被读成"未决边一条都没有"，而这一趟连边都还没有。
    assert diagnostics["completion"]["unresolvedEdges"] is None
    assert diagnostics["completion"]["pendingUnattemptedEdges"] is None
    assert frozen["isochrone"]["statistics"]["unfinished_boundary"] is None

    # 逐条观测证据落在任务明细目录里，与修订文件同时到期。
    assert frozen["isochrone"]["statistics"]["observationEvidence"] == EVIDENCE_NAME
    evidence_path = app.state.checkups.store.root / "tasks" / task_id / EVIDENCE_NAME
    raw = evidence_path.read_text(encoding="utf-8")
    saved = json.loads(raw)
    assert saved["schemaVersion"] == EVIDENCE_SCHEMA and saved["taskId"] == task_id
    assert len(saved["observations"]) == diagnostics["observations"]["logged"] > 0
    assert all(row["rejection"] == "missing_endpoints" for row in saved["observations"])
    assert all(row["actualDestination"] is None for row in saved["observations"])
    assert all(row["requestDestination"] is not None for row in saved["observations"])
    # 存的是观测，不是报文：密钥与 URL 都不在里面。
    assert SECRET not in raw and "http" not in raw.lower()


def test_a_boundary_run_survives_evidence_that_cannot_be_saved(tmp_path, monkeypatch):
    """证据存不下来，不该让一次已经付过费的成圈作废；但也不能在修订里装作存过了。"""
    places = SyntheticPlaces(every_page([(121.50847, 31.299213)]))
    routes = SyntheticRoutes(straight_routes())
    app = make_app(tmp_path, places, routes=routes, osm_data_version=VERSION)

    def refuse(*args, **kwargs):
        raise OSError("no space left on device")

    monkeypatch.setattr("app.engines.baidu_e82.atomic_dump", refuse)
    with TestClient(app) as client:
        task_id, view = run(client, body(center={"lng": 121.50847, "lat": 31.299213}))
        frozen = document(client, task_id)

    # 圈面照常建立、照常发布：缺的只是一份明细。
    assert view["status"] == "completed", view
    assert frozen["isochrone"]["geometry"] is not None
    assert frozen["facilitiesStatus"] == "complete"
    statistics = frozen["isochrone"]["statistics"]
    assert "observationEvidence" not in statistics
    # 诊断仍在这一版里：它随修订走，不依赖那个文件。
    assert statistics["boundaryDiagnostics"]["observations"]["accepted"] > 0
    # 有名字才读得出"少了什么"；报告里一条警告，不是一次静默降级。
    assert "boundary_evidence_not_saved" in frozen["isochrone"]["warnings"]
    assert any(issue["message"] == "boundary_evidence_not_saved"
               for issue in frozen["warnings"])
    assert not (app.state.checkups.store.root / "tasks" / task_id / EVIDENCE_NAME).exists()
