"""§5–§7.1 可达性阶段：固定评估域、逐类别服务场、灰区清单与热力数据面。

这是体检的核心阶段：它把"设施检索拿到了什么"变成"哪一片地方在 1000 米步行距离内
有服务、哪一片没有、哪一片还不知道"。六条口径，每条都有一个更省事但会给出相反结论
的做法被排除：

* **评估域先冻结，再评估。** 分母 A 是评估域的面积，先算出来随快照冻结；网格只评估
  域内的格，叶格面积之和必须等于 A。不闭合就不给该类别分数结论（§7.1），绝不用
  ``C + G + U`` 反推 A —— 那样删掉未评估的格就能把分数抬上去。
* **米制框架只有一个。** 边界、设施、路网必须在同一个投影里。路网用数据集自己的米制
  CRS（``store.projection``），所以边界与设施都在这里投影；POI 查询域用的"以请求中心
  为原点"的局部框架是另一套坐标，两者混用会让所有入口整体偏移几十公里。
* **一次搜索两处使用。** 每格的距离、最近已知设施、可达面都来自同一次反向多源搜索，
  判定与展示因此不可能互相矛盾（§5.4）。
* **缺基础设施不是零分。** 没有 OSM 图、没有边界、面积不闭合，都报 ``supported=False``
  加原因，不报 0%（§6.4）。有分类、没设施则相反：那是观测到的零，覆盖区间如实报成
  [0%, 100%]，不伪装成"无法确定"。
* **检索没跑完就不是观测。** ``query_status`` 不是 ``complete`` 时，只有真的查到设施
  的大类才评估：对没查到的类别给一个区间，就是把"没查"写成"没有"。
* **灰区只由 gap 产生。** unknown 不进灰区、不计为缺失（§6.5）；入口未解决的类别整类
  未知 —— 把"设施位置不明"写成"这里没有服务"是最严重的一类误判。
"""
from dataclasses import dataclass, field as dataclass_field

from shapely import make_valid, segmentize
from shapely.geometry import LineString, Point, shape
from shapely.ops import transform, unary_union

from .. import catalog, service_rules
from ..accessibility.field import ServiceField, VerifiedPoint
from ..accessibility.grid import (COVERED, GAP, MAX_LEAF_CELLS, UNKNOWN, cells_covering,
                                  explore)
from ..accessibility.service_graph import (ENTRANCE_LIMIT_M, SEARCH_CUTOFF_M, PointAttachments, build_views,
                                           coverage_from_settled, resolve_entrances)
from ..accessibility.zones import CellRecord, merge_zones
from ..algorithms.hybrid_isochrone.hard_obstacles import load_obstacles
from ..contracts import Issue
from ..geo.coordinates import wgs84_to_bd09
from ..scoring import (CategoryAreas, DEFAULT_SCORING_CATEGORIES, area_tolerance, category_score,
                       category_weights, interval_degenerates, overall_score)
from .models import (DISTANCE_RULE, AccessibilityEvidence, CategoryCoverage, HeatmapEvidence,
                     ScoreEvidence, ServiceGapsEvidence, ServiceZone, WaterDataEvidence)
from .water_data import water_evidence

#: 障碍判定的容差（米）：与 Hybrid 硬障碍层同一口径，0.1 米以下的重叠算相切。
OBSTACLE_TOUCH_TOLERANCE_M = 0.1
#: 障碍层载入范围在评估域外扩的距离（米）：圈外设施也是服务源，它们的入口可能落在
#: 评估域外一整个搜索截止距离之远，连接段的穿水判定都要用到（见 ``service_rules``）。
OBSTACLE_MARGIN_M = service_rules.OBSTACLE_MARGIN_M
#: 边界折线加密间距（度）：BD09 与米制之间的变换是非线性的，长边不加密会被拉成一条弦。
BOUNDARY_SEGMENT_DEGREES = 1e-4

#: 缺基础设施或域不可用：都是"没有结论"，不是"没有服务"（§6.4）。
NO_GRAPH = "osm_graph_unavailable"
NO_BOUNDARY = "no_boundary_geometry"
NO_FACILITIES = "facility_stage_unavailable"
QUERY_INCOMPLETE = "facility_query_incomplete"
DOMAIN_TOO_LARGE = "assessment_domain_exceeds_grid_limit"
AREA_MISMATCH = "area_partition_mismatch"
EMPTY_DOMAIN = "empty_assessment_domain"


