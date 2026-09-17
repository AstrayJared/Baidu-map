import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest
from shapely.geometry import box, mapping

from app.analyses import RateGate
from app.facilities import analyze_facilities
from app.places import PlacesClient, classify
from life_circle.models import CancelToken, RouteObservation
from tools.test_origin import TEST_ORIGIN

ORIGIN = TEST_ORIGIN


@pytest.mark.parametrize("name,expected", [
    ("社区菜市场", "market"), ("第一农贸市场", "market"), ("便民菜场", "market"),
    ("联华超市", "supermarket"), ("生鲜超市", "supermarket"),
    ("第一药店", "pharmacy"), ("益民大药房", "pharmacy"), ("社区药房", "pharmacy"),
    ("医院药房", "hospital_pharmacy"), ("门诊药房", "hospital_pharmacy"),
    ("第一小学", "school"), ("实验小学", "school"), ("中心小学", "school"), ("附属小学", "school"), ("完全小学", "school"),
    ("小学培训机构", None), ("幼儿园", None), ("制药公司", None), ("医院", None), ("兽药店", None),
])
def test_classification(name, expected):
    assert classify(name) == expected


def poi(uid="x", name="社区药店"):
    return {"uid": uid, "name": name, "location": {"lng": ORIGIN[0]+.001, "lat": ORIGIN[1]}}


def test_rate_limit_stops_all_categories():
    async def run():
        calls = []
        def handle(r):
            calls.append((r.url.params["query"], r.url.params["page_num"]))
            assert r.url.params["coord_type"] == "3" and "ret_coordtype" not in r.url.params
            if len(calls) == 1:
                return httpx.Response(429)
            return httpx.Response(200, json={"status": 0, "total": 150, "results": [poi(str(i)) for i in range(20)]})
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            places = PlacesClient(client, "fixture", RateGate(None), CancelToken())
            items, metadata = await places.search(ORIGIN, 5000, time.monotonic()+20)
        assert places.requests == 1 and len(items) == 0
        assert all(m["status"] == "failed" and m["reason"] == "rate_limit" for m in metadata)
        assert "fixture" not in str(places.records)
    asyncio.run(run())


@pytest.mark.parametrize("distance,expected", [(899,"covered"), (999,"unknown"), (1000,"covered"), (1001,"unknown"), (1101,"unknown")])
def test_walking_distance_boundaries_with_same_analysis(distance, expected):
    async def run():
        def handle(r):
            if "place/v3" in r.url.path:
                rows = [poi()] if r.url.params["query"] == "药店" else []
                return httpx.Response(200, json={"status":0,"total":len(rows),"results":rows})
            start = {"lng":str(ORIGIN[0]),"lat":str(ORIGIN[1])}
            end = {"lng":str(ORIGIN[0]+.001),"lat":str(ORIGIN[1])}
            return httpx.Response(200,json={"status":0,"result":{"routes":[{"distance":distance,"duration":800,"steps":[{"start_location":start,"end_location":end,"path":f"{ORIGIN[0]},{ORIGIN[1]};{ORIGIN[0]+.001},{ORIGIN[1]}"}]}]}})
        geom = box(ORIGIN[0]-.01,ORIGIN[1]-.01,ORIGIN[0]+.01,ORIGIN[1]+.01)
        result = SimpleNamespace(config=SimpleNamespace(origin=ORIGIN,extent=1600),local_geometry=box(-1000,-1000,1000,1000),geometry=mapping(geom),sample_observations=[RouteObservation(ORIGIN,0)])
        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as client:
            facilities, categories, summary, report = await analyze_facilities(result, client, "fixture", RateGate(None), CancelToken())
        assert len(facilities) == 1 and facilities[0].in_circle
        medical = next(c for c in summary.assessments[0].categories if c.category=="medical")
        assert medical.status == expected
        assert summary.routes["x"].distance_m == distance
        assert "medical" in summary.service_blind_regions
        assert summary.service_blind_regions["medical"].type in ("Polygon", "MultiPolygon")
        assert summary.service_blind_regions["medical"].coordinates == []
        assert summary.network_requests == 6
        assert "不生成覆盖率" in report
    asyncio.run(run())


def test_failed_search_never_creates_blind_points():
    async def run():
        geom=box(ORIGIN[0]-.01,ORIGIN[1]-.01,ORIGIN[0]+.01,ORIGIN[1]+.01)
        result=SimpleNamespace(config=SimpleNamespace(origin=ORIGIN,extent=1600),local_geometry=box(-1000,-1000,1000,1000),geometry=mapping(geom),sample_observations=[RouteObservation(ORIGIN,0)])
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda r:httpx.Response(403))) as client:
            _,_,summary,_=await analyze_facilities(result,client,"fixture",RateGate(None),CancelToken())
        assert summary.status=="failed"
        assert all(c.status=="unknown" for c in summary.assessments[0].categories)
    asyncio.run(run())
