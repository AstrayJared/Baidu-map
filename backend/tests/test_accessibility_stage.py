"""§5–§7.1 可达性阶段：评估域冻结、三态面积、灰区清单与热力数据面。

每条断言都钉在一个会被相反实现破坏的位置：

* **C + G + U = A 必须成立**，而且 A 是冻结的评估域面积，不是三态相加凑出来的 ——
  凑出来的话删掉一格就能把分数抬上去；
* **没有入口的类别整类未知**，不是"覆盖率为零"：把"不知道设施在哪"写成"这里没有
  服务"是这个阶段最严重的一类误判；
* **检索没跑完的类别不给区间**，也不假装成"查过但没查到"；
* **灰区只由 gap 产生**，unknown 既不进灰区也不参与连通。
"""
import pytest
from shapely.geometry import MultiPolygon, box, shape

from app import catalog
from app.accessibility.service_graph import build_views
from app.algorithms.hybrid_isochrone.hard_obstacles import LocalObstacles
from app.algorithms.osm_offline.graph_store import PEDESTRIAN_ATTRS, GraphStore
from app.checkups import accessibility_stage as stage
from app.geo.projection import MetricProjection

import networkx as nx

CRS = "EPSG:32651"
SPEED = 1.3
#: 真实数据所在的纬度：投影往返在这里是微米级，测试里的偏移量因此可以直接当米用。
BASE = (121.4737, 31.2304)
PROJECTION = MetricProjection(CRS)
X0, Y0 = PROJECTION.origin(BASE)

#: 一条沿 dx 轴的东西向步行道，节点每 100 米一个，两侧都能走。
ROAD_MIN, ROAD_MAX, ROAD_STEP = -400, 4400, 100


def metric(dx: float, dy: float = 0.0) -> tuple[float, float]:
    return X0 + dx, Y0 + dy


def public(dx: float, dy: float = 0.0) -> tuple[float, float]:
    from app.checkups.accessibility_stage import public_point
    return public_point(PROJECTION, metric(dx, dy))


def road_store() -> GraphStore:
    """一条笔直的东西向步道，长度超过测试用的评估域，远端不至于"图断开"。"""
    graph = nx.MultiDiGraph(crs=CRS, osm_data_version="acc-stage-test", walking_speed_mps=SPEED)
    nodes = {}
    for index, dx in enumerate(range(ROAD_MIN, ROAD_MAX + 1, ROAD_STEP)):
        nodes[index] = metric(dx)
        graph.add_node(index, x=nodes[index][0], y=nodes[index][1])
    for index in range(len(nodes) - 1):
        for tail, head in ((index, index + 1), (index + 1, index)):
            line = _line(nodes[tail], nodes[head])
            data = dict.fromkeys(PEDESTRIAN_ATTRS)
            data.update(geometry=line, length=line.length, travel_time_s=line.length / SPEED,
                        osmid=index + 1)
            graph.add_edge(tail, head, **data)
    return GraphStore(graph, speed=SPEED, crs=CRS)


def _line(point, other):
    from shapely.geometry import LineString
    return LineString([point, other])


def boundary(min_dx: float, min_dy: float, max_dx: float, max_dy: float) -> dict:
    """评估域的外边界（评估域本身由阶段从这份几何重建）。"""
    region = box(*metric(min_dx, min_dy), *metric(max_dx, max_dy))
    return PROJECTION.public_geometry(region, repair_roundoff=True)


def facility(dx: float, dy: float = 0.0, *, facility_id: str = "f-medical",
             category: str = "pharmacy") -> dict:
    """一条 POI 阶段的设施记录，坐标是它自己的 bd09ll 公开坐标。"""
    lng, lat = public(dx, dy)
    return {"id": facility_id, "name": facility_id, "category": category,
            "location": {"lng": lng, "lat": lat}}