@dataclass
class CategoryEvaluation:
    """一个类别的评估结果：三态面积、证据、以及要写进快照的那部分。"""
    category: str
    supported: bool
    areas: CategoryAreas
    cells: dict = dataclass_field(default_factory=dict)
    entrances: dict = dataclass_field(default_factory=dict)
    #: 解析出来的入口本身，供核验阶段按"未知门禁/入口偏移"排候选；不进快照。
    entrance_items: tuple = ()
    records: tuple = ()
    heat: tuple = ()
    coverage: dict | None = None
    unavailable_reason: str | None = None


@dataclass
class AccessibilityOutcome:
    """阶段产出。任何一份证据为 None 都有原因说明为什么。"""
    accessibility: AccessibilityEvidence | None
    service_gaps: ServiceGapsEvidence | None
    heatmap: HeatmapEvidence | None
    scores: ScoreEvidence | None
    status: str
    issues: list = dataclass_field(default_factory=list)
    #: ``{facility_id: Entrance}``：核验阶段要用的入口证据，只在本进程内传递。
    entrances: dict = dataclass_field(default_factory=dict)
    #: 这一版用到的水体障碍的来源与复核范围。
    water: WaterDataEvidence | None = None


def _polygonal(geometry):
    """多边形的各个部分；空几何与线、点在这里被去掉。"""
    parts = getattr(geometry, "geoms", None) or [geometry]
    return [part for part in parts if part.geom_type in ("Polygon", "MultiPolygon")
            and not part.is_empty]


def metric_region(geometry: dict, projection):
    """bd09ll 的公开几何 → 路网所在米制框架里的几何（§5.5）。

    先加密后变换：BD09 与米制之间的偏移是非线性的，只在折线顶点上做变换会把长边拉成
    一条弦，评估域因此少一块 —— 而缺口恰好落在评估域边界上，看起来就像一条服务盲区。
    """
    try:
        densified = segmentize(shape(geometry), BOUNDARY_SEGMENT_DEGREES)
        return make_valid(transform(lambda x, y, z=None: projection.origin((x, y)), densified))
    except Exception:
        raise ValueError(NO_BOUNDARY) from None


def engine_unknown_region(geometry: dict | None, projection):
    """成圈算法标出的不确定区（bd09ll）→ 米制几何；没有就返回 None。

    这块面积**不进评估域，也不从评估域里扣**（§6.1）。它是一句关于成圈质量的说明：
    "这一片我立不住"，而不是"这一片没有服务"。报告里按原样报出来，让它可见，但不许它
    改动分母 A。
    """
    if geometry is None:
        return None
    region = unary_union(_polygonal(metric_region(geometry, projection)))
    return None if region.is_empty else region


def assessment_domain(geometry: dict | None, *, projection, coverage=None):
    """§6.1 的固定报告评估域：计算圈面，扣掉数据集覆盖不到的部分。

    返回 ``(domain, exclusions)``，``exclusions`` 是 ``{原因: 面积}``。``coverage`` 是数据
    集的覆盖范围，它是**窗口**：评估域只保留窗口以内的部分。超出窗口的面积单列出来，因为
    那是"不在结论范围内"，不是"没有服务"。

    成圈算法自己标的那块不确定区**不在这里扣**（方案 §6.1）：E8.2 在缺少未知几何时会拿
    整个计算域当 ``unknownRegion``，机械相减会把评估域整块扣没 —— 真实的一次百度体检因此
    跑出过 A = 0.00035 m²、覆盖率 0%、未知 99.9999%，而报告读起来像"这一带没有服务"。
    即便拿到了标定过的未知面，它也只在可达面之外（两个引擎都是 ``unknown.difference(
    geometry)`` 造出来的），减与不减都改不动 A。评估域里剩下的部分本来就该保留为未知、
    不从分母消失 —— 那是 U 的活（§5.6），不是 A 的。
    """
    if geometry is None:
        raise ValueError(NO_BOUNDARY)
    region = unary_union(_polygonal(metric_region(geometry, projection)))
    if region.is_empty or region.area <= 0:
        raise ValueError(EMPTY_DOMAIN)
    exclusions: dict[str, float] = {}
    if coverage is not None:
        inside = region.intersection(coverage)
        excluded = max(0.0, region.area - inside.area)
        if excluded > 0:
            exclusions["coverage_beyond_dataset"] = excluded
        region = unary_union(_polygonal(inside))
        if region.is_empty or region.area <= 0:
            raise ValueError(EMPTY_DOMAIN)
    return region, exclusions


