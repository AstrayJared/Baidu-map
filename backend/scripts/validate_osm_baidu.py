"""Frozen 60-point OSM/Baidu validation: prepare locally, execute once, render offline."""
from __future__ import annotations

import argparse
import asyncio
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import logging
from pathlib import Path
import subprocess
import sys
import time

BACKEND = Path(__file__).resolve().parents[1]
ROOT = BACKEND.parent
sys.path.insert(0, str(BACKEND))

import httpx
import numpy as np
from shapely.geometry import Point, box, mapping
from app.config import Settings, load_settings
from app.persistence import atomic_dump
from app.algorithms.osm_offline.engine import OsmOfflineEngine
from life_circle.models import IsochroneRequest
from life_circle.providers import BaiduProvider
from life_circle.coordinates import LocalProjection

ORIGIN = (121.51108, 31.20415)
THRESHOLD_S = 900
DEFAULT_OUTPUT = BACKEND / "docs/reviews/2026-09-14/osm-baidu-validation"
FROZEN_SOURCE = DEFAULT_OUTPUT / "plan.json"


def utc_now():
    return datetime.now(timezone.utc).isoformat()


def file_sha(path):
    with Path(path).open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def plan_sha(plan):
    return hashlib.sha256(json.dumps(plan, sort_keys=True, ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def prepare(output, source):
    """Freeze previously selected endpoints before any new Baidu observations."""
    output.mkdir(parents=True, exist_ok=False)
    old = json.loads(source.read_text(encoding="utf-8"))
    assert len(old["cases"]) == 60
    settings = Settings(_env_file=None, osm_data_version="geofabrik-shanghai-20260912",
                        osm_graph_cache_path=ROOT / "data/osm/shanghai.osm-cache",
                        osm_coverage_boundary_path=ROOT / "data/osm/shanghai.poly")
    print("Loading local OSM snapshot...", flush=True)
    engine = OsmOfflineEngine.load(settings)
    if engine.store is None or engine.coverage is None:
        raise RuntimeError(engine.unavailable_reason or engine.coverage_reason)
    result = engine.compute(IsochroneRequest(ORIGIN, "bd09ll", config_version="osm-offline-v1"))
    assert result.result.quality == "usable"
    projection = engine.store.projection
    origin_xy = projection.origin(ORIGIN)
    rows = []
    for old_row in old["cases"]:
        # Classify the exact six-decimal coordinates sent to Baidu, not the
        # unrounded metric point used by the previous plotting script.
        dest = (old_row["lng"], old_row["lat"])
        x, y = projection.origin(dest)
        point = Point(x, y)
        assert engine.coverage.covers(point)
        rows.append(dict(id=old_row["id"], group=old_row["group"], lng=dest[0], lat=dest[1],
                         metric_x=x, metric_y=y, offset_e_m=x-origin_xy[0], offset_n_m=y-origin_xy[1],
                         osm_inside=bool(result.result.local_geometry.covers(point)),
                         osm_network_distance_m=result.reachable_network.distance(point)))
    assert len({(r["lng"], r["lat"]) for r in rows}) == 60
    extent = box(origin_xy[0]-2100, origin_xy[1]-2100, origin_xy[0]+2100, origin_xy[1]+2100)
    roads = {}
    for i in engine.store.index.query(extent, predicate="intersects"):
        line = engine.store.geometries[int(i)].intersection(extent)
        roads[line.normalize().wkb.hex()] = mapping(line)
    atomic_dump(output/"geometry.json", dict(origin_xy=origin_xy, polygon=mapping(result.result.local_geometry),
                network=mapping(result.reachable_network), snap=mapping(result.snap_point), roads=list(roads.values())))
    plan = dict(prepared_at=utc_now(), origin_bd09ll=ORIGIN, threshold_seconds=900, qps=2,
                retries=0, cases=rows, old_evidence_sha256=file_sha(source),
                sampling="Previous OSM-conditioned 20 road + 20 buffer-boundary exterior + 20 outer-ring endpoints; no resampling after Baidu results.",
                validity="HTTP 200, Baidu status 0, finite duration, both route endpoints present and within 50 metres.",
                acceptance="At least 50 valid references and at least 10 in each Baidu class; no pre-agreed accuracy pass threshold.",
                git_revision=subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip(),
                script_sha256=file_sha(__file__), cache_sha256=file_sha(settings.osm_graph_cache_path),
                geometry_sha256=file_sha(output/"geometry.json"),
                coverage_sha256=file_sha(settings.osm_coverage_boundary_path), osm=result.diagnostics)
    atomic_dump(output/"plan.json", plan)
    print(json.dumps(dict(prepared=60, osm_inside=sum(r["osm_inside"] for r in rows),
                          plan_sha256=plan_sha(plan))), flush=True)


def safe_routes(payload, origin, destination):
    """Allowlisted route fields for diagnosing endpoint rejection; never URLs."""
    routes = payload.get("result", {}).get("routes", []) if isinstance(payload, dict) and isinstance(payload.get("result"), dict) else []
    scale = LocalProjection(origin)
    result = []
    for route in routes if isinstance(routes, list) else []:
        if not isinstance(route, dict):
            continue
        row = {}
        for key in ("duration", "distance"):
            value = route.get(key)
            if type(value) in (int, float) and np.isfinite(value) and value >= 0:
                row[key] = value
        steps = route.get("steps")
        if isinstance(steps, list) and steps:
            for key, step, field, target in (("start", steps[0], "start_location", origin),
                                             ("end", steps[-1], "end_location", destination)):
                p = BaiduProvider._endpoint(step.get(field)) if isinstance(step, dict) else None
                row[key] = p
                row[key+"_offset_m"] = float(np.linalg.norm(np.array(scale.to_local(p))-scale.to_local(target))) if p else None
        result.append(row)
    return result


async def query_baidu(settings, cases, origin, output, *, transport=None):
    """One attempt per fixed endpoint; durable progress before and after send."""
    logging.getLogger("httpx").disabled = True
    logging.getLogger("httpcore").disabled = True
    run = dict(started_at=utc_now(), state="running", cases=[], qps=2, retries=0)
    rows = [dict(r, attempted=False, baidu_inside=None, endpoint_verified=False,
                 baidu_reason="not_sent", baidu_duration_s=None) for r in cases]
    run["cases"] = rows
    atomic_dump(output/"run.json", run)
    async with httpx.AsyncClient(transport=transport, trust_env=False, follow_redirects=False) as client:
        provider = BaiduProvider(settings.baidu_map_ak.get_secret_value(), client=client)
        tick0 = time.perf_counter()
        for index, row in enumerate(rows):
            # Serial, wait after the preceding response: at most two starts in
            # a rolling second, with a small timing margin.
            if index:
                await asyncio.sleep(.51)
            dest = (row["lng"], row["lat"])
            row.update(attempted=True, sent_at=utc_now(), start_offset_s=time.perf_counter()-tick0,
                       baidu_reason="attempt_incomplete")
            atomic_dump(output/"run.json", run)  # failure here prevents sending
            started = time.perf_counter()
            payload = None
            params = dict(ak=provider._ak, origin=f"{origin[1]:.6f},{origin[0]:.6f}",
                          destination=f"{dest[1]:.6f},{dest[0]:.6f}", coord_type="bd09ll",
                          ret_coordtype="bd09ll", steps_info="1")
            try:
                response = await client.get(provider.endpoint, params=params, timeout=8)
                row["http_status"] = response.status_code
                try:
                    payload = response.json()
                except ValueError:
                    pass
                row["baidu_status"] = payload.get("status") if isinstance(payload, dict) and type(payload.get("status")) is int else None
                if response.status_code == 200:
                    observation = provider.parse(payload, origin, dest)
                    row["route_candidates"] = safe_routes(payload, origin, dest)
                    row["baidu_duration_s"] = observation.duration
                    row["baidu_distance_m"] = observation.distance_m
                    row["endpoint_verified"] = bool(observation.duration is not None and observation.endpoint_verified)
                    row["baidu_reason"] = observation.reason or (None if observation.endpoint_verified else "endpoint_missing")
                    if row["endpoint_verified"]:
                        row["baidu_inside"] = observation.duration <= 900
                else:
                    row["baidu_reason"] = "http_error"
            except httpx.RequestError as exc:
                row.update(baidu_reason="transport_error", transport_error=type(exc).__name__)
            row["elapsed_ms"] = (time.perf_counter()-started)*1000
            atomic_dump(output/"run.json", run)  # never continue after storage failure
            print(json.dumps(dict(case=index+1, http=row.get("http_status"), status=row.get("baidu_status"),
                                  valid=row["baidu_inside"] is not None, reason=row["baidu_reason"])), flush=True)
            if row["baidu_reason"] in {"transport_error","permission","quota","rate_limit","invalid_parameter","http_error","upstream_status"}:
                run["state"] = "blocked"
                break
        else:
            run["state"] = "completed"
        run.update(finished_at=utc_now(), elapsed_seconds=time.perf_counter()-tick0)
    atomic_dump(output/"run.json", run)
    return run


def classify(row):
    if row.get("baidu_inside") is None:
        return "unknown"
    return ("tp" if row["baidu_inside"] else "fp") if row["osm_inside"] else ("fn" if row["baidu_inside"] else "tn")


def metrics(rows):
    counts = Counter(classify(r) for r in rows)
    tp, tn, fp, fn = (counts[k] for k in ("tp","tn","fp","fn"))
    valid = tp+tn+fp+fn
    div = lambda a,b: a/b if b else None
    return dict(total=len(rows), valid=valid, unknown=len(rows)-valid, tp=tp,tn=tn,fp=fp,fn=fn,
                accuracy=div(tp+tn,valid), precision=div(tp,tp+fp), recall=div(tp,tp+fn),
                false_inclusion_rate=div(fp,tp+fp), false_exclusion_rate=div(fn,tp+fn),
                f1=div(2*tp,2*tp+fp+fn), specificity=div(tn,tn+fp),
                coverage_passed=valid>=50 and tp+fn>=10 and tn+fp>=10)


def confusion_matrix(rows):
    m = metrics(rows)
    # Rows = OSM in/out; columns = Baidu in/out.
    return [[m["tp"],m["fp"]],[m["fn"],m["tn"]]]


def execute(output):
    plan = json.loads((output/"plan.json").read_text(encoding="utf-8"))
    assert len(plan["cases"]) == 60 and file_sha(output/"geometry.json") == plan["geometry_sha256"]
    # Atomic one-time claim prohibits accidentally repeating billable requests.
    with (output/"live-started.json").open("x",encoding="utf-8") as stream:
        json.dump(dict(plan_sha256=plan_sha(plan), started_at=utc_now(), script_sha256=file_sha(__file__)),stream)
    settings = load_settings()
    if not settings.ak_configured:
        raise RuntimeError("baidu_map_ak_not_configured")
    run = asyncio.run(query_baidu(settings, plan["cases"], tuple(plan["origin_bd09ll"]), output))
    key = settings.baidu_map_ak.get_secret_value().encode()
    assert all(key not in p.read_bytes() for p in output.glob("*.json")), "credential_scan_failed"
    print(json.dumps(dict(state=run["state"], metrics=metrics(run["cases"])), ensure_ascii=True), flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--prepare", action="store_true")
    mode.add_argument("--execute-live", action="store_true")
    mode.add_argument("--render", action="store_true")
    parser.add_argument("--output",type=Path,default=DEFAULT_OUTPUT)
    parser.add_argument("--source",type=Path,default=FROZEN_SOURCE)
    args = parser.parse_args()
    if args.prepare:
        prepare(args.output.resolve(), args.source)
    elif args.execute_live:
        execute(args.output.resolve())
    else:
        from render_osm_baidu_report import render
        render(args.output.resolve())


if __name__ == "__main__":
    main()
