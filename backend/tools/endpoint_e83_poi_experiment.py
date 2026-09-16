"""Frozen P1/D0/B1 representative screen; no real network, credentials or retrieval."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import random
import zipfile

from shapely.geometry import Point, Polygon, mapping, shape

from life_circle.coordinates import normalize
from tools.diagnostic_common import no_network
from tools.endpoint_e83_experiment import StressProvider, metrics as circle_metrics
from tools.endpoint_e83_poi import run_task, same_endpoint
from tools.endpoint_multicross_experiment import RotatedSynthetic, truth_for
from tools.endpoint_radial_experiment import ORIGIN, P, local, radius
from tools.live_smoke import dump

CASES = ('circle', 'ellipse', 'concave', 'reentry_rotated33', 'narrow', 'concave_snap80')
BUDGETS = (150, 200, 250, 300)
LOADS = ((20, 5), (50, 10), (200, 20))
WORKLOADS = ('single', 'categories', 'clicks')


def scene(case):
    if case == 'concave_snap80':
        truth = truth_for('concave')
        return truth, lambda: StressProvider(truth, 'snap80')
    return truth_for(case), lambda: RotatedSynthetic(case)


def reference_seconds(case, xy, truth):
    if case.startswith('reentry_') or case == 'concave_snap80':
        return 400 if truth.covers(Point(xy)) else 1900
    return 900 * math.hypot(*xy) / radius(case, math.atan2(xy[1], xy[0]))


def catalog(case, n, k, truth):
    # Fixed before any algorithm output. No deliberate coincidence with circle query seeds.
    rng = random.Random(20260916)
    parts = [truth] if truth.geom_type == 'Polygon' else list(truth.geoms)
    outer = max(parts, key=lambda g: g.area).exterior
    tip = max((xy for part in parts for xy in part.exterior.coords), key=lambda xy: math.hypot(*xy))
    pois, labels, used = [], {}, set()
    while len(pois) < n:
        i = len(pois); angle = rng.uniform(0, 2*math.pi)
        layer = i % 4
        if layer in (0, 1):
            r = rng.uniform(150, 450) if layer == 0 else rng.uniform(1050, 1150)
            xy = (r*math.cos(angle), r*math.sin(angle))
        elif layer == 2:
            b = outer.interpolate(rng.random(), normalized=True)
            r = math.hypot(b.x, b.y)
            factor = (r + rng.choice((-20, 20))) / r
            xy = (b.x*factor, b.y*factor)
        else:
            xy = (tip[0]+rng.uniform(-55, 35), tip[1]+rng.uniform(-45, 45))
        coord = normalize(P.to_geographic(xy))
        if coord in used:
            continue
        used.add(coord)
        identity = f'p{i:03d}'
        pois.append(dict(id=identity, coordinate=list(coord), critical=i < k))
        seconds = reference_seconds(case, P.to_local(coord), truth)
        labels[identity] = dict(reachable=seconds <= 900, seconds=seconds,
            construction_stratum=('interior', 'exterior', 'boundary_pair', 'tip')[layer])
    return pois, labels


def demand_for(pois, workload):
    keys = [p['id'] for p in pois if p['critical']]
    rest = [p['id'] for p in pois if not p['critical']]
    if workload == 'single':
        return keys + rest
    if workload == 'categories':
        # Three categories overlap. All arms may reuse their own POI observations fairly.
        return keys + [p['id'] for category in range(3) for i, p in enumerate(pois)
                       if i % 3 in (category, (category+1) % 3)]
    return keys + list(reversed(keys)) + keys[::2] + rest + keys


def group_scores(ids, state, labels):
    positive = sum(labels[i]['reachable'] for i in ids)
    negative = len(ids)-positive
    false_accept = sum(state[i]['status'] == 'verified_reachable' and not labels[i]['reachable'] for i in ids)
    missed = sum(state[i]['status'] != 'verified_reachable' and labels[i]['reachable'] for i in ids)
    false_reject = sum(state[i]['status'] == 'verified_unreachable' and labels[i]['reachable'] for i in ids)
    pending = sum(state[i]['status'] == 'pending' for i in ids)
    wrong_minutes = sum(state[i]['seconds'] is not None and
                        abs(state[i]['seconds']-labels[i]['seconds']) > 1e-6 for i in ids)
    return dict(n=len(ids), positives=positive, negatives=negative, false_accept=false_accept,
                missed_reachable=missed, explicit_false_reject=false_reject, pending=pending,
                wrong_minutes=wrong_minutes, false_accept_rate=false_accept/negative if negative else None,
                missed_rate=missed/positive if positive else None,
                pending_rate=pending/len(ids) if ids else None)


def evaluate(task, pois, labels, truth, baseline_risk):
    circle = task['circle']
    valid_layers, covered, layer_errors = 0, 0, []
    if circle:
        for name in ('geometry', 'candidateGeometry', 'unknownRegion'):
            if circle.get(name) is not None:
                for g in (shape(circle[name]), local(circle[name])):
                    valid_layers += 1
                    if not g.is_valid:
                        layer_errors.append(name)
    if circle and circle.get('geometry'):
        measured = circle_metrics(circle, truth)
        pred = shape(circle['geometry'])
        covered = sum(pred.covers(Point(e['observation']['route_destination'])) for e in task['ledger']
            if e['observation']['observed_duration'] is not None and e['observation']['observed_duration'] > 900
            and same_endpoint(e['observation']['route_origin'], ORIGIN)
            and e['observation']['route_destination'] is not None)
    else:
        measured = dict(valid=False, failed=True, iou=None, truth_to_outer=None, outer_to_truth=None)
    groups = {'all': [p['id'] for p in pois],
              'critical': [p['id'] for p in pois if p['critical']],
              'ordinary': [p['id'] for p in pois if not p['critical']]}
    for layer in ('interior', 'exterior', 'boundary_pair', 'tip'):
        groups['catalog_'+layer] = [p['id'] for p in pois if labels[p['id']]['construction_stratum'] == layer]
    groups.update({name: ids for name, ids in baseline_risk.items()})
    scores = {name: group_scores(ids, task['poi'], labels) for name, ids in groups.items()}
    key = scores['critical']
    return dict(cost=task['cost'], sharing=task['sharing'], facility=scores,
                critical_complete=(key['n']-key['pending'])/key['n'],
                circle=measured, circle_error=task['circle_error'], layer_errors=layer_errors,
                geometry_checks=valid_layers, all_known_negative_covered=covered,
                first_coarse=task['first_coarse'], critical_ready=task['critical_ready'],
                elapsed_s=task['elapsed_s'])


def risk_groups(circle, pois):
    pred = local(circle['geometry']) if circle and circle.get('geometry') else Polygon()
    unknown = local(circle['unknownRegion']) if circle and circle.get('unknownRegion') else Polygon()
    groups = {name: [] for name in ('baseline_inside', 'baseline_outside', 'baseline_boundary', 'baseline_unknown')}
    for poi in pois:
        point = Point(P.to_local(poi['coordinate']))
        groups['baseline_inside' if pred.covers(point) else 'baseline_outside'].append(poi['id'])
        if not pred.is_empty and pred.boundary.distance(point) <= 25:
            groups['baseline_boundary'].append(poi['id'])
        if pred.is_empty or unknown.covers(point):
            groups['baseline_unknown'].append(poi['id'])
    return groups


async def run(output, selected_cases, selected_loads, selected_workloads):
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[2]
    paths = sorted([*repo.joinpath('backend/tools').glob('*.py'),
                    *repo.joinpath('backend/tests').glob('test_endpoint_e83*.py'),
                    *repo.joinpath('life-circle-algorithm/src').rglob('*.py')])
    hashes = {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    arms = [('independent', 300)] + [('independent', b) for b in BUDGETS if b != 300]
    arms += [('shared', b) for b in BUDGETS] + [('direct', 0)]
    fixtures = {}
    for case in selected_cases:
        truth, _ = scene(case)
        for n, k in LOADS:
            if n in selected_loads:
                pois, labels = catalog(case, n, k, truth)
                fixtures[f'{case}-{n}'] = dict(pois=pois, reference=labels, truth_local=mapping(truth))
    dump(output/'fixtures.json', fixtures)
    dump(output/'protocol.json', dict(cases=selected_cases, budgets=BUDGETS, arms=arms,
        loads=[l for l in LOADS if l[0] in selected_loads], workloads=selected_workloads,
        validation_caps='V=N and V=min(N,50), deduplicated when identical',
        fixture_sha256=hashlib.sha256((output/'fixtures.json').read_bytes()).hexdigest(),
        source_sha256=hashes, live_calls=0, circle_target_m=25, provider_attempts_max=2,
        packages={p: importlib.metadata.version(p) for p in ('numpy', 'shapely', 'httpx')},
        scope='Representative offline P1/D0/B1 screen; G0 for all circles; frozen critical-first demand, no adaptive POI ranking; exact coordinate reuse only; no UID workload; cold per run',
        accounting='Physical provider attempts; circle and POI budgets separate; no retrieval API or reference queries; synthetic evaluation time is not live service latency'))
    with zipfile.ZipFile(output/'source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for p in paths:
            archive.write(p, p.relative_to(repo))
    rows = []
    for case in selected_cases:
        truth, factory = scene(case)
        for n, k in LOADS:
            if n not in selected_loads:
                continue
            frozen = fixtures[f'{case}-{n}']; pois, labels = frozen['pois'], frozen['reference']
            for cap in sorted({n, min(n, 50)}):
                for workload in selected_workloads:
                    demand = demand_for(pois, workload)
                    risk = {}
                    for mode, budget in arms:
                        identity = f'{case}-n{n}-v{cap}-{workload}-{mode}-{budget}'
                        task = await run_task(case, factory(), pois, demand, circle_budget=budget,
                                              poi_budget=cap, shared=mode == 'shared', run_id=identity)
                        if mode == 'independent' and budget == 300:
                            risk = risk_groups(task['circle'], pois)
                            dump(output/f'{case}-n{n}-v{cap}-{workload}-risk.json', risk)
                        measured = evaluate(task, pois, labels, truth, risk)
                        row = dict(id=identity, case=case, n=n, k=k, v=cap, workload=workload,
                                   mode=mode, budget=budget, **measured)
                        # Serialize the full evidence ledger, but omit duplicate private observations.
                        if task['circle']:
                            task['circle'] = {key: value for key, value in task['circle'].items() if not key.startswith('_')}
                        dump(output/f'{identity}.json', task)
                        rows.append(row)
                        dump(output/'metrics.json', rows)
                        print(json.dumps(dict(id=identity, calls=task['cost']['total_calls'],
                            critical_complete=measured['critical_complete'],
                            circle_failed=measured['circle']['failed'] if budget else None)), flush=True)
    unchanged = all(hashlib.sha256((repo/p).read_bytes()).hexdigest() == h for p, h in hashes.items())
    fixture_unchanged = read_fixture_hash(output) == json.loads((output/'protocol.json').read_text())['fixture_sha256']
    dump(output/'audit.json', dict(runs=len(rows), synthetic_calls=sum(r['cost']['total_calls'] for r in rows),
        live_calls=0, source_hashes_unchanged=unchanged, fixture_unchanged=fixture_unchanged,
        geometry_failed=sum(bool(r['circle']['failed'] or r['layer_errors']) for r in rows if r['budget']),
        shared_negative_conflict_runs=sum(r['all_known_negative_covered'] > 0 for r in rows if r['mode'] == 'shared'),
        wrong_minutes=sum(r['facility']['all']['wrong_minutes'] for r in rows)))
    assert unchanged and fixture_unchanged


def read_fixture_hash(output):
    return hashlib.sha256((output/'fixtures.json').read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cases', choices=CASES, nargs='+', default=list(CASES))
    parser.add_argument('--loads', choices=[20, 50, 200], type=int, nargs='+', default=[20, 50, 200])
    parser.add_argument('--workloads', choices=WORKLOADS, nargs='+', default=list(WORKLOADS))
    args = parser.parse_args()
    async def guarded():
        with no_network():
            await run(args.output, args.cases, args.loads, args.workloads)
    asyncio.run(guarded())


if __name__ == '__main__':
    main()