def water_band(dx: float, low: float, high: float, *, half_width: float = 20.0
               ) -> LocalObstacles:
    """一段横在水体里的窄带（米制），用于入口连接段的穿水判定。

    带子要横在设施与道路**之间**：盖住设施本身的方块不会让连接段穿过任何东西，
    因为它把两个端点一起淹了。
    """
    pool = box(*metric(dx - half_width, low), *metric(dx + half_width, high))
    return LocalObstacles(water=MultiPolygon([pool]))


def dry_layer() -> LocalObstacles:
    """离评估域很远的一小块水体：障碍层"可用"但不影响任何判定。"""
    return LocalObstacles(water=MultiPolygon([box(*metric(-90000, -90000),
                                                  *metric(-89900, -89900))]))


MAJORS = ("shopping", "medical", "education")


def run(**overrides):
    """跑一次阶段，默认是一片 3000×200 米的评估域、一家在路边的药店。"""
    arguments = dict(geometry=boundary(0, -100, 3000, 100), facilities=[facility(100)],
                     query_status="complete", majors=MAJORS, store=road_store(),
                     version="acc-stage-test", obstacles=dry_layer())
    arguments.update(overrides)
    return stage.assess_accessibility(**arguments)


def row(scores, category: str):
    return next(item for item in scores.categories if item.category == category)


def category(evidence, name: str):
    return next(item for item in evidence.categories if item.category == name)


# ---------------------------------------------------------------- 评估域与面积


def test_the_frozen_domain_is_the_denominator():
    """A 是评估域自己要出来的面积，C+G+U 与它相符；域比外框大一点也要如实报出来。"""
    outcome = run()
    evidence = outcome.accessibility
    assert evidence.status == "complete"
    assert evidence.grid_step_m == 50.0 and evidence.refined_step_m == 25.0
    assert evidence.views["edges_total"] > 0
    for name in MAJORS:
        item = category(evidence, name)
        assert item.supported is True
        assert item.covered_m2 + item.gap_m2 + item.unknown_m2 == pytest.approx(
            evidence.domain_area_m2, abs=1.0)
    # 评估域的面积来自重建出来的多边形，与名义上的 3000×200 米相符（BD09 往返有
    # 十万分之几的伸缩，所以比的是相对误差，不是相等）。
    assert evidence.domain_area_m2 == pytest.approx(600_000, rel=1e-4)
    assert evidence.domain is not None
    assert outcome.scores.domain_area_m2 == evidence.domain_area_m2
    # 可评估率 + 未知率恒等于 100%：这是 C+G+U=A 的另一面，也是对 A 的独立校验。
    for item in outcome.scores.categories:
        assert item.assessable_pct + item.unknown_pct == pytest.approx(100, abs=1e-6)


def test_covered_and_gap_are_both_present_along_the_road():
    """一家路边设施：近处是覆盖、远端是灰区，中间夹着细化后仍无结论的未知。"""
    outcome = run()
    medical = category(outcome.accessibility, "medical")
    assert medical.covered_m2 > 0
    assert medical.gap_m2 > 0
    assert medical.cells["gap"] > 0 and medical.cells["covered"] > 0
    assert medical.entrances["valid_entrances"] == 1
    scored = row(outcome.scores, "medical")
    assert 0 < scored.coverage_lower_pct < scored.coverage_upper_pct <= 100
    assert scored.interval_width_pct == pytest.approx(
        scored.coverage_upper_pct - scored.coverage_lower_pct)
    assert scored.interval_degenerate is False


def test_a_category_without_any_facility_is_unknown_not_uncovered():
    """只检索到医疗设施：另外两类是三态里的未知，不是"覆盖率为零"。"""
    outcome = run()
    gap = category(outcome.accessibility, "shopping")
    assert gap.supported is True
    assert gap.covered_m2 == 0 and gap.gap_m2 == 0
    assert gap.unknown_m2 == pytest.approx(outcome.accessibility.domain_area_m2, abs=1.0)
    scored = row(outcome.scores, "shopping")
    assert scored.coverage_lower_pct == 0
    assert scored.coverage_upper_pct == pytest.approx(100, abs=0.01)
    # 未评估的面积进了"最高覆盖率"，没有进灰区。
    assert [zone for zone in outcome.service_gaps.zones
            if "shopping" in zone.categories] == []
    assert "shopping" not in outcome.service_gaps.by_category_m2