def public_point(projection, xy) -> tuple[float, float]:
    """米制点 → bd09ll。热力点是点，不需要 :meth:`public_geometry` 的加密与修复。"""
    return wgs84_to_bd09(*projection.inverse.transform(xy[0], xy[1], errcheck=True))


def public_geometry(projection, geometry):
    """米制几何 → bd09ll 几何；无效几何先修，微小偏移允许修复，大的拓扑变化不允许。"""
    return projection.public_geometry(make_valid(geometry), repair_roundoff=True)


def metric_facilities(items, projection, *, major: str | None = None):
    """把 POI 阶段接受的设施记录投影到路网框架（§5.3 的输入）。

    ``resolve_entrances`` 的契约是**米制坐标**：它直接把 ``location`` 交给路网的空间
    索引。所以"bd09ll → 米制"必须在进它之前完成，而且只做一次 —— 在里面再做一次等于
    把坐标落到第二套框架上，症状是"全城设施都没有合法接入"。
    """
    selected = []
    for item in items:
        if major is not None and catalog.major_of(item.get("category")) != major:
            continue
        record = {"id": item.get("id"), "category": item.get("category")}
        for key in ("location", "navigationLocation"):
            value = item.get(key)
            if not isinstance(value, dict):
                continue
            try:
                x, y = projection.origin((float(value["lng"]), float(value["lat"])))
            except (KeyError, TypeError, ValueError):
                continue
            # 键名沿用 lng/lat 是因为 ``resolve_entrances`` 读的就是它们；这里装的是米。
            record[key] = {"lng": x, "lat": y}
        if "location" in record or "navigationLocation" in record:
            selected.append(record)
    return selected


class _Obstacles:
    """硬障碍的两种用法：入口连接段是否穿水体，以及共享边是否被水体隔断。"""

    def __init__(self, local=None):
        self.local = local
        self.failed = 0

    @property
    def water(self):
        return None if self.local is None else self.local.water

    @property
    def available(self) -> bool:
        water = self.water
        return water is not None and not water.is_empty

    @property
    def conflicts(self):
        """复核认定"来源互相矛盾、没有裁决"的面：不是水，也不是陆地。"""
        conflicts = None if self.local is None else getattr(self.local, "conflicts", None)
        return None if conflicts is None or conflicts.is_empty else conflicts

    def conflict_intersects(self, geometry) -> bool:
        try:
            return self.conflicts.intersects(geometry)
        except Exception:
            self.failed += 1
            return True

    def intersects(self, geometry) -> bool:
        if not self.available:
            return False
        try:
            return self.water.intersects(geometry)
        except Exception:
            # 判定不了就按"有障碍"处理：多细化一次不会有结论性代价，把水体当空地
            # 却会凭空给出覆盖。
            self.failed += 1
            return True

    def connector_blocked(self, point: Point, projected: Point) -> bool:
        """入口到接入点的直连段是否穿过水体：一条"过河"的连线段不是入口的证据。

        穿过数据冲突区的连线同样不算：那一片可能是水，走不走得通没有证据。
        """
        segment = LineString([point, projected])
        try:
            if self.conflicts is not None and                     segment.intersection(self.conflicts).length > OBSTACLE_TOUCH_TOLERANCE_M:
                return True
            if not self.available:
                return False
            crossing = segment.intersection(self.water)
        except Exception:
            self.failed += 1
            return True
        return crossing.length > OBSTACLE_TOUCH_TOLERANCE_M

    def separates(self, shared) -> bool:
        """共享边去掉水体之后还剩不剩可走的部分（§6.5 的障碍两侧）。

        判据是**剩余长度**，不是"有没有碰到障碍"：一条河横穿共享边时边上还有没被水淹的
        部分，两侧仍然连通；只有整条共享边都被水吃掉，两侧才真正分开。
        """
        if not self.available:
            return False
        try:
            free = shared.difference(self.water)
        except Exception:
            self.failed += 1
            # 几何运算失败时按"隔开"处理：误合并会把河两岸写成一条连续缺口，
            # 比多分出几个区更难在核对时发现。
            return True
        return free.length <= OBSTACLE_TOUCH_TOLERANCE_M


def local_obstacles(settings, projection, domain):
    """载入评估域附近的硬障碍层。

    ``load_obstacles`` 自己不会抛：缺层、缺版本、文件坏了都返回一个带 warning 的空层，
    由调用方在质量说明里讲清楚"这一版的灰区没有按障碍分隔"。载入范围用评估域的外接框
    外扩 —— 入口可能落在评估域外一点，连接段判定要用到那一段。
    """
    return load_obstacles(settings.hybrid_obstacle_path, projection,
                          settings.osm_data_version, domain.envelope.buffer(OBSTACLE_MARGIN_M),
                          getattr(settings, "water_review_dir", None))


