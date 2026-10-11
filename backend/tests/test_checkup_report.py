"""§5–§7.2 端到端：成圈 → 设施 → 服务覆盖 → 灰区 → 评分 → 报告 → 图层。

The facility transport is the synthetic one from ``test_checkup_facilities``, so
the run issues no HTTP; the walking network is a synthetic road grid installed
at the seam production resolves the OSM graph through
(``app.state.checkups.offline``). What is under test is the whole chain a reader
of the report depends on:

* the four stages publish in order and the last one is the report;
* A is frozen before it is divided, and C + G + U equals it for every category;
* a category with no facility is *unknown*, not 0% covered;
* every grey zone can answer what is missing, how big it is, why, and what to
  check next — and the layer drawn on the map is the same object that was
  counted, not a second reconstruction of it;
* the report pins the digest of the revision it summarises rather than its own,
  and carries the limitations it is not allowed to leave out.
"""
import json

import networkx as nx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString, shape

from app.algorithms.osm_offline.graph_store import PEDESTRIAN_ATTRS, GraphStore
from app.checkups import accessibility_stage as stage
from app.geo.projection import MetricProjection
from test_checkup_facilities import (ORIGIN, SyntheticPlaces, body, document, every_page,
                                     make_app, offset, run)

@pytest.fixture(autouse=True)
def three_category_query_fixture(monkeypatch):
    """Freeze complete three-minor evidence for geometry/report regressions."""
    from app import catalog
    selected = ('market', 'pharmacy', 'primary_school')
    monkeypatch.setattr(catalog, 'poi_keys', lambda majors=None: tuple(
        key for key in selected if majors is None or catalog.major_of(key) in majors))


CRS = "EPSG:32651"
SPEED = 1.3
#: Must match the deployment's ``osm_data_version``: the graph views are keyed by it.
VERSION = "checkup-report-test"
PROJECTION = MetricProjection(CRS)
X0, Y0 = PROJECTION.origin(ORIGIN)
#: A road grid that covers the whole 15-minute boundary and then some, so a cell
#: is never "unreachable" for lack of a network to walk on.
STEP, REACH = 100, 1600
MAJORS = ("shopping", "medical", "education")

#: 障碍层的输入是 wgs84，而用例里的偏移量都是米制。手写经纬度会把水体放到别处，
#: 所以按米制目标位置反投影回来。
WATERSIDE = [list(PROJECTION.inverse.transform(X0 + dx, Y0 + dy, errcheck=True))
             for dx, dy in ((1150, -25), (1250, -25), (1250, 25), (1150, 25))]


def road_grid() -> GraphStore:
    """A full lattice of two-way footways; every intersection is a node.

    The keys are index pairs and the coordinates live in the node attributes:
    an edge endpoint is a node id, so passing coordinates there would add a
    second, coordinate-less node for every edge.
    """
    graph = nx.MultiDiGraph(crs=CRS, osm_data_version=VERSION, walking_speed_mps=SPEED)
    coords = {}
    for ix, dx in enumerate(range(-REACH, REACH + 1, STEP)):
        for iy, dy in enumerate(range(-REACH, REACH + 1, STEP)):
            coords[(ix, iy)] = (X0 + dx, Y0 + dy)
            graph.add_node((ix, iy), x=coords[(ix, iy)][0], y=coords[(ix, iy)][1])
    for (ix, iy) in coords:
        for step in ((1, 0), (0, 1)):
            other = (ix + step[0], iy + step[1])
            if other not in coords:
                continue
            line = LineString([coords[(ix, iy)], coords[other]])
            for tail, head in (((ix, iy), other), (other, (ix, iy))):
                data = dict.fromkeys(PEDESTRIAN_ATTRS)
                data.update(geometry=line, length=line.length, travel_time_s=line.length / SPEED,
                            osmid=ix * 1000 + iy)
                graph.add_edge(tail, head, **data)
    return GraphStore(graph, speed=SPEED, crs=CRS)


class OfflineGraph:
    """The deployment's own walking network, standing in for the OSM cache.

    It is the same object the OSM engine would resolve, so the endpoint under
    test is the one production uses rather than a test-only parameter.
    """

    def __init__(self, store, coverage=None):
        self.store, self.coverage = store, coverage

    def get(self):
        return self


def make_report_app(tmp_path, points, **overrides):
    """An offline app whose assessment runs on the synthetic road grid."""
    app = make_app(tmp_path, SyntheticPlaces(every_page(points)), osm_data_version=VERSION,
                   **overrides)
    app.state.checkups.offline = OfflineGraph(road_grid())
    return app


