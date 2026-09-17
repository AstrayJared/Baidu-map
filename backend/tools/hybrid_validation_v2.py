"""Frozen boundary-first sampling and explicitly time-tolerant audit metrics."""
import math
import random
from collections import Counter

from shapely.geometry import Point, LineString, shape

from app.algorithms.hybrid_isochrone.extent import computation_extent, contains
from app.algorithms.hybrid_isochrone.models import HybridConfig
from app.geo.projection import MetricProjection
from app.geo.coordinates import wgs84_to_bd09
from app.hybrid_api import content_hash
from life_circle.coordinates import normalize

GROUPS = ('boundary', 'inferred_fill', 'interior', 'exterior')
ALLOCATIONS = dict(zip(GROUPS, (75, 15, 5, 5)))
SCHEMA = 'hybrid-validation-v2'


def freeze(result, diagnostics, ledger, seed):
    core, d = result['isochrone'], diagnostics['diagnostics']
    if result['result_hash'] != content_hash(core):
        raise ValueError('validation_result_hash_mismatch')
    config = HybridConfig(**core['config'])
    projection = MetricProjection(d['metric_crs'])
    origin = tuple(d['origin_xy'])
    g, inferred = shape(d['metric_geometry']), shape(d['metric_inferred_fill'])
    mask, support = shape(d['metric_hard_obstacles']), shape(d['metric_support'])
    if g.is_empty:
        raise ValueError('validation_empty_geometry')
    time_boundary = shape(d['metric_time_boundary'])
    unconfirmed = shape(d['metric_unconfirmed_boundary'])
    extent = computation_extent(origin, config)
    near = g.boundary.buffer(50)
    regions = dict(inferred_fill=inferred.difference(near),
                   interior=g.difference(inferred).difference(near),
                   exterior=extent.difference(g).difference(near).difference(mask))
    allocations = dict(ALLOCATIONS)
    unavailable = []
    for group, region in regions.items():
        if region.is_empty or region.area < 1e-6:
            allocations['boundary'] += allocations[group]
            allocations[group] = 0
            unavailable.append(group)
    rng = random.Random(seed)
    seen = {tuple(s['request_coordinate']) for s in ledger['samples']}
    rows = []

    def candidate(xy, group, kind, anchor=None):
        coordinate = normalize(wgs84_to_bd09(*projection.inverse.transform(*xy)))
        exact = projection.origin(coordinate)
        point = Point(exact)
        if coordinate in seen or not contains(origin, exact, config) or mask.covers(point):
            return None
        inside = g.covers(point)
        distance = g.boundary.distance(point)
        if group == 'boundary':
            if kind == 'contour':
                # Six-decimal BD09 quantization is checked, not assumed exact.
                if time_boundary.distance(point) > .2:
                    return None
            elif not (10 <= distance <= 50) or inside != (kind == 'inner'):
                return None
        elif not regions[group].covers(point):
            return None
        return dict(group=group, kind=kind, anchor_id=anchor, coordinate=coordinate, xy=exact,
                    polygon_inside=bool(inside), prediction=bool(inside),
                    evidence_supported=bool(support.covers(point)), boundary_distance_m=distance)

    def append(row):
        if row is None:
            return False
        seen.add(tuple(row['coordinate']))
        rows.append(dict(id=f'v{len(rows)+1:03d}', **row))
        return True

    rings = [LineString(poly.exterior.coords) for poly in (g.geoms if hasattr(g, 'geoms') else [g])]
    total = sum(r.length for r in rings)
    def location(fraction):
        distance = (fraction % 1) * total
        for ring in rings:
            if distance <= ring.length:
                return ring, distance
            distance -= ring.length
        return rings[-1], rings[-1].length / 2

    # Fifteen evenly spaced anchors, then ten geographically separated risk
    # anchors. Additional candidates are fixed before any reference response.
    phase = rng.random()
    regular = [(i + phase) / 15 for i in range(15)]
    pool = [(i + .5) / 500 for i in range(500)]
    def risk(f):
        ring, distance = location(f)
        p = ring.interpolate(distance)
        return (0 if unconfirmed.distance(p) < 1 else 1,
                0 if inferred.distance(p) < 50 else 1, f)
    selected = list(regular)
    def separation(fraction):
        return min(min(abs(fraction-f), 1-abs(fraction-f)) * total for f in selected)
    for _ in range(10):
        candidates = [f for f in pool if separation(f) >= 40]
        if not candidates:
            break
        # Disperse equal-risk points instead of taking ten adjacent positions.
        # With no risk geometry this becomes uniform gap-filling coverage.
        fraction = min(candidates, key=lambda f: (*risk(f)[:2], -separation(f), f))
        selected.append(fraction)
    selected += [(i + phase) / 1000 for i in range(1000)]
    anchors = []
    for fraction in selected:
        if sum(r['group'] == 'boundary' for r in rows) >= allocations['boundary']:
            break
        ring, distance = location(fraction)
        p = ring.interpolate(distance)
        if any(p.distance(q) < 10 for q in anchors):
            continue
        a, b = ring.interpolate((distance-1) % ring.length), ring.interpolate((distance+1) % ring.length)
        dx, dy = b.x-a.x, b.y-a.y
        length = math.hypot(dx, dy)
        if not length:
            continue
        anchor = f'a{len(anchors)+1:03d}'
        contour = candidate((p.x, p.y), 'boundary', 'contour', anchor)
        if contour is None:
            continue
        pair = None
        for offset in (20, 30, 40, 50, 10):
            points = [(p.x + sign * -dy / length * offset, p.y + sign * dx / length * offset) for sign in (1, -1)]
            inner_xy = next((xy for xy in points if g.contains(Point(xy))), None)
            outer_xy = next((xy for xy in points if not g.covers(Point(xy))), None)
            if inner_xy is None or outer_xy is None:
                continue
            inner = candidate(inner_xy, 'boundary', 'inner', anchor)
            outer = candidate(outer_xy, 'boundary', 'outer', anchor)
            if inner is not None and outer is not None:
                pair = inner, outer
                break
        if pair is None:
            continue
        anchors.append(p)
        for row in (contour, *pair):
            if sum(r['group'] == 'boundary' for r in rows) < allocations['boundary']:
                append(row)

    for group, region in regions.items():
        if not allocations[group]:
            continue
        xmin, ymin, xmax, ymax = region.bounds
        for _ in range(100000):
            if sum(r['group'] == group for r in rows) >= allocations[group]:
                break
            xy = rng.uniform(xmin, xmax), rng.uniform(ymin, ymax)
            if region.covers(Point(xy)):
                append(candidate(xy, group, 'random'))
    selected_counts = Counter(r['group'] for r in rows)
    return dict(schema_version=SCHEMA, algorithm_version=core['algorithm_version'],
                origin=list(ledger['origin']), seed=seed, config=core['config'],
                metric_crs=d['metric_crs'], result_hash=result['result_hash'],
                diagnostics_hash=content_hash(diagnostics), generation_ledger_hash=content_hash(ledger),
                generation_requests=ledger['requests_used'], validation_budget=100,
                allocations=allocations, unavailable_strata=unavailable,
                selection_shortfalls={g: allocations[g]-selected_counts[g] for g in GROUPS},
                boundary_summary=core['boundary_summary'], extent_truncated=core['extent_truncated'],
                generation_readiness=core['readiness'], generation_warnings=core['warnings'], cases=rows,
                acceptance=dict(tolerance_seconds=15, min_valid_fraction=.8, min_boundary_accuracy=.95,
                                min_contour_accuracy=.95, max_unconfirmed_fraction=.05),
                scope='fixed_origin_boundary_first_polygon_membership_and_contour_audit')