def evaluated_majors(majors, facilities, *, query_complete: bool) -> tuple[str, ...]:
    """这次体检可以给出结论的大类。

    检索跑完时用请求里的大类：没查到设施的类别同样要评估，那是观测到的零覆盖，不是
    "没评估"。检索没跑完时只认真的查到设施的类别 —— 对没查到的类别给一个 [0%, 100%]
    的区间，是把"没查"写成"没有"，而那个区间没有任何证据支撑（§4.4）。
    """
    requested = tuple(major for major in catalog.majors() if major in set(majors))
    if query_complete:
        return requested
    found = {catalog.major_of(item.get("category")) for item in facilities}
    return tuple(major for major in requested if major in found)


def metric_verified(items, projection, *, major: str) -> list[VerifiedPoint]:
    """Coverage spot-check results (bd09ll) → metric points for one major category."""
    points = []
    for item in items or ():
        if item.get("major") != major:
            continue
        try:
            x, y = projection.origin((float(item["lng"]), float(item["lat"])))
        except (KeyError, TypeError, ValueError):
            continue
        points.append(VerifiedPoint(point=Point(x, y), verdict=item["verdict"],
                                    contradicted=item.get("contradicted")))
    return points


def metric_incomplete(regions, projection, *, major: str):
    """The query blocks left unfinished for one major (bd09ll) → one metric geometry."""
    parts = []
    for category, polygons in (regions or {}).items():
        if catalog.major_of(category) != major:
            continue
        for polygon in polygons:
            try:
                parts.append(transform(lambda x, y, z=None: projection.origin((x, y)), shape(polygon)))
            except (TypeError, ValueError):
                continue
    return unary_union(parts) if parts else None


def evaluate_category(major: str, facilities, *, store, views, domain, rule, query_complete,
                      obstacles: _Obstacles, attachments=None, on_cell=None,
                      verified=(), query_incomplete=None) -> CategoryEvaluation:
    """一个类别的完整评估：入口 → 服务场 → 网格 → 面积 → 灰区输入与热力点。

    ``query_complete`` 为假时 :func:`category_service` 永远不会给出 gap —— 检索没跑完
    时"没查到"不是"没有"。同理，没有可用入口的类别整类未知。
    """
    entrances = resolve_entrances(
        metric_facilities(facilities, store.projection, major=major), store=store,
        category_of=lambda item: item["category"],
        guard=(obstacles.connector_blocked
               if obstacles.available or obstacles.conflicts is not None else None),
        limit_m=ENTRANCE_LIMIT_M)
    field = ServiceField(category=major, rule=rule, store=store, views=views, entrances=entrances,
                         query_complete=query_complete, boundary=domain.boundary,
                         attachments=attachments,
                         obstacle_intersects=obstacles.intersects if obstacles.available else None,
                         data_conflict=(obstacles.conflict_intersects
                                        if obstacles.conflicts is not None else None),
                         verified=verified, query_incomplete=query_incomplete)
    # 判定与证据一次算完：网格只拿结论，评估对象留在这里给热力与灰区用。
    assessed: dict = {}

    def evaluate(cell, samples):
        assessment = field.assess(cell, samples)
        assessed[cell.id] = assessment
        if on_cell is not None:
            on_cell(len(assessed))
        return field.decide(cell, samples, assessment)

    try:
        outcome = explore(domain, evaluate)
    except ValueError as exc:
        # 评估域本身就装不下网格（或域不可用）：整类没有结论，不是零分。
        reason = DOMAIN_TOO_LARGE if "grid_limit" in str(exc) else str(exc)
        return CategoryEvaluation(category=major, supported=False, areas=CategoryAreas(),
                                  entrances=field.summary(), unavailable_reason=reason)

    areas = CategoryAreas(covered_m2=_verdict_area(outcome, COVERED),
                          gap_m2=_verdict_area(outcome, GAP),
                          unknown_m2=_verdict_area(outcome, UNKNOWN))
    records, heat = [], []
    for leaf in outcome.leaves:
        # 只与评估域相切的格：既不是一片区域，也不是一个热点。
        if leaf.geometry is None or leaf.area_m2 <= 0:
            continue
        assessment = assessed.get(leaf.cell.id)
        if leaf.verdict == GAP:
            records.append(CellRecord(
                cell=leaf.cell, category=major, verdict=GAP, geometry=leaf.geometry,
                distance_m=None if assessment is None else assessment.min_distance_m,
                nearest_facility=None if assessment is None else assessment.nearest_facility,
                reason=leaf.reason or _gap_reason(assessment),
                evidence_grade="model", query_complete=query_complete))
        # A gap cell usually has no model path at all (nothing within the cutoff); it
        # still gets a point, with no distance, so the map shows a gap rather than a
        # hole the renderer would read as "unknown".
        if assessment is not None and (assessment.min_distance_m is not None or leaf.verdict == GAP):
            heat.append(_heat_point(leaf, assessment, store.projection))
    # 可达面用服务场已经算好的距离场出图：判定与展示来自同一次搜索（§5.4）。
    # Drawn at the rule's threshold: the 1100 m search cutoff only bounds the search,
    # and a reachable surface out to it would show the tolerance band as covered.
    coverage, _counts = coverage_from_settled(views, field.fields, cutoff_m=rule.threshold_m)
    return CategoryEvaluation(
        category=major, supported=True, areas=areas,
        cells={"total": len(outcome.leaves), "refined": outcome.refined, "capped": outcome.capped,
               "covered": len(outcome.by_verdict(COVERED)), "gap": len(outcome.by_verdict(GAP)),
               "unknown": len(outcome.by_verdict(UNKNOWN))},
        entrances=field.summary(), entrance_items=tuple(entrances), records=tuple(records),
        heat=tuple(heat),
        coverage=None if coverage is None else public_geometry(store.projection, coverage))