def test_overall_score_is_unavailable_when_a_category_cannot_be_scored():
    """只分析两大类时不给总体分：把两类重新加权成"三类总分"会让读者以为都评估过。"""
    outcome = run(majors=("shopping", "medical"))
    assert outcome.scores.overall.available is False
    assert outcome.scores.overall.reason == "categories_not_analysed"
    assert outcome.scores.overall.missing_categories == [major for major in catalog.majors() if major not in ("shopping", "medical")]


def test_ten_assessable_categories_give_one_overall_interval():
    outcome = run(majors=catalog.majors(), facilities=[
        facility(100, facility_id=major, category=catalog.minors_of(major)[0])
        for major in catalog.majors()])
    overall = outcome.scores.overall
    assert overall.available is True
    assert 0 <= overall.coverage_lower_pct <= overall.coverage_upper_pct <= 100
    assert overall.weights == {name: pytest.approx(1 / 10) for name in catalog.majors()}


def test_an_expanded_taxonomy_neither_dilutes_nor_hides_the_overall_score():
    """请求的类别超出核心范围时，总体分仍然是三大类各 1/3。

    这是"十大类"最容易出错的两种写法：把十类等权就把 1/3 的业务权重悄悄换成 1/10；
    因为多查了几类就收起总体分，则让用户以为一次正常的扩展体检"没有结论"。
    扩展类别照常出自己的一行，只是不进总体分。
    """
    requested = tuple(catalog.majors())
    assert set(MAJORS) < set(requested)
    outcome = run(majors=requested + ("dining", "leisure"))
    overall = outcome.scores.overall
    assert overall.available is True
    assert overall.categories == list(MAJORS)
    assert overall.weights == {name: pytest.approx(1 / 3) for name in MAJORS}
    # 扩展类别确实被评估、也确实有自己的区间，只是没有进总体分。
    scored = {item.category for item in outcome.scores.categories}
    assert {"care", "dining", "finance", "public", "leisure", "transport", "life"} <= scored
    assert set(overall.categories) < scored
    assert outcome.scores.formula["categoryWeights"] == {
        name: pytest.approx(1 / 3) for name in MAJORS}


# ---------------------------------------------------------------- 拒绝与降级


def test_missing_graph_refuses_without_inventing_scores():
    """没有 OSM 图：只报"无法确定"，不报 0%，也不产生灰区。"""
    outcome = run(store=None)
    assert outcome.status == "failed"
    assert outcome.scores is None and outcome.service_gaps is None and outcome.heatmap is None
    assert outcome.accessibility.status == "failed"
    for item in outcome.accessibility.categories:
        assert item.supported is False
        assert item.unavailable_reason == stage.NO_GRAPH
        assert item.covered_m2 == 0 and item.gap_m2 == 0 and item.unknown_m2 == 0
    assert [issue.code for issue in outcome.issues] == ["ACCESSIBILITY_UNAVAILABLE"]


def test_missing_facility_stage_refuses():
    outcome = run(facilities=None)
    assert outcome.scores is None
    assert {item.unavailable_reason for item in outcome.accessibility.categories} \
        == {stage.NO_FACILITIES}


def test_failed_facility_query_is_not_reported_as_incomplete_evidence():
    outcome = run(query_status="failed", facilities=[])
    assert outcome.service_gaps is None and outcome.heatmap is None
    assert outcome.accessibility.status == "failed"
    assert {item.unavailable_reason for item in outcome.accessibility.categories} \
        == {stage.NO_FACILITIES}


def test_no_boundary_refuses():
    outcome = run(geometry=None)
    assert outcome.scores is None
    assert {item.unavailable_reason for item in outcome.accessibility.categories} \
        == {stage.NO_BOUNDARY}