def zone_areas(zones):
    return {zone["id"]: zone["areaM2"] for zone in zones}


# -- the chain ---------------------------------------------------------------

def test_the_pipeline_publishes_the_report_last_and_the_domain_divides_exactly(tmp_path):
    app = make_report_app(tmp_path, [ORIGIN, offset(700)])
    with TestClient(app) as client:
        task_id, view = run(client, body())
        assert view["status"] == "completed", view
        assert view["stage"] == "ready" and view["revision"] == 5
        # Every stage published its own revision, in order, and the report is
        # the last thing the task does.
        assert [item["stage"] for item in app.state.checkups.store.revisions(task_id)] == \
            ["isochrone", "poi", "accessibility", "verification", "reporting"]

        # 核验发的是新修订，但它一个字也不改评估：覆盖面积、灰区、热力与评分在核验
        # 修订里与评估修订逐字节相同。一条真实路线只能给某一格挂上"待细化"的冲突标
        # 记（那写在核验证据里），不能回头改一个已经发布的面积 —— 否则同一个域在两
        # 个修订里面积不同，报告引哪一版都对不上。
        store = app.state.checkups.store
        assessed = store.revision(task_id, 3)["snapshot"]
        verified = store.revision(task_id, 4)["snapshot"]
        for section in ("accessibility", "serviceGaps", "heatmap", "scores"):
            assert verified[section] == assessed[section], section
        assert verified["verification"] is not None
        assert assessed["verification"] is None
        # 核验修订换掉的只有核验那一节和它自己的摘要。
        assert verified["trace"]["resultHash"] != assessed["trace"]["resultHash"]
        assert verified["facilities"] == assessed["facilities"]

        document_ = document(client, task_id)
        assert document_["stage"] == "reporting"
        # The facilities were retrieved, so the assessment could run: this
        # deployment simply has no hard-obstacle layer, which is a limitation of
        # the result rather than a reason to withhold it.
        assert document_["facilitiesStatus"] == "complete"
        assert document_["accessibility"]["status"] == "partial"
        assert document_["businessStatus"] == "partial"
        assert document_["serviceGaps"]["obstacleLayerAvailable"] is False

        domain_area = document_["accessibility"]["domainAreaM2"]
        assert domain_area > 0
        assert document_["accessibility"]["gridStepM"] == 50
        assert document_["accessibility"]["searchCutoffM"] == 1100
        for item in document_["accessibility"]["categories"]:
            assert item["supported"] is True, item
            assert item["coveredM2"] + item["gapM2"] + item["unknownM2"] == pytest.approx(
                domain_area, abs=1.0)
            # This focused three-minor directory completes all keyword pages.
            # Its frozen domain contains coverage, measured gaps and uncertainty.
            assert item["coveredM2"] > 0 and item["gapM2"] > 0 and item["unknownM2"] > 0, item


def test_the_scores_are_intervals_over_the_frozen_domain(tmp_path):
    app = make_report_app(tmp_path, [ORIGIN, offset(700)])
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        scores = document_["scores"]
        assert scores["domainAreaM2"] == document_["accessibility"]["domainAreaM2"]
        assert scores["formula"]["identity"] == "C+G+U=A"
        for item in scores["categories"]:
            assert item["supported"] is True
            # The interval exists because unknown area exists: reporting only the
            # lower bound would present "not measured" as "not served".
            assert 0 <= item["coverageLowerPct"] < item["coverageUpperPct"] <= 100
            assert item["intervalWidthPct"] == pytest.approx(
                item["coverageUpperPct"] - item["coverageLowerPct"])
            assert item["assessablePct"] + item["unknownPct"] == pytest.approx(100, abs=1e-6)
            assert item["assessablePct"] > item["coverageLowerPct"]
        overall = scores["overall"]
        assert overall["available"] is False
        assert overall["reason"] == "categories_not_analysed"
        assert len(overall["missingCategories"]) == 7