def _verdict_area(outcome, verdict: str) -> float:
    return sum(leaf.area_m2 for leaf in outcome.by_verdict(verdict))


def _gap_reason(assessment) -> str | None:
    """灰区格的理由。

    已定论的格没有细化理由（``decide`` 只在未定论时才给理由），所以理由要从判定本身
    取：一个格被判成 gap，意味着两个视图上所有支持点都明确不满足规则，而每一条这样的
    判定都是从"乐观视图上也超过了搜索截止"来的。写进报告的就是这句话，而不是空白
    —— 灰区的理由栏空着，读者只能自己猜这一片是"没查到"还是"太远"。
    """
    if assessment is None:
        return None
    return "beyond_service_distance" if any(sample.reason == "beyond_search_cutoff"
                                            for sample in assessment.samples) else None


def _heat_point(leaf, assessment, projection) -> dict:
    """一格一个热力点。取格中心，值是这一格的支持点里最近的模型距离。

    细分后的格是 25 米，所以热力面不是等间距的：它跟结论用的是同一批格，热力不会在
    结论是"未知"的地方画出一个距离。
    """
    lng, lat = public_point(projection, (leaf.cell.center.x, leaf.cell.center.y))
    distance = assessment.min_distance_m
    point = {"cell": leaf.cell.id, "lng": lng, "lat": lat,
             "distanceM": None if distance is None else round(distance, 3),
             "nearestFacility": assessment.nearest_facility, "status": leaf.verdict}
    # 未知格带上理由：水面、数据冲突与"证据不足"在地图上是三种不同的未知。
    if leaf.verdict == UNKNOWN and leaf.reason:
        point["reason"] = leaf.reason
    return point


def _areas_match(areas: CategoryAreas, domain_area_m2: float) -> bool:
    return abs(areas.total_m2() - domain_area_m2) <= area_tolerance(domain_area_m2)


def _overall_payload(overall) -> dict:
    """总体分与"给不出总体分"都序列化成 dict：界面必须能说清是哪一类给不出。"""
    if hasattr(overall, "missing_categories"):
        return {"available": False, "reason": overall.reason,
                "missingCategories": list(overall.missing_categories)}
    return {"available": True, "coverageLowerPct": overall.coverage_lower_pct,
            "coverageUpperPct": overall.coverage_upper_pct,
            "assessablePct": overall.assessable_pct, "unknownPct": overall.unknown_pct,
            "categories": list(overall.categories), "weights": dict(overall.weights)}