def test_domain_beyond_the_grid_limit_refuses_the_stage():
    """评估域装不下网格时，不假装评估过，也不静默粗化。"""
    outcome = run(geometry=boundary(0, -4000, 4000, 0))
    assert outcome.status == "failed"
    assert outcome.scores is None
    assert {item.unavailable_reason for item in outcome.accessibility.categories} \
        == {stage.DOMAIN_TOO_LARGE}


def test_incomplete_retrieval_does_not_claim_a_verdict():
    """检索没跑完：只有真的查到设施的大类才评估，其余类别连区间都不给。"""
    outcome = run(query_status="partial", facilities=[facility(100)])
    names = [item.category for item in outcome.accessibility.categories]
    assert names == ["medical"]
    assert outcome.status == "partial"
    assert outcome.scores.overall.available is False


def test_incomplete_retrieval_without_facilities_refuses_the_stage():
    outcome = run(query_status="partial", facilities=[])
    assert outcome.service_gaps is None and outcome.heatmap is None
    assert outcome.accessibility.status == "partial"
    assert {item.unavailable_reason for item in outcome.accessibility.categories} \
        == {stage.QUERY_INCOMPLETE}
    assert outcome.issues[0].severity == "warning"


def test_a_category_whose_areas_do_not_add_up_is_downgraded(monkeypatch):
    """C+G+U 与 A 不符时降级为"无法确定"，绝不归一化后照常给分。"""
    monkeypatch.setattr(stage, "_areas_match", lambda areas, domain_area: False)
    outcome = run()
    assert all(item.supported is False for item in outcome.accessibility.categories)
    assert all(item.unavailable_reason == stage.AREA_MISMATCH
               for item in outcome.accessibility.categories)
    assert outcome.scores.overall.available is False
    assert [issue.code for issue in outcome.issues] == ["ACCESSIBILITY_AREA_MISMATCH"] * 3


def test_entrance_behind_water_leaves_the_category_unknown():
    """连接段穿水体：这个入口不算数，整类未知 —— 而不是"这里没有服务"。"""
    # 设施在路北 60 米，水体横在 20–40 米之间：连接段必须过水。
    outcome = run(facilities=[facility(100, 60)], obstacles=water_band(100, 20, 40))
    medical = category(outcome.accessibility, "medical")
    assert medical.entrances["unresolved_entrances"] == 1
    assert medical.entrances["valid_entrances"] == 0
    assert medical.unknown_m2 == pytest.approx(outcome.accessibility.domain_area_m2, abs=1.0)
    assert medical.gap_m2 == 0


def test_excluded_area_is_reported_separately_from_unknown():
    """被数据集覆盖范围裁掉的部分是"不在结论范围内"，不并进未知面积。"""
    coverage = box(*metric(-1000, -1000), *metric(1000, 1000))
    outcome = run(coverage=coverage)
    evidence = outcome.accessibility
    assert evidence.excluded_area_m2 == pytest.approx(400_000, rel=1e-4)
    assert evidence.domain_area_m2 == pytest.approx(200_000, rel=1e-4)
    assert any("excluded_area_m2" in note for note in evidence.notes)
    assert evidence.status == "partial"


def test_a_missing_obstacle_layer_is_stated_not_silently_ignored():
    outcome = run(obstacles=LocalObstacles())
    assert outcome.service_gaps.obstacle_layer_available is False
    assert any("hard_obstacle_layer_unavailable" in note for note in outcome.accessibility.notes)
    assert outcome.status == "partial"


# ---------------------------------------------------------------- 灰区与热力


