"""Versioned checkup HTTP surface, separate from the two legacy task APIs."""
from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from ..algorithms.hybrid_isochrone.water_review import review_catalog
from .. import catalog
from ..catalog import major_of
from .. import catalog
from ..engines import STATUS_THRESHOLD_S
from ..poi.online import PROCESSING_STEP_LIMIT
from .manager import CheckupError, CheckupManager
from .models import ContinueCheckupRequest
from .models import (DEFAULT_POI_REQUESTS, DEFAULT_ROUTE_REQUESTS, DETAIL_ROUTE_REQUESTS,
                     DISTANCE_RULE, MAX_POI_REQUESTS, MAX_ROUTE_REQUESTS, QUERY_PADDING_M,
                     RULE_VERSION, CheckupCapabilities, CheckupLayer, CheckupRequest,
                     CheckupSnapshot, CheckupTaskView, FacilityExtensionDocument,
                     FacilityExtensionRequest, FacilityExtensionView, FacilityRetryRequest,
                     FacilityRetryView, FacilityRoute, RetainedCheckupView, SessionOpenRequest,
                     SessionView)

# Layers this release can serve, one per published group. The boundary arrives
# with the first stage, the retrieved facilities with the second, the assessment
# with the third, the route evidence with the fourth and the report with the
# fifth. ``report`` is a document rather than a shape: it is served from the same
# endpoint so a client can address any group of one revision the same way, and
# the layer says which of the two it is by which field it fills.
LAYER_IDS = ("isochrone", "facilities", "accessibility", "service_gaps", "heatmap",
             "verification", "report")
CACHE_CONTROL = "private, max-age=0, must-revalidate"
#: 含明细的响应一律不许落任何缓存。修订是不可变的，所以浏览器**可以**按 ETag 复用旧正文 ——
#: 而"不可变"只对内容成立：§5 B2 决策 2 的到期说的是这些明细从某一刻起不该再被读到，
#: 一个还躺在磁盘缓存里的响应会让它继续被读到。
NO_STORE = "no-store"
# The balance counts this application's own attempts. The browser SDK, other
# applications and the console's accounting sit outside it, so the interface
# must not present it as the account's remaining allowance.
QUOTA_LABEL = "本应用请求限制（不含浏览器 SDK、其他应用及旧接口流量）"


def _latest(manager: CheckupManager, task_id: str, revision: int | None = None):
    """The newest published revision, or an explicit not-ready refusal."""
    return manager.snapshot(task_id, revision)


def _retained(manager: CheckupManager, task_id: str) -> None:
    """明细已经到期时，这里给出**具名拒绝**，而不是少给一点明细。

    部分地提供明细是这里最坏的选项：读者看到一张设施更少的图，会把它读成"这次体检只找到
    这些"，而真实答案是"这一部分不再被授权查看"。所以到期就是到期，汇总另走
    ``/retained-result``，两条路各自说清自己是什么。
    """
    retention = manager.retention_of(manager.get(task_id))
    if not retention.details_available:
        raise CheckupError(
            410, "checkup_details_expired",
            "这次体检的明细已按保留期到期（"
            + {"session_closed": "浏览会话已结束", "superseded": "之后又完成了三次体检",
               "legacy": "它是保留期开始之前的数据",
               "cleared": "明细已被清理"}.get(retention.reason or "", "保留期已过")
            + "）：设施名称、UID、地址与坐标不再提供。结论、分数与汇总仍可读，见"
              " /api/v2/checkups/{task_id}/retained-result。")