def _score(evaluated, domain_area_m2: float) -> ScoreEvidence:
    """§7.1 的区间分。缺空间支持的类别不给百分比，也不参与总体分。

    总体分的口径固定是**核心分析范围**（三大类各 1/3），不是这一次请求的类别集合。
    两个方向都会错：请求两类时把两类等权，等于用"这次只看了两类"冒充"三类总分"；
    请求十类时按十类等权，则把 1/3 的业务权重悄悄换成 1/10，而那个权重没有任何依据。
    扩展类别照常出自己的一行，只是不进总体分。
    """
    scores, rows = {}, []
    for item in evaluated:
        score = category_score(item.category, item.areas, domain_area_m2=domain_area_m2,
                               spatial_support=item.supported,
                               unavailable_reason=item.unavailable_reason)
        scores[item.category] = score
        rows.append({"category": item.category, "supported": score.supported,
                     "coverageLowerPct": score.coverage_lower_pct,
                     "coverageUpperPct": score.coverage_upper_pct,
                     "assessablePct": score.assessable_pct, "unknownPct": score.unknown_pct,
                     "intervalWidthPct": score.interval_width_pct,
                     "intervalDegenerate": interval_degenerates(score),
                     "unavailableReason": score.unavailable_reason})
    return ScoreEvidence(
        domain_area_m2=domain_area_m2, categories=rows,
        overall=_overall_payload(overall_score(
            scores, expected_categories=DEFAULT_SCORING_CATEGORIES)),
        formula={"coverageLower": "100*C/A", "coverageUpper": "100*(C+U)/A",
                 "assessable": "100*(C+G)/A", "identity": "C+G+U=A",
                 "categoryWeights": category_weights(DEFAULT_SCORING_CATEGORIES)})


def _zone_payload(zone, projection) -> ServiceZone:
    """灰区在修订里的形态：统计几何（米制）与展示几何（bd09ll）各一份。

    面积只认统计几何。展示几何由同一个对象导出，所以图上画的边界就是算了面积的那条
    边界 —— §6.5 不允许展示简化改变统计几何，把两份做成同源是最省事也最不容易做错的做法。
    """
    payload = zone.as_dict()
    payload["displayGeometry"] = (
        None if payload["geometry"] is None
        else public_geometry(projection, shape(payload["geometry"])))
    return ServiceZone(**payload)


def _zone_area_by_category(zones) -> dict:
    """每类灰区的面积取该类各区之和：同类别的两个区都来自互不重叠的叶格，不会重复。
    综合灰区跨越类别，单独由 ``composite_area_m2`` 报出，不混进这里。"""
    totals: dict[str, float] = {}
    for zone in zones:
        if zone.kind == "composite":
            continue
        for category in zone.categories:
            totals[category] = totals.get(category, 0.0) + zone.area_m2
    return totals


def _union_gap_area(records, domain) -> float:
    """所有类别 gap 格的**并集**面积：一个格同时缺两类只算一次。

    逐类 gap 面积相加在这里是重复计数 —— 那正是 §6.5 点名要避免的算法。
    """
    parts = [record.geometry for record in records if record.geometry is not None]
    if not parts:
        return 0.0
    merged = unary_union(parts)
    return merged.intersection(domain).area if domain is not None else merged.area


def _status_of(evaluated, query_complete: bool, obstacles_available: bool,
               excluded_m2: float) -> str:
    if not evaluated or not any(item.supported for item in evaluated):
        return "failed"
    if not query_complete or not all(item.supported for item in evaluated):
        return "partial"
    # 障碍层缺失时灰区没有按水体分隔、被数据集覆盖范围裁掉的部分没有被评估：两种都是
    # "这一阶段没做完"，所以整套结论降为部分完成。分数本身不受影响 —— 它只由网格面积
    # 决定 —— 变的只是"这份结论覆盖了多少"。
    if not obstacles_available or excluded_m2 > 0:
        return "partial"
    return "complete"