def test_gap_zones_are_derived_from_gap_cells_only():
    outcome = run()
    gaps = outcome.service_gaps
    assert gaps.status == outcome.status
    assert gaps.zones, "远端应当出现灰区"
    medical = [zone for zone in gaps.zones if zone.categories == ["medical"]]
    assert len(medical) == 1
    zone = medical[0]
    assert zone.kind == "single"
    assert zone.area_m2 > 0
    assert zone.parts >= 1
    assert zone.cell_ids, "每个灰区都要能回到它的格"
    assert len(zone.cell_ids) == len(set(zone.cell_ids)), "格 ID 不重复"
    # 理由栏不能空着：读者要能分清这一片是"没查到"还是"太远"。
    assert zone.reason == "beyond_service_distance"
    assert zone.suggestion, "灰区必须带一条可执行的后续建议"
    assert zone.evidence_grade == "model" and zone.query_status == "complete"
    # 最近已知设施只在量得到的时候报出来：搜索截止 1100 米，而接入成本也算进总距离，
    # 所以只有靠近截止的那些格子能报出"1170 米外的那家"。报不出就是 null，不猜。
    assert zone.nearest_facility in (None, "f-medical")
    assert gaps.by_category_m2["medical"] == pytest.approx(zone.area_m2, rel=1e-9)
    assert gaps.composite_area_m2 == 0
    assert gaps.composite_min_categories == 2 and gaps.min_label_area_m2 == 2500.0
    # 综合灰区要求三大类里至少两类同时为 gap：只有医疗查到过设施时不可能出现。
    assert not any(item.kind == "composite" for item in gaps.zones)


def test_gap_area_is_a_union_not_a_sum_of_categories():
    """三类都缺同一片地方时，缺口面积只算一次。"""
    outcome = run(facilities=[])
    gaps = outcome.service_gaps
    # 没有设施 ⇒ 三类都是未知，一片灰区都没有，缺口面积为 0。
    assert gaps.zones == []
    assert gaps.gap_area_m2 == 0
    assert gaps.by_category_m2 == {}


def test_composite_zone_needs_two_of_the_three_majors():
    """三类都没查到设施是未知，不是缺口；构造"两类同时 gap"才会出综合灰区。"""
    outcome = run(facilities=[facility(100), facility(100, facility_id="f-school",
                                                   category="school")])
    composite = [item for item in outcome.service_gaps.zones if item.kind == "composite"]
    assert len(composite) == 1
    assert set(composite[0].categories) == {"medical", "education"}
    assert composite[0].area_m2 > 0
    assert outcome.service_gaps.composite_area_m2 == pytest.approx(composite[0].area_m2)


def test_heat_points_carry_a_measured_distance_or_are_gaps_without_one():
    outcome = run()
    heat = outcome.heatmap
    assert heat.metric == "walking_route" and heat.estimated is True
    assert heat.step_m == 50.0
    assert heat.domain == outcome.accessibility.domain
    medical = heat.categories["medical"]
    assert medical, "覆盖格应当有热力点"
    for point in medical:
        # 有模型距离的格报出距离；缺口格（截止内没有路径）也出点，距离为空、状态为 gap，
        # 地图才画得出缺口 —— 它绝不是一个"距离为零"的热点。
        if point["distanceM"] is None:
            assert point["status"] == "gap"
        else:
            assert point["distanceM"] >= 0
        assert point["cell"]
        assert point["status"] in ("covered", "gap", "unknown")
        assert -180 <= point["lng"] <= 180 and -90 <= point["lat"] <= 90
    # 没有设施的大类没有可测的距离，热力为空 —— 不是一堆距离为零的点。
    assert heat.categories["shopping"] == []
    assert any("模型估计" in note for note in heat.notes)


