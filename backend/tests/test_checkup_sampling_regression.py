"""五个固定坐标上的完整流程回归：换一个中心点，业务口径不该跟着变。

这五个点是随机抽出来的（种子 20261010，经度 121.46–121.55、纬度 31.24–31.33，各取六位
小数），抽出来后逐个跑过一遍真实的完整流程，所以它们是一条**跨坐标**的抽样回归，而不是
某一个夹具的快照。全部使用离线替身：路线是解析式几何，设施与路网是 Python 对象，没有一次
请求离开机器，也没有一点百度额度被动用。

每个点验的是同一组口径，也就是"业务逻辑是否正常"这句话的具体含义：

* 三个大类各查各的，计数按圈面裁剪（圈外的记录只作服务来源，不算发现）；
* ``C + G + U = A`` 对每个类别成立，A 是冻结的评估域面积；
* 各桶的消耗不超过各自的上限，且与实际派发出去的页面／路线数一致；
* 圈面在五个修订里逐字节相同，接口发出来的就是磁盘上那一版。

图上说"这个点覆盖多少"是模型结论，不是实地测量；本文件断言的是这条链路的自洽，
不是这五处的现实服务水平。
"""
import json

import networkx as nx
import pytest
from fastapi.testclient import TestClient
from shapely.geometry import LineString

from app.algorithms.osm_offline.graph_store import PEDESTRIAN_ATTRS, GraphStore
from app.catalog import default_analysis_majors, poi_keys
from app.geo.projection import MetricProjection
from life_circle.coordinates import LocalProjection
from test_checkup_facilities import (CORE_ACCEPTED, CORE_ACCEPTED_TOTAL, CORE_PAGES,
                                     SyntheticPlaces, SyntheticRoutes, body, document,
                                     every_page, make_app, run, straight_routes)
from test_checkup_report import VERSION, OfflineGraph

#: 抽样点（bd09ll，原样交给后端），以及这一趟的固定输入。
CENTERS = ((121.491517, 31.299588), (121.463796, 31.314841), (121.483141, 31.276126),
           (121.492101, 31.296429), (121.475626, 31.275345))
#: 设施夹具在中心、中心以东 500 米、以北 1600 米各放一条：前两处落在圈面里，
#: 第三处在查询范围内、圈面外（它是服务来源，不是发现）。
INSIDE_OFFSETS, OUTSIDE_OFFSET = ((0, 0), (500, 0)), (0, 1600)
CORE_MINORS = len(poi_keys(list(default_analysis_majors())))
#: 每个位置每个小类回一条，判进本类的有 ``CORE_ACCEPTED_TOTAL`` 条（只有负类 ``training``
#: 的样本不判进本类；``combined_school`` 在 #53 收窄 ``review`` 之后按名字判进本类）。
#: 两点在圈内、一点在圈外：所以"查到了"是两倍，
#: "圈外的服务来源"是一倍，而圈外的**记录**是全部小类各一条。
COUNTS = {category: 2 * count for category, count in CORE_ACCEPTED.items()}
OUTSIDE_RECORDS = CORE_MINORS


def sparse_road_grid(origin) -> GraphStore:
    """中心点周围 ±1600 米、每 200 米一个路口的路网替身。

    比 ``test_checkup_report`` 的那张 100 米格子粗，因为这份文件的目的是"换坐标看同一条
    链路"，不是路网密度；解析式圈面的半径约 1080 米，加 1300 米查询外扩，±1600 米足够
    让评估域里的格子都有路可走。
    """
    projection = MetricProjection("EPSG:32651")
    x0, y0 = projection.origin(origin)
    graph = nx.MultiDiGraph(crs="EPSG:32651", osm_data_version=VERSION, walking_speed_mps=1.3)
    coords = {(ix, iy): (x0 + dx, y0 + dy)
              for ix, dx in enumerate(range(-1600, 1601, 200))
              for iy, dy in enumerate(range(-1600, 1601, 200))}
    for node, (x, y) in coords.items():
        graph.add_node(node, x=x, y=y)
    for (ix, iy), point in coords.items():
        for step in ((1, 0), (0, 1)):
            other = (ix + step[0], iy + step[1])
            if other not in coords:
                continue
            line = LineString([point, coords[other]])
            for tail, head in (((ix, iy), other), (other, (ix, iy))):
                data = dict.fromkeys(PEDESTRIAN_ATTRS)
                data.update(geometry=line, length=line.length, travel_time_s=line.length / 1.3,
                            osmid=ix * 1000 + iy)
                graph.add_edge(tail, head, **data)
    return GraphStore(graph, speed=1.3, crs="EPSG:32651")