def test_every_grey_zone_explains_itself_and_the_layer_draws_the_counted_geometry(tmp_path):
    app = make_report_app(tmp_path, [ORIGIN, offset(700)])
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        gaps = document_["serviceGaps"]
        assert gaps["gapAreaM2"] > 0 and gaps["zones"]
        assert gaps["compositeMinCategories"] == 2
        # All three majors are missing the same far region, so it is one
        # composite zone as well as one zone per category.
        assert gaps["compositeAreaM2"] > 0
        assert any(zone["kind"] == "composite" for zone in gaps["zones"])

        for zone in gaps["zones"]:
            # A zone that cannot say why it is one, how big it is and what to do
            # next is not checkable by the person reading the report.
            assert zone["reason"] and zone["suggestion"], zone
            assert zone["areaM2"] > 0 and zone["cellIds"] and zone["parts"] >= 1
            assert zone["queryStatus"] == "complete"
            assert zone["evidenceGrade"] == "model" and zone["suspected"] is True
            assert zone["nearestFacility"], "量得到最近设施的区域要报出是哪一家"
            assert zone["geometrySystem"] == "metric"
            # The counted area and the drawn shape are the same object: the
            # metric geometry is what areaM2 was taken from.
            assert shape(zone["geometry"]).area == pytest.approx(zone["areaM2"], rel=1e-9)
            # And the drawn bd09 shape is that same object, not a re-projection
            # of a separately built one.
            assert shape(zone["displayGeometry"]).area > 0

        layer = client.get(f"/api/v2/checkups/{task_id}/layers/service_gaps")
        assert layer.status_code == 200
        collection = layer.json()["geometry"]
        assert collection["type"] == "FeatureCollection"
        assert collection["coordinateSystem"] == "bd09ll"
        # One feature per zone, and the same area in both places — a layer that
        # quietly drops a zone would disagree with the number beside it.
        assert len(collection["features"]) == len(gaps["zones"])
        assert collection["properties"]["gapAreaM2"] == gaps["gapAreaM2"]
        assert {feature["properties"]["id"] for feature in collection["features"]} == \
            set(zone_areas(gaps["zones"]))
        by_id = {zone["id"]: zone for zone in gaps["zones"]}
        for feature in collection["features"]:
            # 图层画的是 bd09ll 的展示几何，所以要比的是"把它投回米制之后的面积"。
            # 直接拿度数面积去比，得到的差值小到看着像通过（十万分之几的度² 对几十
            # 万平方米），一条对不上的几何会被读成"只是精度问题"。
            counted = by_id[feature["properties"]["id"]]
            drawn = stage.metric_region(feature["geometry"], PROJECTION)
            assert drawn.area == pytest.approx(counted["areaM2"], rel=1e-4)
            assert feature["properties"]["areaM2"] == counted["areaM2"]
            assert "geometry" not in feature["properties"]


def test_the_heatmap_reports_measured_distances_and_distance_free_gaps(tmp_path):
    app = make_report_app(tmp_path, [ORIGIN, offset(700)])
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        heat = document_["heatmap"]
        assert heat["metric"] == "walking_route" and heat["estimated"] is True
        assert heat["stepM"] == 50
        # A model estimate, and the report says so rather than presenting it as
        # measured walking time or as population coverage.
        assert any("模型估计" in note for note in heat["notes"])
        for category in MAJORS:
            points = heat["categories"][category]
            assert points, category
            for point in points:
                # A gap cell has no model path within the cutoff: a point without a
                # distance, never a zero-distance hot spot.
                assert (point["status"] == "gap" if point["distanceM"] is None
                        else point["distanceM"] >= 0)
                assert point["cell"]
                assert point["status"] in ("covered", "gap", "unknown")
                assert -180 <= point["lng"] <= 180 and -90 <= point["lat"] <= 90

        layer = client.get(f"/api/v2/checkups/{task_id}/layers/heatmap").json()["geometry"]
        assert layer["properties"]["metric"] == "walking_route"
        assert {feature["properties"]["category"] for feature in layer["features"]} == set(MAJORS)
        assert len(layer["features"]) == sum(len(heat["categories"][c]) for c in MAJORS)


def test_the_boundary_and_the_domain_layers_are_served_under_the_same_revision(tmp_path):
    app = make_report_app(tmp_path, [ORIGIN, offset(700)])
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        result_hash = document_["trace"]["resultHash"]

        boundary = client.get(f"/api/v2/checkups/{task_id}/layers/isochrone")
        domain = client.get(f"/api/v2/checkups/{task_id}/layers/accessibility")
        assert boundary.status_code == domain.status_code == 200
        assert boundary.json()["resultHash"] == domain.json()["resultHash"] == result_hash
        # Two representations of one revision: a validator for one must not tell
        # a client its copy of the other is current.
        assert boundary.headers["etag"] != domain.headers["etag"]

        # The domain layer is the denominator itself, with the areas it was
        # divided into — without it a reader cannot tell 100% of a boundary from
        # 100% of a region that was never assessed.
        collection = domain.json()["geometry"]
        assert collection["type"] == "FeatureCollection"
        feature = collection["features"][0]
        assert feature["geometry"]["type"] in ("Polygon", "MultiPolygon")
        properties = feature["properties"]
        assert properties["domainAreaM2"] == document_["accessibility"]["domainAreaM2"]
        assert properties["gridStepM"] == 50
        assert {item["category"] for item in properties["categories"]} == set(MAJORS)
        assert len(properties["categories"]) == len(
            document_["accessibility"]["categories"])
        counted = PROJECTION.public_geometry(
            stage.metric_region(feature["geometry"], PROJECTION).buffer(0), repair_roundoff=True)
        assert counted  # the drawn domain is a valid public geometry, not a shell