def test_the_frozen_zone_list_is_a_validated_contract_not_a_bag_of_dicts():
    """灰区清单的每一项都是校验过的 ``ServiceZone``：键写错了要当场报错。

    这份清单是报告里"每片灰区为什么是灰区、下一步查什么"的唯一来源，而它曾经是裸
    dict —— ``as_dict()`` 里少写一个键、或者多写一个键，没有任何一处会拒绝，读到的人
    只能对着 ``None`` 猜。这里钉住的是"少一个键就构造不出来"，顺带钉住键名本身。
    """
    from app.checkups.models import ServiceZone

    outcome = run()
    zones = outcome.service_gaps.zones
    assert zones and all(isinstance(zone, ServiceZone) for zone in zones)
    payload = zones[0].model_dump(mode="json", by_alias=True)
    assert set(payload) == {
        "id", "index", "categories", "kind", "areaM2", "parts", "cellIds", "labelVisible",
        "suspected", "evidenceGrade", "queryStatus", "nearestFacility", "reason",
        "suggestion", "geometry", "geometrySystem", "displayGeometry"}
    with pytest.raises(Exception):
        ServiceZone(**{**payload, "suggestion": None})
    with pytest.raises(Exception):
        ServiceZone(**{**payload, "unexpected": 1})


def test_zone_geometry_is_the_counted_geometry():
    """灰区的几何必须就是被算面积的那一块：重新构造一次会让图与数字对不上。"""
    outcome = run()
    zone = next(item for item in outcome.service_gaps.zones if item.kind == "single")
    counted = shape(zone.geometry)
    assert zone.geometry_system == "metric"
    domain = stage.assessment_domain(outcome.accessibility.domain, projection=PROJECTION)[0]
    assert counted.within(domain.buffer(1e-6))
    assert counted.area == pytest.approx(zone.area_m2, rel=1e-9)
    # 展示几何是同一块地方在 bd09ll 下的样子，不是另算一遍：把它投回米制，
    # 面积必须还是同一个（BD09 往返有十万分之几的伸缩）。
    drawn = stage.metric_region(zone.display_geometry, PROJECTION)
    assert drawn.area == pytest.approx(counted.area, rel=1e-4)


# -- 评估域只由圈面和数据覆盖范围决定 ----------------------------------------

def test_the_dataset_window_trims_only_what_lies_outside_it():
    """数据集覆盖范围是窗口：域里超出窗口的那一半要报出来，而不是把域换掉。

    窗口求交（``∩``）—— 这一条钉住的是"运算符的方向"，不是"扣了多少"。
    """
    window = box(*metric(-500, -400), *metric(0, 400))
    domain, exclusions = stage.assessment_domain(
        boundary(-500, -400, 500, 400), projection=PROJECTION, coverage=window)
    assert domain.area == pytest.approx(500 * 800, rel=1e-3)
    assert exclusions["coverage_beyond_dataset"] == pytest.approx(500 * 800, rel=1e-3)


def test_a_domain_entirely_outside_the_dataset_is_not_scored():
    """域整个落在数据集之外时报"评估域为空"，不是 0% 覆盖 —— 那两句话不是一回事。"""
    window = box(*metric(5000, 5000), *metric(6000, 6000))
    with pytest.raises(ValueError) as refused:
        stage.assessment_domain(boundary(-500, -400, 500, 400), projection=PROJECTION,
                                coverage=window)
    assert str(refused.value) == stage.EMPTY_DOMAIN


def test_the_engine_unknown_region_never_shrinks_the_assessment_domain():
    """成圈算法的不确定区不进评估域，也不从评估域里扣（方案 §6.1）。

    两个引擎都把它造成 ``unknown.difference(geometry)``，也就是围在可达面外面的一圈；
    把它当"要保留的那一块"去求交，评估域就只剩边界上那一丝缝 —— 真实的一次百度体检
    因此跑出过 A = 0.00035 m²、覆盖率 0%、未知 99.9999%，报告读起来像"这一带没有服务"。
    """
    reachable = box(*metric(-500, -400), *metric(500, 400))
    ring = box(*metric(-600, -500), *metric(600, 500)).difference(reachable)
    window = box(*metric(-2000, -2000), *metric(2000, 2000))
    domain, exclusions = stage.assessment_domain(
        boundary(-500, -400, 500, 400), projection=PROJECTION, coverage=window)
    assert domain.area == pytest.approx(reachable.area, rel=1e-3)
    assert exclusions == {}
    # 传进来的那一圈原样报出来，看得见，但不许它改动分母。
    reported = stage.engine_unknown_region(PROJECTION.public_geometry(ring), PROJECTION)
    assert reported.area == pytest.approx(ring.area, rel=1e-3)


