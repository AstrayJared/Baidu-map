"""One fixed-origin API run (<=400) and a frozen held-out audit (<=100).

python -m tools.validate_hybrid --run-live --output .hybrid-ledgers/verification-v15-restored
python -m tools.validate_hybrid --summarize --output .hybrid-ledgers/verification-v15-restored
python -m tools.validate_hybrid --validate-frozen --output .hybrid-ledgers/verification-v15-restored
Live execution is a one-time claim. It never resumes or retries billable calls.
"""
import argparse
import asyncio
import json
import math
import random
import time
from collections import Counter
from pathlib import Path

from shapely.geometry import Point, shape

from app.algorithms.hybrid_isochrone.baidu_validator import StrictBaiduProvider
from app.algorithms.hybrid_isochrone.cache import EvidenceSession
from app.algorithms.hybrid_isochrone.extent import contains, ALGORITHM_VERSION
from app.algorithms.hybrid_isochrone.models import HybridConfig
from app.analyses import RateGate
from app.config import load_settings
from app.geo.projection import MetricProjection
from app.hybrid_api import content_hash
from app.hybrid_contracts import HybridResultResponse
from app.persistence import atomic_dump
from app.test_origin import TEST_ORIGIN

SEED = 20260916
GROUPS = ("boundary", "inferred_fill", "interior", "exterior")


def read(path):
    return json.loads(path.read_text(encoding="utf-8"))


def claim(path, payload):
    # The marker is never removed, including on startup/transport failure.
    with path.open("x", encoding="utf-8") as stream:
        json.dump(payload, stream, ensure_ascii=False, allow_nan=False)