def _layer_geometry(snapshot, layer_id: str):
    """One layer of one revision: ``(geometry, display_geometry, document)``.

    A group that is absent is a named refusal, never an empty shape — an empty
    collection drawn on a map reads as "nothing here", which is exactly the
    claim the stage refused to make.
    """
    if layer_id == "isochrone":
        # Stored revisions use the wire (camelCase) names; the snake_case read never matched.
        display = snapshot.isochrone.get("displayGeometry", snapshot.isochrone.get("display_geometry"))
        return snapshot.isochrone.get("geometry"), display, None
    if layer_id == "facilities":
        if snapshot.facilities is None:
            raise CheckupError(409, "checkup_facilities_not_ready", "设施结果尚未就绪")
        # Only the accepted facilities are drawn. Review candidates, exclusions and
        # quarantine are evidence for the report, not findings on a map.
        return {"type": "FeatureCollection", "coordinateSystem": "bd09ll",
                "features": [{"type": "Feature",
                              "geometry": {"type": "Point",
                                           "coordinates": [item["location"]["lng"],
                                                           item["location"]["lat"]]},
                              "properties": {
                                  "id": item["id"], "name": item["name"],
                                  "category": item["category"],
                                  "majorCategory": major_of(item["category"]),
                                  "address": item["address"],
                                  "classificationStatus": item["classificationStatus"],
                                  "possibleDuplicateGroup": item["possibleDuplicateGroup"]}}
                             for item in snapshot.facilities.facilities]}, None, None
    if layer_id == "accessibility":
        return _accessibility_layer(snapshot)
    if layer_id == "service_gaps":
        return _service_gaps_layer(snapshot)
    if layer_id == "heatmap":
        return _heatmap_layer(snapshot)
    if layer_id == "verification":
        return _verification_layer(snapshot)
    return _report_layer(snapshot)


def _accessibility_layer(snapshot):
    """The frozen assessment domain, with the per-category areas it was divided into.

    The domain is the denominator every area in the report is taken against, so it
    is drawable on its own: without it a reader cannot tell an assessable 100% from
    a 100% of a boundary that was never assessed at all.
    """
    evidence = snapshot.accessibility
    if evidence is None:
        raise CheckupError(409, "checkup_accessibility_not_ready", "服务覆盖结果尚未就绪")
    if evidence.domain is None:
        raise CheckupError(409, "checkup_domain_unavailable",
                           "本次体检没有可用的评估域，无法绘制服务覆盖图层")
    return {"type": "FeatureCollection", "coordinateSystem": "bd09ll",
            "features": [{"type": "Feature", "geometry": evidence.domain,
                          "properties": {
                              "status": evidence.status,
                              "gridStepM": evidence.grid_step_m,
                              "refinedStepM": evidence.refined_step_m,
                              "domainAreaM2": evidence.domain_area_m2,
                              "excludedAreaM2": evidence.excluded_area_m2,
                              "views": evidence.views,
                              "notes": evidence.notes,
                              "categories": [
                                  {"category": item.category, "supported": item.supported,
                                   "coveredM2": item.covered_m2, "gapM2": item.gap_m2,
                                   "unknownM2": item.unknown_m2,
                                   "unavailableReason": item.unavailable_reason}
                                  for item in evidence.categories]}}]}, None, None


def _service_gaps_layer(snapshot):
    """The grey-zone polygons, each carrying the reason it is one and what to check next.

    Counted geometry is metric; the drawn geometry is the same object in bd09ll.
    A zone whose drawn geometry is missing is refused rather than skipped: dropping
    it would draw a map that disagrees with the reported gap area.
    """
    gaps = snapshot.service_gaps
    if gaps is None:
        raise CheckupError(409, "checkup_service_gaps_not_ready", "服务盲区结果尚未就绪")
    features = []
    for zone in gaps.zones:
        if zone.display_geometry is None:
            raise CheckupError(409, "checkup_zone_geometry_incomplete",
                               f"灰区 {zone.id} 缺少展示几何，图层与报告面积不一致")
        features.append({"type": "Feature", "geometry": zone.display_geometry,
                         "properties": {key: value for key, value
                                        in zone.model_dump(mode="json", by_alias=True).items()
                                        if key not in ("geometry", "displayGeometry")}})
    return {"type": "FeatureCollection", "coordinateSystem": "bd09ll", "features": features,
            "properties": {"status": gaps.status, "gapAreaM2": gaps.gap_area_m2,
                           "compositeAreaM2": gaps.composite_area_m2,
                           "byCategoryM2": gaps.by_category_m2,
                           "obstacleLayerAvailable": gaps.obstacle_layer_available,
                           "notes": gaps.notes}}, None, None