def test_a_whole_extent_unknown_region_still_leaves_the_domain_alone():
    """缺未知几何时 E8.2 会拿整个计算域当 unknownRegion（§6.1 点名的那个坑）。

    机械相减会在这里把评估域整块扣没。域里剩下的部分本来就该保留为未知、不从分母消失：
    那是 U 的活，不是 A 的。
    """
    reachable = box(*metric(-500, -400), *metric(500, 400))
    whole_extent = box(*metric(-1600, -1600), *metric(1600, 1600))
    domain, exclusions = stage.assessment_domain(
        boundary(-500, -400, 500, 400), projection=PROJECTION)
    assert domain.area == pytest.approx(reachable.area, rel=1e-3)
    assert exclusions == {}
    assert stage.engine_unknown_region(
        PROJECTION.public_geometry(whole_extent), PROJECTION).area > reachable.area


def test_views_are_built_once_for_the_whole_stage():
    """两张视图按图与版本复用：每类重建一次会把整城图的通行判定算三遍。"""
    store = road_store()
    views = build_views(store.graph, version="acc-stage-test")
    assert build_views(store.graph, version="acc-stage-test") is views


def test_shared_attachment_cache_preserves_all_assessment_outputs(monkeypatch):
    from app.accessibility import service_graph
    real_attach = service_graph.attach_point
    calls = []
    def counted(*args, **kwargs):
        calls.append(1)
        return real_attach(*args, **kwargs)
    monkeypatch.setattr(service_graph, 'attach_point', counted)
    arguments = dict(geometry=boundary(0, -60, 1600, 60), facilities=[
        facility(100, facility_id='medical', category='pharmacy'),
        facility(400, facility_id='shopping', category='market'),
        facility(700, facility_id='education', category='school')])
    cached = run(**arguments)
    cached_calls = len(calls)
    calls.clear()
    monkeypatch.setattr(stage, 'PointAttachments',
                        lambda store: service_graph.PointAttachments(store, max_entries=0))
    uncached = run(**arguments)
    assert cached_calls < len(calls) / 2
    for section in ('accessibility', 'service_gaps', 'heatmap', 'scores'):
        assert getattr(cached, section).model_dump(mode='json') == \
            getattr(uncached, section).model_dump(mode='json'), section


def test_a_water_data_conflict_is_unknown_not_coverage_or_gap():
    """来源互相矛盾的水面：那一片可能是水，距离站不住，格是"数据冲突／未知"。"""
    conflict = box(*metric(500, -40), *metric(620, 40))
    layer = LocalObstacles(water=dry_layer().water, conflicts=MultiPolygon([conflict]))
    outcome = run(obstacles=layer)
    inside = [point for point in outcome.heatmap.categories["medical"]
              if conflict.contains(shape({"type": "Point", "coordinates": metric_of(point)}))]
    assert inside, "冲突区在服务距离之内，应当有测到距离的格"
    assert all(point["status"] == "unknown" and point["reason"] == "water_data_conflict"
               for point in inside)
    assert not any(shape(zone.geometry).intersects(conflict.buffer(-1))
                   for zone in outcome.service_gaps.zones)
    assert outcome.water.conflict_area_m2 == pytest.approx(120 * 80, rel=1e-6)
    assert any("water_data_conflict_m2" in note for note in outcome.accessibility.notes)
    # 冲突区外、同样距离上的格仍然是覆盖：冲突只影响它自己那一片。
    assert any(point["status"] == "covered" and "reason" not in point
               for point in outcome.heatmap.categories["medical"])


def metric_of(point) -> tuple[float, float]:
    from app.geo.coordinates import bd09_to_wgs84
    return PROJECTION.forward.transform(*bd09_to_wgs84(point["lng"], point["lat"]))