def assess_accessibility(*, geometry, facilities, query_status: str, majors, store,
                         version: str, coverage=None, unknown_region=None, obstacles=None,
                         rule=DISTANCE_RULE, settings=None, progress=None,
                         verified=None, incomplete=None) -> AccessibilityOutcome:
    """跑完可达性阶段（§5–§7.1）。

    ``store`` 为 None 表示没有 OSM 图，``facilities`` 为 None 表示设施阶段没有建立
    结果 —— 两种都是"这一阶段没有结论"，各自带着名字拒绝，不返回空的 0%。
    ``obstacles`` 为 None 时会按 ``settings.hybrid_obstacle_path`` 现载一层。

    这是同步的 CPU 密集工作（整城图上的多源最短路 + 上千格的吸附），调用方应放进线程，
    不要占住事件循环。

    ``progress(step, **detail)`` 在每一步开始时被调用（``domain``、``views``、
    ``obstacles``、每个类别的 ``category``），类别内每判定一格再报一次已判定的格数；
    它只报已经发生的事，不预估还剩多少。

    ``verified`` 是覆盖抽检的实测结论（``major``/``lng``/``lat``/``verdict``/
    ``contradicted``）：给了就是 §6.3 的局部重算——只在实测点所在的 25 米格定论，
    同一父格里被推翻的模型结论降为未知，其余格照旧由模型判定。

    ``incomplete`` 是检索没有查完的分块（按小类，bd09ll 多边形）。给了就按格判断：
    一格的服务搜索范围内有没查完的分块才不许判缺口，而不是整个任务一个开关（§4.4）。
    """
    report = progress or (lambda step, **detail: None)
    requested = tuple(major for major in catalog.majors() if major in set(majors))
    # ``query_status`` 用的是修订文档里 ``facilitiesStatus`` 的口径（complete/partial/
    # failed），不是检索器内部的 queryStatus 拼写。两者只差一个词尾，混用不会报错，
    # 却会让每一格"明确太远"的结论退化成未知 —— 灰区清单随之整体消失，而未知面积
    # 涨上去，报告看起来一样完整。
    query_complete = query_status == "complete"
    if store is None:
        return _refusal(NO_GRAPH, requested, "OSM 步行路网不可用，服务覆盖与灰区无法评估。")
    if facilities is None:
        return _refusal(NO_FACILITIES, requested, "设施检索没有建立结果，服务覆盖无从评估。")
    # A failed retrieval may still publish an empty group so its query evidence
    # and stop reason remain inspectable. It is not partial evidence, though:
    # no category was actually established and the failure should not be
    # reported as an ordinary incomplete search.
    if query_status == "failed":
        return _refusal(NO_FACILITIES, requested, "设施检索阶段未产出可用结果，服务覆盖无从评估。")
    majors_used = evaluated_majors(requested, facilities, query_complete=query_complete)
    # A completed major with zero findings is evaluable even while another
    # major still has queued keywords. Absence is never inferred for unfinished majors.
    if incomplete is not None:
        completed_majors = {major for major in requested if not any(
            regions for minor, regions in incomplete.items() if catalog.major_of(minor) == major)}
        majors_used = tuple(major for major in requested if major in majors_used or major in completed_majors)
    if not majors_used:
        return _refusal(QUERY_INCOMPLETE, requested,
                        "设施检索未完成且没有取到任何可用设施，服务覆盖未评估。",
                        status="partial")
    report("domain")
    try:
        domain, exclusions = assessment_domain(geometry, projection=store.projection,
                                              coverage=coverage)
    except ValueError as exc:
        return _refusal(str(exc), majors_used,
                        "评估域无法建立（边界不可用或落在数据集覆盖范围之外），服务覆盖未评估。")
    if len(cells_covering(domain)) > MAX_LEAF_CELLS:
        return _refusal(DOMAIN_TOO_LARGE, majors_used,
                        "评估域超过网格上限，服务覆盖未评估；请缩小分析范围后重试。")
    domain_area_m2 = domain.area
    report("views")
    views = build_views(store.graph, version=version)
    report("obstacles")
    obstacles = _Obstacles(obstacles if obstacles is not None
                           else (local_obstacles(settings, store.projection, domain)
                                 if settings is not None else None))
    # 接入索引覆盖整张路网，比载入障碍层慢得多：单列一步，免得"载入障碍"背上它的时长。
    report("attachments")
    attachments = PointAttachments(store)
    evaluated = []
    for index, major in enumerate(majors_used, start=1):
        detail = dict(major=major, index=index, total=len(majors_used))
        report("category", cells=0, **detail)
        evaluated.append(evaluate_category(
            major, facilities, store=store, views=views, domain=domain, rule=rule,
            # Known unfinished blocks replace the task-wide switch with a per-cell test.
            query_complete=query_complete or incomplete is not None,
            obstacles=obstacles, attachments=attachments,
            on_cell=lambda cells, detail=detail: report("category", cells=cells, **detail),
            verified=metric_verified(verified, store.projection, major=major),
            query_incomplete=(None if incomplete is None
                              else metric_incomplete(incomplete, store.projection, major=major))))
    issues = []
    for item in evaluated:
        if item.supported and not _areas_match(item.areas, domain_area_m2):
            # C+G+U ≠ A：这一类的面积结论不可用。不归一化，直接降级为"无法确定"。
            item.supported = False
            item.unavailable_reason = AREA_MISMATCH
            issues.append(Issue(code="ACCESSIBILITY_AREA_MISMATCH",
                                message=f"{item.category}: C+G+U 与评估域面积不符，"
                                        f"该类别未给出分数结论。",
                                scope="accessibility", severity="error"))
    notes = []
    if obstacles.failed:
        notes.append(f"obstacle_geometry_failures={obstacles.failed}")
    if not obstacles.available:
        notes.append("hard_obstacle_layer_unavailable: 灰区未按障碍分隔，"
                     "跨河两岸可能被并成一片。")
    excluded = sum(exclusions.values())
    for reason, area in sorted(exclusions.items()):
        notes.append(f"excluded_area_m2={round(area, 3)} ({reason}): 未计入评估域。")
    water = water_evidence(obstacles.local, domain, store.projection)
    if water is not None and water.conflict_area_m2 > 0:
        notes.append(f"water_data_conflict_m2={round(water.conflict_area_m2, 3)}: "
                     "水系来源互相矛盾且未裁决的格按“数据冲突／未知”计，不计覆盖也不计灰区。")
    if water is not None and water.rejected_reviews:
        notes.append("water_review_not_applied: 覆盖本区域的水系复核与当前 OSM 数据版本不符，"
                     "未采用，按 OSM 原样计算。")
    engine_unknown = engine_unknown_region(unknown_region, store.projection)
    if engine_unknown is not None:
        # 报出来是为了让它可见，不是为了让它做减法：分母 A 只由圈面和数据覆盖范围决定。
        notes.append(f"engine_unknown_region_m2={round(engine_unknown.area, 3)}: "
                     "成圈算法标出的不确定区，不从评估域扣除（§6.1）；"
                     "域内无法评估的格子按未知计，仍留在分母里。")
    records = [record for item in evaluated for record in item.records]
    zones = merge_zones(records, domain=domain, majors=majors_used,
                        obstacle_between=obstacles.separates if obstacles.available else None)
    domain_public = public_geometry(store.projection, domain)
    status = _status_of(evaluated, query_complete, obstacles.available, excluded)
    evidence = AccessibilityEvidence(
        status=status, domain=domain_public, domain_area_m2=domain_area_m2,
        excluded_area_m2=excluded, views=views.counts,
        categories=[_category_evidence(item) for item in evaluated], notes=notes)
    gaps = ServiceGapsEvidence(
        status=status, zones=[_zone_payload(zone, store.projection) for zone in zones],
        by_category_m2=_zone_area_by_category(zones),
        composite_area_m2=sum(zone.area_m2 for zone in zones if zone.kind == "composite"),
        gap_area_m2=_union_gap_area(records, domain),
        obstacle_layer_available=obstacles.available, notes=notes)
    heatmap = HeatmapEvidence(
        domain=domain_public, categories={item.category: list(item.heat) for item in evaluated},
        notes=["模型估计：格到最近设施入口的步行路网距离，不是实测，也不代表人口覆盖。",
               *notes])
    return AccessibilityOutcome(
        accessibility=evidence, service_gaps=gaps, heatmap=heatmap,
        scores=_score(evaluated, domain_area_m2), status=status, issues=issues, water=water,
        # 一个设施只属于一个大类（按类别过滤过），所以 ID 之间不会互相覆盖。
        entrances={entrance.facility_id: entrance
                   for item in evaluated for entrance in item.entrance_items})


def _category_evidence(item: CategoryEvaluation) -> CategoryCoverage:
    return CategoryCoverage(
        category=item.category, supported=item.supported, covered_m2=item.areas.covered_m2,
        gap_m2=item.areas.gap_m2, unknown_m2=item.areas.unknown_m2, cells=item.cells,
        entrances=item.entrances, coverage=item.coverage,
        unavailable_reason=item.unavailable_reason)


def _refusal(reason: str, majors, message: str, *, status: str = "failed") -> AccessibilityOutcome:
    """阶段跑不起来：三份证据都是 null，原因有名字，按类别逐条写出。"""
    return AccessibilityOutcome(
        accessibility=AccessibilityEvidence(
            status=status,
            categories=[CategoryCoverage(category=major, supported=False,
                                         unavailable_reason=reason) for major in majors],
            notes=[message]),
        service_gaps=None, heatmap=None, scores=None, status=status,
        issues=[Issue(code="ACCESSIBILITY_UNAVAILABLE", message=message, scope="accessibility",
                      severity="error" if status == "failed" else "warning")])