def classify(case, evidence):
    truth, prediction = evidence.get('reachable'), case.get('prediction')
    duration = evidence.get('duration')
    raw = ('api_unknown' if truth is None else 'algorithm_unknown' if prediction is None else
           'tp' if prediction and truth else 'fp' if prediction else 'fn' if truth else 'tn')
    if raw.endswith('unknown') or duration is None or not math.isfinite(duration):
        return raw if raw.endswith('unknown') else 'api_unknown', raw
    if 885 <= duration <= 915:
        return 'within_tolerance', raw
    if case.get('kind') == 'contour':
        return 'contour_miss', raw
    return ('fp' if prediction and duration > 915 else 'fn' if not prediction and duration < 885
            else 'tp' if prediction else 'tn'), raw


def metrics(plan, ledger):
    observations = {tuple(s['request_coordinate']): s['evidence'] for s in ledger.get('samples', [])}
    rows = []
    for case in plan['cases']:
        evidence = observations.get(tuple(case['coordinate']), {})
        label, strict = classify(case, evidence)
        rows.append(dict(**case, evidence=evidence, classification=label, strict_classification=strict))

    def summarize(selected, planned):
        counts = Counter(r['classification'] for r in selected)
        valid = sum(counts[k] for k in ('tp', 'tn', 'fp', 'fn', 'within_tolerance', 'contour_miss'))
        success = counts['tp'] + counts['tn'] + counts['within_tolerance']
        return dict(planned=planned, selected=len(selected), decidable=valid,
                    **{k: counts[k] for k in ('tp', 'tn', 'fp', 'fn', 'within_tolerance', 'contour_miss', 'api_unknown', 'algorithm_unknown')},
                    accuracy=success/valid if valid else None,
                    unknown_fraction=(planned-valid)/planned if planned else None)
    strata = {g: summarize([r for r in rows if r['group'] == g], plan['allocations'][g]) for g in GROUPS}
    overall = summarize(rows, sum(plan['allocations'].values()))
    contour_rows = [r for r in rows if r.get('kind') == 'contour']
    contour = summarize(contour_rows, math.ceil(plan['allocations']['boundary']/3))
    enough = (overall['decidable'] >= math.ceil(overall['planned']*.8)
              and all(s['decidable'] >= math.ceil(s['planned']*.8) for s in strata.values())
              and contour['decidable'] >= math.ceil(contour['planned']*.8))
    failures = []
    if not enough:
        failures.append('insufficient_valid_reference_points')
    if (strata['boundary']['accuracy'] or 0) < .95:
        failures.append('boundary_tolerance_accuracy_below_95pct')
    if (contour['accuracy'] or 0) < .95:
        failures.append('contour_duration_accuracy_below_95pct')
    if plan['boundary_summary']['unconfirmed_fraction'] > .05:
        failures.append('unconfirmed_boundary_above_5pct')
    if plan['extent_truncated']:
        failures.append('extent_truncated')
    return dict(schema_version=SCHEMA, outcome='insufficient_evidence' if not enough else 'failed' if failures else 'passed',
                failure_reasons=failures, overall=overall, strata=strata, contour=contour, cases=rows,
                strict_counts=dict(Counter(r['strict_classification'] for r in rows)),
                boundary_summary=plan['boundary_summary'], selection_shortfalls=plan['selection_shortfalls'],
                generation_requests=plan['generation_requests'], validation_requests=ledger.get('requests_used', 0),
                total_new_requests=plan['generation_requests']+ledger.get('requests_used', 0),
                validation_stop_reason=ledger.get('stop_reason'),
                confidence_scope='descriptive_stratified_clustered_samples_not_area_or_global_boundary_guarantee')