def _heatmap_layer(snapshot):
    """Measured walking distances, per category, as points.

    Display only: the points show where a distance was measured, never a coverage
    verdict — those come from the grid, and the numbers from the wall charts.
    """
    heat = snapshot.heatmap
    if heat is None:
        raise CheckupError(409, "checkup_heatmap_not_ready", "热力数据尚未就绪")
    features = [{"type": "Feature",
                 "geometry": {"type": "Point", "coordinates": [point["lng"], point["lat"]]},
                 "properties": {"category": category, "cell": point["cell"],
                                "distanceM": point["distanceM"], "status": point["status"],
                                "nearestFacility": point.get("nearestFacility"),
                                **({"reason": point["reason"]} if point.get("reason") else {})}}
                for category, points in sorted(heat.categories.items())
                for point in points]
    return {"type": "FeatureCollection", "coordinateSystem": "bd09ll", "features": features,
            "properties": {"metric": heat.metric, "estimated": heat.estimated,
                           "stepM": heat.step_m, "domain": heat.domain, "notes": heat.notes,
                           # 水体用的是哪份数据、复核过哪里：地图据此画出冲突与底图误绘。
                           # 早于水系复核的修订为 null，界面据此标出"旧版本"。
                           "water": (None if snapshot.water is None
                                     else snapshot.water.model_dump(mode="json", by_alias=True))},
            }, None, None


def _verification_layer(snapshot):
    """问过路的那几家设施，以及每一家问回来的结果。

    画的是设施自己的位置，不是路线端点：端点可能被吸附到几十米外的道路上，把它当成
    "设施在这里"就等于静默挪动了设施。端点、偏移、判定和冲突与否都在属性里，所以图上
    少一个点是不可能的，图上多一个点也是不可能的。
    """
    evidence = snapshot.verification
    if evidence is None:
        raise CheckupError(409, "checkup_verification_not_ready", "核验结果尚未就绪")
    if evidence.status == "not_integrated":
        # 具名拒绝，不是一个空集合：空集合在地图上读作"这里都核验过了、没问题"。
        raise CheckupError(409, "checkup_verification_not_integrated",
                           "本次体检没有进行现实核验，核验图层不可用")
    features = []
    for record in evidence.facilities:
        location = record.get("location")
        if location is None:
            raise CheckupError(409, "checkup_verification_geometry_incomplete",
                               f"核验记录 {record['facilityId']} 缺少设施位置，"
                               f"图层与已冻结的证据不一致")
        features.append({"type": "Feature",
                         "geometry": {"type": "Point",
                                      "coordinates": [location["lng"], location["lat"]]},
                         "properties": {key: value for key, value in record.items()
                                        if key != "location"}})
    return {"type": "FeatureCollection", "coordinateSystem": "bd09ll", "features": features,
            "properties": {"status": evidence.status, "provider": evidence.provider,
                           "checked": evidence.checked, "failed": evidence.failed,
                           "unresolved": evidence.unresolved, "conflicts": evidence.conflicts,
                           "queries": evidence.queries, "notes": evidence.notes}}, None, None


def _report_layer(snapshot):
    """The report document. Not a shape, so it fills ``document`` and leaves both
    geometry fields null — the client asked for this group by name and a silently
    empty map would not be the answer."""
    if snapshot.report is None:
        raise CheckupError(409, "checkup_report_not_ready", "体检报告尚未就绪")
    return None, None, snapshot.report.model_dump(mode="json", by_alias=True)


#: 会话标识走请求头而不是请求体：它属于**调用方**，不属于某一次体检的参数。放进
#: ``CheckupRequest`` 会让它进入指纹 —— 于是同一个请求标识在不同会话里重发就变成"参数变了"，
#: 而那不是参数，是"谁在问"。它也因而不会出现在任何一份生成契约的请求体里。
SESSION_HEADER = "X-Checkup-Session"


def session_id_of(request: Request) -> str | None:
    """请求头里的会话标识；没有就返回 None（这次体检没有会话归属）。"""
    value = request.headers.get(SESSION_HEADER)
    if value is None or not value.strip():
        return None
    return value.strip()[:100]


