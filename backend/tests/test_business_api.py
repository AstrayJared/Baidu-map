import copy
import math
import time

import pytest
import httpx
from fastapi.testclient import TestClient

from app import catalog
from app.config import Settings
from app.contracts import TaskResultResponse
from app.main import create_app


@pytest.mark.parametrize("shift,seconds,expected", [(0,500,"verified_reachable"), (0,1000,"verified_unreachable"), (.001,500,"pending")])
def test_full_http_business_chain_and_server_owned_route_ids(monkeypatch, shift, seconds, expected):
    actual_client = httpx.AsyncClient
    calls = []
    def handle(r):
        calls.append(r.url.path)
        if "place/v3" in r.url.path:
            name = r.url.params["query"]
            return httpx.Response(200,json={"status":0,"total":1,"results":[{"uid":name,"name":"测试"+name,"location":{"lng":116.405,"lat":39.915}}]})
        a,b = [tuple(map(float,r.url.params[k].split(",")))[::-1] for k in ("origin","destination")]
        distance = math.dist(a,b)*111000
        duration = distance/1.2
        if "destination_uid" in r.url.params:
            duration = seconds
            b = (b[0]+shift, b[1])
        return httpx.Response(200,json={"status":0,"result":{"routes":[{"duration":duration,"distance":distance,"steps":[{"start_location":{"lng":str(a[0]),"lat":str(a[1])},"end_location":{"lng":str(b[0]),"lat":str(b[1])},"path":f"{a[0]},{a[1]};{b[0]},{b[1]}"}]}]}})
    monkeypatch.setattr("app.analyses.httpx.AsyncClient",lambda **kw:actual_client(transport=httpx.MockTransport(handle),**kw))
    # ANALYSIS_QPS only lowers the shared direction gate's tier; the tier itself is raised
    # (and kept from switching on the calendar) so the offline chain runs at full speed.
    app=create_app(Settings(_env_file=None,analysis_provider="baidu",baidu_map_ak="test-secret",analysis_qps=10000,
                            baidu_direction_qps=10000,baidu_place_qps=10000,
                            baidu_quota_fallback_at="2099-01-01T00:00:00+08:00"))
    with TestClient(app) as client:
        task=client.post("/api/analyses",json={"center":{"lng":116.404,"lat":39.915},"coordinateSystem":"bd09ll","budget":200,"clientRequestId":"business"}).json()["taskId"]
        for _ in range(500):
            state=client.get(f"/api/analyses/{task}").json()
            if state["status"] in ("completed","failed"):
                break
            time.sleep(.01)
        assert state["status"]=="completed"
        result=client.get(f"/api/analyses/{task}/result").json()
        TaskResultResponse.model_validate(result)
        assert result["facilityAnalysis"]["assessed_points"]==9
        assert {f["category"] for f in result["data"]["facilities"]} == {
            c.key for c in catalog.CATEGORIES if not c.negative} and result["data"]["report"]
        assert result["status"]=="partial" and "test-secret" not in str(result)
        before=len(calls)
        cached=next(iter(result["facilityAnalysis"]["routes"]))
        cached_response = client.post(f"/api/analyses/{task}/routes/{cached}")
        assert cached_response.status_code==200
        strict = cached_response.json()["poiEvidence"]
        assert strict["status"] == expected and strict["observedDuration"] == seconds
        assert strict["duration"] == (None if expected == "pending" else seconds)
        assert next(f for f in result["data"]["facilities"] if f["id"] == cached)["poiEvidence"] == strict
        assert all(f["poiEvidence"]["status"] in (expected, "pending") for f in result["data"]["facilities"])
        assert len(calls)==before
        assert client.post(f"/api/analyses/{task}/routes/not-stored").status_code==404
        stored=app.state.analyses.jobs[task].result["data"]["facilities"]
        for i in range(4):
            item=copy.deepcopy(stored[0]);item["id"]=f"extra-{i}";item["poiEvidence"]=None;stored.append(item)
            assert client.post(f"/api/analyses/{task}/routes/extra-{i}").status_code==(200 if i<3 else 429)
        assert len(calls)==before+3
        refreshed = client.get(f"/api/analyses/{task}/result").json()
        for i in range(3):
            fid = f"extra-{i}"
            evidence = refreshed["facilityAnalysis"]["routes"][fid]["poiEvidence"]
            assert evidence["status"] == expected and evidence["facilityId"] == fid
            assert next(f for f in refreshed["data"]["facilities"] if f["id"] == fid)["poiEvidence"] == evidence