def test_the_report_pins_the_revision_it_summarises_and_keeps_its_limitations(tmp_path):
    app = make_report_app(tmp_path, [ORIGIN, offset(700)])
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        report = document_["report"]
        revisions = app.state.checkups.store.revisions(task_id)
        assert report["reportId"] == f"{task_id}:5"
        assert report["schemaVersion"] == "checkup-v1"
        # The report describes the revision it repeats. Its own digest would make
        # the document describe itself and no reader could recompute it.
        assert report["evidence"]["sourceResultHash"] == revisions[3]["result_hash"]
        assert report["evidence"]["sourceResultHash"] != document_["trace"]["resultHash"]
        assert report["evidence"]["obstacleLayerAvailable"] is False
        assert report["evidence"]["catalogCompleteness"] == "unverified"
        assert report["domainAreaM2"] == document_["accessibility"]["domainAreaM2"]

        # One row per category, with the areas and the interval in the same
        # place: a reader must not have to join two sections to see one number.
        assert {item["category"] for item in report["categories"]} == set(MAJORS)
        for item in report["categories"]:
            assert item["coveredM2"] + item["gapM2"] + item["unknownM2"] == pytest.approx(
                report["domainAreaM2"], abs=1.0)
            assert item["coverageLowerPct"] < item["coverageUpperPct"]
            assert item["cells"]["total"] > 0 if "total" in item["cells"] else True
        # The section a missing stage would otherwise leave out, present and
        # named: an omitted section reads as "there was nothing to say". This
        # deployment answers routes, so it is available and says who answered.
        verification = report["verification"]
        assert verification["available"] is True
        assert verification["status"] == "complete"
        assert verification["provider"] == "synthetic:checkup-routes"
        assert verification["checked"] == verification["queries"]["candidates"] == 6
        assert verification["queries"]["unverified"] == 0
        assert verification["conflicts"] == []
        # Every facility that was asked carries its own verdict and its own
        # evidence grade — the section is a list of answers, not a summary.
        assert [item["evidenceGrade"] for item in verification["facilities"]] == \
            ["verified"] * 6
        assert all(item["withinRule"] is True for item in verification["facilities"])
        # The distance the report repeats is the strict one, on the 700 m facility:
        # 700 m straight × 1.15 roads = 805 m, inside the 1000 m rule.
        far = next(item for item in verification["facilities"]
                   if item["facilityId"] == "synthetic:market-1")
        assert far["routeDistanceM"] == pytest.approx(805.05, abs=0.1)
        assert far["poiStatus"] == "verified_reachable"
        assert report["gaps"]["status"] == "partial"
        limitations = report["limitations"]
        assert any("目录完整性" in item for item in limitations)
        assert any("不构成建设" in item for item in limitations)
        assert any("15 分钟" in item for item in limitations)

        layer = client.get(f"/api/v2/checkups/{task_id}/layers/report")
        assert layer.status_code == 200
        assert layer.json()["document"]["reportId"] == report["reportId"]
        assert layer.json()["geometry"] is None and layer.json()["displayGeometry"] is None


def test_a_deployment_without_a_graph_still_reports_the_other_stages(tmp_path):
    """No walking network: the assessment says so by name, and the report is still
    a document rather than a failure — a 0% here would be a measurement nobody made."""
    app = make_app(tmp_path, SyntheticPlaces(every_page([ORIGIN])))
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        assert document_["facilitiesStatus"] == "complete"
        assert document_["accessibility"]["status"] == "failed"
        assert document_["accessibility"]["domain"] is None
        assert {item["unavailableReason"] for item in document_["accessibility"]["categories"]} \
            == {"osm_graph_unavailable"}
        assert document_["accessibilityStatus"] == "failed"
        assert document_["serviceGaps"] is None and document_["scores"] is None
        assert client.get(f"/api/v2/checkups/{task_id}/layers/accessibility").json()["code"] == \
            "checkup_domain_unavailable"
        # The report exists, states which stage is missing, and carries no
        # interval the assessment never produced.
        report = document_["report"]
        assert report["domainAreaM2"] is None and report["overall"] is None
        assert all(item["coverageLowerPct"] is None for item in report["categories"])
        assert report["gaps"]["status"] == "failed"
        # 没有图就没有视图，也就没有"多少条边可通行"这种数字可报：空集合而不是一份
        # 看起来跑过一遍的统计。
        assert report["evidence"]["views"] == {}
        assert report["evidence"]["grid"]["searchCutoffM"] == 1100
        # The business status is not "partial": the checkup cannot answer the
        # question it exists for.
        assert document_["businessStatus"] == "insufficient"