def freeze(result, diagnostics, ledger, *, seed=SEED):
    """Select reference coordinates without seeing reference Baidu labels."""
    HybridResultResponse.model_validate(result)
    core = result["isochrone"]
    if core["algorithm_version"] != ALGORITHM_VERSION:
        raise ValueError("validation_algorithm_version_mismatch")
    if result["result_hash"] != content_hash(core):
        raise ValueError("validation_result_hash_mismatch")
    cfg = HybridConfig(**core["config"])
    d = diagnostics["diagnostics"]
    projection = MetricProjection(d["metric_crs"])
    g = shape(d["metric_geometry"])
    if g.is_empty:
        raise ValueError("validation_empty_geometry")
    origin_xy = tuple(d["origin_xy"])
    from app.algorithms.hybrid_isochrone.extent import computation_extent
    extent = computation_extent(origin_xy, cfg)
    mask = shape(d["metric_hard_obstacles"])
    land = extent.difference(mask)
    inferred = shape(d["metric_inferred_fill"])
    support = shape(d["metric_support"])
    boundary = g.boundary.buffer(50).intersection(land)
    regions = dict(boundary=boundary, inferred_fill=inferred.difference(boundary),
                   interior=g.difference(inferred).difference(boundary),
                   exterior=land.difference(g).difference(boundary))
    # Fill near the boundary is still a relevant independent fill stratum.
    if regions["inferred_fill"].is_empty and not inferred.is_empty:
        regions["inferred_fill"] = inferred
    # Audit scheduling is independent of circle generation. Spend only a few
    # checks on the interior/exterior; scale fill checks with its area.
    regions['exterior'] = regions['exterior'].intersection(support)
    fill_fraction = regions['inferred_fill'].area / max(g.area, 1)
    fill_count = min(10, max(3, math.ceil(20 * fill_fraction))) if regions['inferred_fill'].area > 1e-6 else 0
    allocations = dict(boundary=100-fill_count-5-2, inferred_fill=fill_count, interior=5, exterior=2)
    risk_region = boundary.intersection(inferred.buffer(25).union(boundary.intersection(support.boundary.buffer(25))))
    unavailable = []
    for group in GROUPS[1:]:
        if regions[group].is_empty or regions[group].area < 1e-6:
            allocations["boundary"] += allocations[group]
            allocations[group] = 0
            unavailable.append(group)
    rng = random.Random(seed)
    seen = {tuple(s["request_coordinate"]) for s in ledger["samples"]}
    # Reuse normalization exactly as the live request session does, without IO.
    from app.geo.coordinates import wgs84_to_bd09
    from life_circle.coordinates import normalize
    rows = []
    for group in GROUPS:
        region = regions[group]
        target = allocations[group]
        if not target:
            continue
        if region.is_empty:
            raise ValueError("validation_no_boundary_candidates")
        xmin, ymin, xmax, ymax = region.bounds
        selected = 0
        for _ in range(200000):
            focus = risk_region if group == 'boundary' and selected < min(20, target // 3) and risk_region.area > 1e-6 else region
            fxmin, fymin, fxmax, fymax = focus.bounds
            xy = rng.uniform(fxmin, fxmax), rng.uniform(fymin, fymax)
            if not focus.covers(Point(xy)):
                continue
            if not region.covers(Point(xy)):
                continue
            coordinate = normalize(wgs84_to_bd09(*projection.inverse.transform(*xy)))
            exact = projection.origin(coordinate)
            point = Point(exact)
            if coordinate in seen or not contains(origin_xy, exact, cfg) or not region.covers(point):
                continue
            seen.add(coordinate)
            inside = bool(g.covers(point))
            prediction = True if inside else False if support.covers(point) else None
            rows.append(dict(id=f"v{len(rows)+1:03d}", group=group, coordinate=coordinate, xy=exact,
                             polygon_inside=inside, prediction=prediction,
                             boundary_distance_m=g.boundary.distance(point)))
            selected += 1
            if selected == target:
                break
        if selected != target:
            raise ValueError("validation_candidate_selection_insufficient")
    assert len(rows) == 100 and len({tuple(r['coordinate']) for r in rows}) == 100
    return dict(schema_version="hybrid-validation-v3", algorithm_version=ALGORITHM_VERSION,
                origin=list(ledger["origin"]), seed=seed, result_hash=result["result_hash"],
                diagnostics_hash=content_hash(diagnostics), generation_ledger_hash=content_hash(ledger),
                generation_requests=ledger["requests_used"], validation_budget=100,
                config=core["config"], metric_crs=d["metric_crs"], allocations=allocations,
                generation_readiness=core["readiness"], generation_warnings=core["warnings"],
                unavailable_strata=unavailable, cases=rows,
                scope="fixed_origin_stratified_held_out_classification_not_area_accuracy",
                allocation_policy='geometry_adaptive_boundary_first_far_exterior_at_most_2',
                acceptance=dict(tolerance_seconds=15, min_boundary_accuracy=.95, min_decidable=80, min_stratum_fraction=.8,
                                min_each_baidu_class=20, min_accuracy=.9))


def wilson(success, count):
    if not count:
        return None
    z = 1.959963984540054
    p = success / count
    denominator = 1 + z*z/count
    middle = (p + z*z/(2*count)) / denominator
    half = z * math.sqrt(p*(1-p)/count + z*z/(4*count*count)) / denominator
    return [max(0, middle-half), min(1, middle+half)]


def metrics(plan, ledger):
    if plan.get('schema_version') == 'hybrid-validation-v2':
        from tools.hybrid_validation_v2 import metrics as historical_v16
        return historical_v16(plan, ledger)
    strict = metrics_legacy(plan, ledger)
    if plan.get('schema_version') == 'hybrid-validation-v3':
        from tools.hybrid_validation_v3 import tolerant_metrics
        return tolerant_metrics(plan, strict)
    return strict


def metrics_legacy(plan, ledger):
    observations = {tuple(s["request_coordinate"]): s["evidence"] for s in ledger.get("samples", [])}
    rows = []
    for case in plan["cases"]:
        evidence = observations.get(tuple(case["coordinate"]), {})
        truth = evidence.get("reachable")
        prediction = case["prediction"]
        label = ("api_unknown" if truth is None else "algorithm_unknown" if prediction is None else
                 "tp" if prediction and truth else "fp" if prediction else "fn" if truth else "tn")
        rows.append({**case, "evidence": evidence, "classification": label})

    def summarize(selected):
        counts = Counter(r["classification"] for r in selected)
        tp, tn, fp, fn = (counts[k] for k in ("tp", "tn", "fp", "fn"))
        n = tp+tn+fp+fn
        divide = lambda a, b: a/b if b else None
        return dict(planned=len(selected), decidable=n, tp=tp, tn=tn, fp=fp, fn=fn,
                    api_unknown=counts["api_unknown"], algorithm_unknown=counts["algorithm_unknown"],
                    accuracy=divide(tp+tn, n), accuracy_wilson_95=wilson(tp+tn, n),
                    false_inclusion_rate=divide(fp, tp+fp), false_exclusion_rate=divide(fn, tp+fn),
                    false_inclusion_wilson_95=wilson(fp, tp+fp), false_exclusion_wilson_95=wilson(fn, tp+fn),
                    unknown_fraction=divide(len(selected)-n, len(selected)))

    total = summarize(rows)
    strata = {g: summarize([r for r in rows if r["group"] == g]) for g in GROUPS}
    enough = (total["decidable"] >= 80 and total["tp"]+total["fn"] >= 20
              and total["tn"]+total["fp"] >= 20
              and all(s["decidable"] >= math.ceil(s["planned"]*.8) for s in strata.values()))
    outcome = "insufficient_evidence" if not enough else "passed" if total["accuracy"] >= .9 else "failed"
    return dict(outcome=outcome, overall=total, strata=strata, cases=rows,
                generation_requests=plan["generation_requests"], validation_requests=ledger.get("requests_used", 0),
                total_new_requests=plan["generation_requests"]+ledger.get("requests_used", 0),
                validation_stop_reason=ledger.get("stop_reason"),
                confidence_scope="conditional_on_fixed_stratified_valid_decidable_samples_not_area_or_global_boundary")


async def validate_live(plan, output, settings, gate):
    config = HybridConfig(**{**plan["config"], "max_baidu_requests": 100, "request_qps": 3})
    projection = MetricProjection(plan["metric_crs"])
    async with StrictBaiduProvider(settings.baidu_map_ak.get_secret_value(), projection, config) as provider:
        session = EvidenceSession(tuple(plan["origin"]), projection, config, provider, gate,
                                  path=output / "validation-ledger.json")
        for row in plan["cases"]:
            if not session.available:
                break
            await session.query(tuple(row["xy"]), "INDEPENDENT_VALIDATION", row["group"],
                                request_coordinate=tuple(row["coordinate"]))
            if session.requests_used % 10 == 0:
                print(json.dumps(dict(stage="validation", requests=session.requests_used)), flush=True)
        session.flush()


def reference_ready(result, diagnostics):
    """Data availability is distinct from a conservative quality warning.

    A known unresolved water line outside the final shell does not prevent an
    independent audit of that frozen partial geometry. Keep its warning intact.
    """
    readiness = result["isochrone"]["readiness"]
    flags = ("graph_available", "data_version_matches", "coverage_available", "origin_in_coverage",
             "extent_in_coverage", "obstacle_layer_available", "risk_layer_available")
    return (all(readiness.get(k) is True for k in flags)
            and diagnostics["diagnostics"]["unresolved_water_lines_affecting_shell"] == 0)


def validate_frozen(output):
    """First validation only, after a completed generation; no generation calls."""
    settings = load_settings()
    if not settings.ak_configured or settings.analysis_qps != 3:
        raise ValueError("validation_requires_configured_baidu_and_qps_3")
    started = read(output / "live-started.json")
    if started["generation_budget"] != 400 or started["validation_budget"] != 100:
        raise ValueError("validation_original_budget_mismatch")
    if (output / "validation-ledger.json").exists() or (output / "validation-started.json").exists():
        raise ValueError("validation_already_started_no_retry")
    task_id = read(output / "generation-task.json")["task_id"]
    from uuid import UUID
    if str(UUID(task_id)) != task_id:
        raise ValueError("validation_task_id_invalid")
    task = output / "tasks" / task_id
    result = read(output / "frozen-result.json")
    ledger, diagnostics = read(task / "ledger.json"), read(task / "diagnostics.json")
    from life_circle.coordinates import normalize
    if (normalize(tuple(ledger["origin"])) != normalize(TEST_ORIGIN) or ledger["requests_used"] > 400
            or read(output / "generation-status.json")["status"] != "completed"
            or result != read(task / "result.json") or result["taskId"] != task_id):
        raise ValueError("validation_frozen_generation_mismatch")
    if not reference_ready(result, diagnostics) or result["isochrone"]["geometry"] is None:
        raise ValueError("validation_frozen_generation_not_ready")
    if ledger.get("stop_reason") not in (None, "budget_exhausted", "deadline"):
        raise ValueError("validation_generation_stopped_by_upstream_error")
    plan = freeze(result, diagnostics, ledger)
    atomic_dump(output / "validation-plan.json", plan)
    claim(output / "validation-started.json", dict(plan_hash=content_hash(plan), started_at=time.time(),
          phase="first_validation_of_frozen_generation", generation_unchanged=True))
    asyncio.run(validate_live(plan, output, settings, RateGate(3)))
    summarize(output)


def run_live(output):
    from fastapi.testclient import TestClient
    from app.main import create_app
    settings = load_settings()
    if not settings.ak_configured:
        raise ValueError("baidu_walking_not_configured")
    if settings.analysis_qps != 3:
        raise ValueError("validation_requires_shared_qps_3")
    output.mkdir(parents=True, exist_ok=True)
    claim(output / "live-started.json", dict(origin=TEST_ORIGIN, algorithm_version=ALGORITHM_VERSION,
          generation_budget=400, validation_budget=100, retries=0, qps=3, started_at=time.time()))
    settings.hybrid_ledger_dir = output / "tasks"
    app = create_app(settings)
    started = time.perf_counter()
    with TestClient(app) as client:
        cold_seconds = time.perf_counter()-started
        offline = app.state.osm_offline
        if offline.store is None or offline.coverage is None:
            atomic_dump(output / "blocked.json", dict(reason="osm_startup_not_ready", requests=0))
            return
        config = HybridConfig()
        origin_xy = offline.store.projection.origin(TEST_ORIGIN)
        from app.algorithms.hybrid_isochrone.extent import computation_extent
        if not offline.coverage.covers(computation_extent(origin_xy, config)):
            atomic_dump(output / "blocked.json", dict(reason="extent_outside_osm_coverage", requests=0))
            return
        body = dict(origin=dict(zip(("lng", "lat"), TEST_ORIGIN)), coordinate_system="bd09ll",
                    client_request_id="verification-v15-restored-fixed-origin", config=config.model_dump(mode="json"))
        response = client.post("/api/v1/analysis/hybrid", json=body)
        response.raise_for_status()
        task_id = response.json()["taskId"]
        atomic_dump(output / "generation-task.json", dict(task_id=task_id, cold_load_seconds=cold_seconds))
        print(json.dumps(dict(stage="api_started", task_id=task_id, cold_load_seconds=cold_seconds)), flush=True)
        while True:
            response = client.get(f"/api/v1/analysis/hybrid/{task_id}")
            response.raise_for_status()
            status = response.json()
            print(json.dumps(dict(stage=status["stage"], requests=status["requests"], elapsed=status["elapsedSeconds"])), flush=True)
            if status["status"] in ("completed", "failed", "cancelled"):
                break
            if time.perf_counter()-started > 2100:
                client.post(f"/api/v1/analysis/hybrid/{task_id}/cancel")
                raise ValueError("validation_api_deadline")
            time.sleep(10)
        atomic_dump(output / "generation-status.json", status)
        if status["status"] != "completed":
            return
        response = client.get(f"/api/v1/analysis/hybrid/{task_id}/result")
        response.raise_for_status()
        result = response.json()
        atomic_dump(output / "frozen-result.json", result)
        task_path = settings.hybrid_ledger_dir / task_id
        diagnostics, ledger = read(task_path / "diagnostics.json"), read(task_path / "ledger.json")
        if ledger["requests_used"] > 400:
            raise ValueError("generation_budget_exceeded")
        if not reference_ready(result, diagnostics):
            atomic_dump(output / "blocked.json", dict(reason="hybrid_degraded_no_precision_claim",
                        requests=ledger["requests_used"]))
            return
        if result["isochrone"]["geometry"] is None or ledger.get("stop_reason") in (
                "permission", "quota", "rate_limit", "consecutive_api_failures", "invalid_origin_endpoint_offset"):
            atomic_dump(output / "blocked.json", dict(reason=ledger.get("stop_reason") or "empty_geometry",
                        requests=ledger["requests_used"]))
            return
        plan = freeze(result, diagnostics, ledger)
        atomic_dump(output / "validation-plan.json", plan)
        claim(output / "validation-started.json", dict(plan_hash=content_hash(plan), started_at=time.time()))
        # Use the same event-loop-owned shared gate, not an independent limiter.
        client.portal.call(validate_live, plan, output, settings, app.state.analyses.gate)
    summarize(output)


def summarize(output):
    plan = read(output / "validation-plan.json")
    started = read(output / "validation-started.json")
    if content_hash(plan) != started["plan_hash"]:
        raise ValueError("frozen_validation_plan_changed")
    result = read(output / "frozen-result.json")
    if content_hash(result["isochrone"]) != plan["result_hash"]:
        raise ValueError("frozen_result_changed")
    task_id = read(output / "generation-task.json")["task_id"]
    task = output / "tasks" / task_id
    for filename, key in (("ledger.json", "generation_ledger_hash"), ("diagnostics.json", "diagnostics_hash")):
        if content_hash(read(task / filename)) != plan[key]:
            raise ValueError("generation_evidence_changed")
    ledger = read(output / "validation-ledger.json")
    if ledger["requests_used"] > 100 or ledger["requests_used"] + plan["generation_requests"] > 500:
        raise ValueError("validation_budget_exceeded")
    measured = metrics(plan, ledger)
    atomic_dump(output / "metrics.json", measured)
    print(json.dumps({k: v for k, v in measured.items() if k not in ("cases", "strata")}), flush=True)
    return measured


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    modes = parser.add_mutually_exclusive_group(required=True)
    modes.add_argument("--run-live", action="store_true")
    modes.add_argument("--summarize", action="store_true")
    modes.add_argument("--validate-frozen", action="store_true")
    parser.add_argument("--output", type=Path, default=Path(".hybrid-ledgers/verification-v15-restored"))
    args = parser.parse_args()
    try:
        (run_live if args.run_live else validate_frozen if args.validate_frozen else summarize)(args.output.resolve())
    except Exception as exc:
        # No provider exception text, request URL or credentials in terminal/logs.
        print(json.dumps(dict(error="hybrid_validation_failed", exception_type=type(exc).__name__)), flush=True)
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()