def checkup_router(manager: CheckupManager):
    router = APIRouter(prefix="/api/v2/checkups", tags=["checkups"])

    @router.post("", status_code=202, response_model=CheckupTaskView)
    async def create(payload: CheckupRequest, request: Request):
        view, _created = manager.submit(payload, session_id=session_id_of(request))
        return view

    # §5 B2 决策 2：会话是保留期的第一条期限。客户端在 localStorage / sessionStorage 里保管
    # 两个标识，服务端只回答"这个会话现在还算数吗"。它挂在 checkups 前缀下，因为它是这些
    # 结果的生命周期的一部分，而不是一个独立的账号概念。
    @router.post("/sessions", response_model=SessionView)
    async def open_session(payload: SessionOpenRequest):
        return manager.open_session(payload)

    @router.post("/sessions/{session_id}/tabs/{tab_id}/heartbeat", response_model=SessionView)
    async def heartbeat(session_id: str, tab_id: str):
        return manager.heartbeat(session_id, tab_id)

    @router.post("/sessions/{session_id}/tabs/{tab_id}/close", response_model=SessionView)
    async def close_tab(session_id: str, tab_id: str):
        # 关闭是**记录**，不是判决：会话是否到期由"最后离开的时刻 + 宽限"决定，
        # 所以一个标签页关掉之后，另一个还开着的标签页照样能续租。
        return manager.close_tab(session_id, tab_id)

    # Declared before the bare task route so the literal segment is not read as
    # a task id, which would otherwise shadow it.
    @router.get("/by-request/{client_request_id}", response_model=CheckupTaskView)
    async def by_request(client_request_id: str):
        return manager.view(manager.by_request(client_request_id)).model_copy(
            update={'client_request_id': client_request_id})

    @router.get("/{task_id}", response_model=CheckupTaskView)
    async def status(task_id: str):
        return manager.view(manager.get(task_id))

    @router.get("/{task_id}/result", response_model=CheckupSnapshot)
    async def result(task_id: str, revision: int | None = None):
        # 客户端一直带着 `?revision=`（重试会为同一次体检发布新修订），而这里曾经不看它：
        # 想要第 3 版的人会拿到第 4 版，并且因为响应里的版本号与请求不符而被判为"契约异常"。
        _retained(manager, task_id)
        snapshot, _stored = _latest(manager, task_id, revision)
        return JSONResponse(status_code=200, headers={"Cache-Control": NO_STORE},
                            content=snapshot.model_dump(mode="json", by_alias=True))

    # 到期之后仍然可以读的东西：结论、分数与汇总，以及"为什么只剩这些"。它与 /result 分开，
    # 因为它们回答的是两个不同的问题 —— 合成一个就等于把"明细还在"和"明细没了"混成一个
    # 可以被忽略的细节。
    @router.get("/{task_id}/retained-result", response_model=RetainedCheckupView)
    async def retained_result(task_id: str, revision: int | None = None):
        view = manager.retained(task_id, revision)
        # 同样不许缓存：它里面的 `retention` 是**现在**的状态（还没清 / 已经清了），
        # 一个被缓存下来的副本会把"即将到期"一直显示成"还没到期"。
        return JSONResponse(status_code=200, headers={"Cache-Control": NO_STORE},
                            content=view.model_dump(mode="json", by_alias=True))
    @router.post('/{task_id}/continue', status_code=202, response_model=CheckupTaskView)
    async def continue_checkup(task_id: str, payload: ContinueCheckupRequest):
        return manager.continue_checkup(task_id, payload)

    @router.get("/{task_id}/layers/{layer_id}")
    async def layer(task_id: str, layer_id: str, request: Request, revision: int | None = None):
        if layer_id not in LAYER_IDS:
            raise CheckupError(404, "checkup_layer_not_found", "未知图层")
        # 图层全是明细：设施图层是设施本身，其余图层是照着这次评估画出来的形状。
        # 到期之后整组都不再提供，免得出现"面积还在、设施没了"的图。
        _retained(manager, task_id)
        snapshot, stored = _latest(manager, task_id, revision)
        # The validator names the layer as well as the revision: two layers of one
        # revision are different representations, and a shared digest would let a
        # client revalidate a layer it never fetched.
        etag = f'"{stored["result_hash"]}-{layer_id}"'
        headers = {"ETag": etag, "Cache-Control": CACHE_CONTROL}
        # A revision is immutable, so a matching validator always means the
        # caller's copy is still current.
        if request.headers.get("if-none-match") in (etag, "*"):
            return Response(status_code=304, headers=headers)
        geometry, display, document = _layer_geometry(snapshot, layer_id)
        body = CheckupLayer(
            layer_id=layer_id, revision=stored["revision"], geometry=geometry,
            # Drawing shell only; never a counting or assessment surface.
            display_geometry=display, document=document, result_hash=stored["result_hash"])
        return JSONResponse(status_code=200, headers=headers,
                            content=body.model_dump(mode="json", by_alias=True))

    @router.post("/{task_id}/cancel", status_code=202, response_model=CheckupTaskView)
    async def cancel(task_id: str):
        return manager.cancel(task_id)[0]

    # 按需补查是独立资源，不是原任务的新修订：它自己的标识、自己的预算，
    # 失败或被取消都不会改动已经发布的报告和评分。
    @router.post("/{task_id}/facility-extensions", status_code=202,
                 response_model=FacilityExtensionView)
    async def create_extension(task_id: str, payload: FacilityExtensionRequest):
        return manager.submit_extension(task_id, payload)

    # §5 B2 决策 1 的重试：与补查是两个资源，因为定稿之后做的事不同 —— 补查产出并列的
    # 一份结果、父任务一字不改；重试为**同一次体检**发布新修订，因为"这次查完了没有"
    # 必须体现在这一份报告里，而不是旁边多一个文件。
    @router.post("/{task_id}/retries", status_code=202, response_model=FacilityRetryView)
    async def create_retry(task_id: str, payload: FacilityRetryRequest):
        return manager.submit_retry(task_id, payload)

    @router.get("/{task_id}/retries", response_model=list[FacilityRetryView])
    async def list_retries(task_id: str):
        return manager.retries(task_id)

    @router.get("/{task_id}/retries/{retry_id}", response_model=FacilityRetryView)
    async def retry_status(task_id: str, retry_id: str):
        return manager.retry(task_id, retry_id)

    @router.post("/{task_id}/retries/{retry_id}/cancel", status_code=202,
                 response_model=FacilityRetryView)
    async def cancel_retry(task_id: str, retry_id: str):
        return manager.cancel_retry(task_id, retry_id)

    @router.get("/{task_id}/facility-extensions", response_model=list[FacilityExtensionView])
    async def list_extensions(task_id: str):
        return manager.extensions(task_id)

    @router.get("/{task_id}/facility-extensions/{extension_id}", response_model=FacilityExtensionView)
    async def extension_status(task_id: str, extension_id: str):
        return manager.extension(task_id, extension_id)

    @router.get("/{task_id}/facility-extensions/{extension_id}/result",
                response_model=FacilityExtensionDocument)
    async def extension_result(task_id: str, extension_id: str):
        return manager.extension_document(task_id, extension_id)

    @router.post("/{task_id}/facility-extensions/{extension_id}/cancel", status_code=202,
                 response_model=FacilityExtensionView)
    async def cancel_extension(task_id: str, extension_id: str):
        return manager.cancel_extension(task_id, extension_id)

    @router.post("/{task_id}/routes/{facility_id}", response_model=FacilityRoute)
    async def facility_route(task_id: str, facility_id: str):
        # 路线详情是明细（它是一条从设施出发的路线），到期之后同样不提供。
        _retained(manager, task_id)
        # A route detail is only ever issued for an identifier this task itself
        # retrieved: an id from anywhere else is not a facility of this checkup,
        # which is a different answer from "the facilities are not there yet".
        # Both of those, and the spending, live in the manager (§3.3).
        return await manager.detail_route(task_id, facility_id)

    return router