def test_the_obstacle_layer_reaches_the_merge_and_its_absence_is_stated(tmp_path):
    """障碍层真的进了合并环节：有水体时这一版不再降级为部分完成，说明里也不再出现
    "障碍层不可用"。只断言接线与状态，不断言具体哪两格被分开 —— 那是
    ``test_service_zones`` 与 ``test_accessibility_stage`` 的判据。
    """
    app = make_report_app(tmp_path, [ORIGIN, offset(700)])
    with TestClient(app) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        gaps = document_["serviceGaps"]
        assert gaps["zones"], "没有障碍层时也要有灰区"
        assert gaps["obstacleLayerAvailable"] is False
        # 没有障碍层是"这一阶段没做完"，所以整套结论降为部分完成：灰区没有按水体分开，
        # 跨河两岸可能被并成一片，而这一点必须写在报告里，而不是留给读者去猜。
        assert document_["accessibility"]["status"] == "partial"
        assert "hard_obstacle_layer_unavailable" in "\n".join(gaps["notes"])
        assert document_["report"]["evidence"]["obstacleLayerAvailable"] is False

    configured_root = tmp_path / "configured"
    configured_root.mkdir()
    water = configured_root / "obstacles.json"
    water.write_text(json.dumps({
        "schema_version": 1, "coordinate_system": "wgs84", "osm_data_version": VERSION,
        # 一块落在评估域东缘外侧的窄水面。障碍层必须是 wgs84，而本用例的坐标都是
        # bd09ll，所以这里按米制目标位置反投影回去 —— 手写经纬度会把水体放到别处。
        "features": [{"type": "Feature", "properties": {"kind": "water", "osm_id": 1},
                      "geometry": {"type": "Polygon",
                                   "coordinates": [WATERSIDE + [WATERSIDE[0]]]}}]}),
        encoding="utf-8")
    configured = make_report_app(configured_root, [ORIGIN, offset(700)],
                                 hybrid_obstacle_path=water)
    with TestClient(configured) as client:
        task_id, _view = run(client, body())
        document_ = document(client, task_id)
        gaps = document_["serviceGaps"]
        # 障碍层可用、检索完整、没有被裁掉的面积：这一版是完整结论，而不是部分完成。
        assert gaps["obstacleLayerAvailable"] is True
        assert document_["accessibility"]["status"] == "complete"
        assert document_["businessStatus"] == "partial"
        assert "hard_obstacle_layer_unavailable" not in "\n".join(gaps["notes"])
        assert document_["report"]["evidence"]["obstacleLayerAvailable"] is True


def test_the_report_states_which_scope_the_overall_score_covers():
    """总体分的口径必须被渲染出来，不能只留在一个没人读的字段里。

    三种情形各有各的说法：只列核心范围时不需要额外说明；列了扩展类别时必须写明
    那个总分没把它们算进去；给不出总体分时必须点名缺了哪几类。
    """
    from app.checkups.reporting_stage import _scope_limitations

    core = ("shopping", "medical", "education")
    core_rows = [{"category": category} for category in core]
    assert _scope_limitations({"categories": core_rows,
                               "overall": {"available": True}}) == ()

    widened = _scope_limitations({
        "categories": core_rows + [{"category": "dining"}],
        "overall": {"available": True, "weights": {name: 1 / 3 for name in core}}})
    assert len(widened) == 1
    assert "不进入总体分" in widened[0] and "各占 1/3" in widened[0]

    refused = _scope_limitations({
        "categories": [{"category": "shopping"}, {"category": "medical"}],
        "overall": {"available": False, "reason": "categories_not_analysed",
                    "missingCategories": ["education"]}})
    assert len(refused) == 1
    assert "教育" in refused[0] and "不能由已分析的类别重新加权得到" in refused[0]
