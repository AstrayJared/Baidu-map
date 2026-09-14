"""Offline checks for the validation instrument, not Baidu accuracy tests."""
import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, patch

import httpx
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]/"scripts"))
from validate_osm_baidu import classify, confusion_matrix, metrics, query_baidu, safe_routes
from app.config import Settings

ORIGIN = (121.51108,31.20415)
DEST = (121.513104,31.207251)


def row(osm=True, baidu=None):
    return dict(id="test", group="road", lng=DEST[0], lat=DEST[1], osm_inside=osm, baidu_inside=baidu)


def payload(duration=900, endpoints=True):
    route = dict(duration=duration, distance=1000)
    if endpoints:
        route["steps"] = [dict(start_location=dict(lng=ORIGIN[0],lat=ORIGIN[1]),
                               end_location=dict(lng=DEST[0],lat=DEST[1]))]
    return dict(status=0,result=dict(routes=[route]))


def test_matrix_axes_and_metrics():
    rows = [row(True,True)] + [row(True,False)]*2 + [row(False,True)]*3 + [row(False,False)]*4 + [row()]
    assert confusion_matrix(rows) == [[1,2],[3,4]]
    m = metrics(rows)
    assert m["valid"] == 10 and m["unknown"] == 1
    assert m["false_inclusion_rate"] == 2/3
    assert m["false_exclusion_rate"] == 3/4
    assert m["accuracy"] == .5


def test_f1_zero_not_null_and_empty_null():
    assert metrics([row(True,False),row(False,True)])["f1"] == 0
    assert metrics([row()])["f1"] is None
    assert classify(row(False,None)) == "unknown"


@pytest.mark.parametrize("duration,endpoints,expected",[(900,True,True),(901,True,False),(900,False,None)])
def test_live_contract_and_strict_endpoint_gate(tmp_path,duration,endpoints,expected):
    seen = []
    def send(request):
        seen.append(dict(request.url.params))
        return httpx.Response(200,json=payload(duration,endpoints))
    settings = Settings(_env_file=None,baidu_map_ak="SYNTHETIC-KEY")
    run = asyncio.run(query_baidu(settings,[row()],ORIGIN,tmp_path,transport=httpx.MockTransport(send)))
    assert run["cases"][0]["baidu_inside"] is expected
    assert seen[0]["coord_type"] == "bd09ll"
    assert seen[0]["origin"] == "31.204150,121.511080"
    assert seen[0]["destination"] == "31.207251,121.513104"
    assert "SYNTHETIC-KEY" not in (tmp_path/"run.json").read_text()


def test_endpoint_offset_excluded_with_numeric_evidence(tmp_path):
    data = payload()
    data["result"]["routes"][0]["steps"][0]["end_location"]["lat"] += .01
    settings = Settings(_env_file=None,baidu_map_ak="SYNTHETIC-KEY")
    run = asyncio.run(query_baidu(settings,[row()],ORIGIN,tmp_path,
                     transport=httpx.MockTransport(lambda req: httpx.Response(200,json=data))))
    result = run["cases"][0]
    assert result["baidu_inside"] is None
    assert result["baidu_reason"] == "endpoint_offset"
    assert result["route_candidates"][0]["end_offset_m"] > 50
    assert result["route_candidates"][0]["duration"] == 900


@pytest.mark.parametrize("failure",["transport","quota"])
def test_systemic_failure_stops_later_requests(tmp_path,failure):
    sends = []
    def send(req):
        sends.append(1)
        if failure == "transport":
            raise httpx.ConnectError("secret URL must not be serialized", request=req)
        return httpx.Response(200,json=dict(status=301))
    settings = Settings(_env_file=None,baidu_map_ak="SYNTHETIC-KEY")
    run = asyncio.run(query_baidu(settings,[row(),row()],ORIGIN,tmp_path,transport=httpx.MockTransport(send)))
    assert run["state"] == "blocked" and len(sends) == 1
    assert not run["cases"][1]["attempted"]
    assert run["cases"][1]["baidu_reason"] == "not_sent"
    assert "secret URL" not in (tmp_path/"run.json").read_text()


def test_no_retries_and_endpoint_counts(tmp_path):
    settings = Settings(_env_file=None,baidu_map_ak="SYNTHETIC-KEY")
    with patch("validate_osm_baidu.asyncio.sleep",new_callable=AsyncMock) as sleep:
        run = asyncio.run(query_baidu(settings,[row(),row()],ORIGIN,tmp_path,
                         transport=httpx.MockTransport(lambda req:httpx.Response(200,json=payload()))))
    assert len(run["cases"]) == 2 and run["retries"] == 0
    assert sleep.await_args.args == (.51,)


def test_malformed_response_safely_allowlisted():
    assert safe_routes({"result":None},ORIGIN,DEST) == []
    data = payload()
    data["result"]["routes"][0]["private"] = "ignored"
    result = safe_routes(data,ORIGIN,DEST)
    assert "private" not in result[0]


def test_plot_path_keeps_hole_with_opposite_winding():
    import numpy as np
    from shapely.geometry import Polygon
    from matplotlib.path import Path as MP
    from render_osm_baidu_report import polygon_path
    p=Polygon([(0,0),(10,0),(10,10),(0,10)],holes=[[(2,2),(8,2),(8,8),(2,8)]])
    path=polygon_path(p,np.array([0,0]))
    assert list(path.codes).count(MP.MOVETO)==2
    starts=np.flatnonzero(path.codes==MP.MOVETO)
    rings=[path.vertices[starts[0]:starts[1]],path.vertices[starts[1]:]]
    signed=lambda r:sum(a[0]*b[1]-b[0]*a[1] for a,b in zip(r,r[1:]))
    assert signed(rings[0])>0 and signed(rings[1])<0