def capabilities_router(manager: CheckupManager, settings, offline=None):
    router = APIRouter(prefix="/api/v2", tags=["checkups"])

    @router.get("/capabilities", response_model=CheckupCapabilities)
    async def capabilities():
        graph_configured = settings.osm_graph_cache_path is not None \
            and settings.osm_graph_cache_path.is_file()
        graph_state = "unavailable" if not graph_configured else (None if offline is None else offline.state)
        return CheckupCapabilities(
            category_directory_version=catalog.VERSION,
            category_directory=catalog.major_directory(),
            engines=[item.model_dump(mode="json", by_alias=True)
                     for item in manager.registry.capabilities()],
            # ``DistanceRule`` stays in its own snake-case form; it is the same
            # rule object the legacy contracts and the assessment use.
            rules={"distance": DISTANCE_RULE.model_dump(mode="json"), "ruleVersion": RULE_VERSION,
                   "statusThresholdSeconds": STATUS_THRESHOLD_S,
                   "assessmentScope": "isochrone",
                   "bandIsNotRadiusExpansion": True},
            data_versions={"osm": settings.osm_data_version,
                           "projection": settings.osm_metric_crs},
            water_reviews=[item for item in review_catalog(getattr(settings, "water_review_dir", None))
                           if item["osmDataVersion"] == settings.osm_data_version],
            coverage={"metricCrs": settings.osm_metric_crs, "queryPaddingM": QUERY_PADDING_M,
                      "graphConfigured": graph_configured,
                      "graphState": graph_state,
                      "coverageBoundaryConfigured": settings.osm_coverage_boundary_path is not None,
                      "completeDirectory": False},
            budgets={"poiRequests": DEFAULT_POI_REQUESTS, "routeRequests": DEFAULT_ROUTE_REQUESTS,
                     "detailRouteRequests": DETAIL_ROUTE_REQUESTS,
                     "maxPoiRequests": MAX_POI_REQUESTS, "maxRouteRequests": MAX_ROUTE_REQUESTS,
                     # 每块每小类先取主关键词一次，所以一次检索的首轮页数是"分块数 × 小类数"。
                     # 分块数由圈面包络决定（最多 4），请求体里算不出来，这里给出小类数与块数
                     # 上界，客户端据此在提交前估计够不够，而不是提交后拿到一个失败任务。
                     "poiMinorCategories": {
                         "default": len(catalog.poi_keys(catalog.default_analysis_majors())),
                         "all": len(catalog.poi_keys(catalog.majors())),
                     },
                     "poiBlocksUpperBound": 4,
                     # 这个乘积是**首轮估计**，不是总请求数的下界：几何确定后实际分块数可能
                     # 更少，而翻页、细分与重试一定需要更多。旧字段保留下来但报 false —— 它
                     # 此前把估计说成了下界，那正是要修掉的说法。
                     "poiRequestsIsLowerBound": False,
                     "poiFirstRoundIsAnEstimate": True},
            # 预检与执行共用的口径：首轮页数怎么估、可复用多少、什么在限制新增调用。
            poi_planning={"processingStepLimit": PROCESSING_STEP_LIMIT,
                          "networkBudgetIsSeparate": True,
                          "initialPlanReportsCacheReuse": True,
                          "initialPlanIsReservation": False},
            # §3.4 的跨任务复用窗口。未配置时缓存只在同一个任务内复用，
            # 所以重复体检同一片区域不会省下任何请求；这个值要能被客户端看到。
            cache={"freshnessSeconds": settings.cache_freshness_seconds,
                   "crossTaskReuse": settings.cache_freshness_seconds is not None,
                   # 页面缓存是进程内存：重启即失效，也不跨进程共享。
                   "processLocal": True},
            # 类别选择器的唯一来源：展示组、十个大类、每类检索小类数、核心口径。
            facility_categories=catalog.facility_categories(),
            # Reported per request rather than cached: the tier switches at a
            # wall-clock instant and the day turns at Shanghai midnight.
            quota={**manager.quota.balance(), "label": QUOTA_LABEL})

    return router