def sample_app(tmp_path, center):
    """一个跑在给定中心点上的离线体检应用：路线、设施、路网全是替身。"""
    projection = LocalProjection(center)
    points = [projection.to_geographic(offset) for offset in (*INSIDE_OFFSETS, OUTSIDE_OFFSET)]
    places = SyntheticPlaces(every_page(points))
    routes = SyntheticRoutes(straight_routes())
    app = make_app(tmp_path, places, routes=routes, osm_data_version=VERSION)
    app.state.checkups.offline = OfflineGraph(sparse_road_grid(center))
    return app, places, routes


@pytest.mark.parametrize("center", CENTERS)
def test_every_sampled_center_keeps_the_same_business_rules(tmp_path, center):
    app, places, routes = sample_app(tmp_path, center)
    with TestClient(app) as client:
        task_id, view = run(client, body(center={"lng": center[0], "lat": center[1]}))
        assert view["status"] == "completed", view
        assert view["stage"] == "ready" and view["revision"] == 5
        frozen = document(client, task_id)

    # 圈面、检索、评估、核验都跑完了；口径仍是"目录完整性未经独立核验"。
    assert frozen["isochrone"]["geometry"] is not None
    assert frozen["facilitiesStatus"] == "complete"
    assert frozen["businessStatus"] == "partial"
    group = frozen["facilities"]
    assert group["queryStatus"] == "completed"
    assert group["catalogCompleteness"] == "unverified"
    # 分类词典的版本写在修订里：这一次用的是修好 ``高等院校`` 的那一版。
    assert frozen["trace"]["ruleVersions"]["classification"] == "poi-categories-v2.3"
    assert {item["classificationRuleVersion"] for item in group["facilities"]} == \
        {"poi-categories-v2.3"}
    # 计数只算圈面里的：三处在查询范围内，只有两处落在圈面内。
    assert group["countsByCategory"] == COUNTS
    assert len(group["facilities"]) == sum(COUNTS.values())
    # 圈外那一条仍然被记下来：它是服务来源，永远不是发现（§2.3）。
    assert group["statistics"]["outsideBoundaryRecords"] == OUTSIDE_RECORDS
    assert group["statistics"]["nearbyServiceSources"] == CORE_ACCEPTED_TOTAL
    assert len(group["nearbyFacilities"]) == CORE_ACCEPTED_TOTAL
    assert all(item["classificationStatus"] == "accepted" for item in group["facilities"])

    # C + G + U = A：面积必须闭合到冻结的评估域上，允许的只是浮点误差。
    area = frozen["accessibility"]["domainAreaM2"]
    assert area > 0
    for row in frozen["accessibility"]["categories"]:
        assert row["supported"] is True, row
        error = abs(row["coveredM2"] + row["gapM2"] + row["unknownM2"] - area)
        assert error <= max(1.0, area * 1e-6), (row["category"], error)

    # 预算：实际派发数等于桶里的消耗，且都不越过上限。成圈的尝试由求解器自己数，
    # 设施与路线都是这张夹具里能核对的确定数。
    budgets = frozen["trace"]["budgets"]
    assert frozen["isochrone"]["requestsUsed"] == frozen["isochrone"]["statistics"]["requests"]
    assert 0 < frozen["isochrone"]["requestsUsed"] <= budgets["isochrone"]["limit"]
    assert budgets["poi"]["spent"] == len(places.sent) == CORE_PAGES
    assert budgets["poi"]["spent"] <= budgets["poi"]["limit"]
    assert budgets["route"]["spent"] == len(routes.sent) <= budgets["route"]["limit"]
    assert view["networkRequests"] == budgets["poi"]["spent"] + budgets["route"]["spent"]
    assert view["requests"] >= view["networkRequests"]

    # 修订不可变：圈面在五版里逐字节相同，接口发出来的就是磁盘上那一版。
    store = app.state.checkups.store
    revisions = store.revisions(task_id)
    assert [item["stage"] for item in revisions] == \
        ["isochrone", "poi", "accessibility", "verification", "reporting"]
    assert len({item["result_hash"] for item in revisions}) == 5
    documents = [json.loads((store.root / item["payload"]).read_text(encoding="utf-8"))
                 for item in revisions]
    assert all(item["isochrone"] == documents[0]["isochrone"] for item in documents)
    assert frozenset(item["trace"]["isochroneHash"] for item in documents) == \
        frozenset({documents[0]["trace"]["isochroneHash"]})
    assert frozen == documents[-1]
