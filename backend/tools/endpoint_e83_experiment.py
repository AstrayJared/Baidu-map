"""Frozen E8.3 offline mechanism screening, including failed outputs in denominators."""
import argparse
import asyncio
from collections import Counter
import hashlib
import importlib.metadata
import json
import math
from pathlib import Path
import time
import zipfile

import numpy as np
from shapely.affinity import rotate
from shapely.geometry import LineString, Point, Polygon, mapping, shape
from shapely.ops import unary_union

from life_circle.field import GeometryError
from life_circle.models import CancelToken, IsochroneRequest, RouteObservation
from tools.diagnostic_common import no_network
from tools.endpoint_multicross_boundary import compute_multicross_boundary
from tools.endpoint_geometry import OVERLAY_GRID_M, ROUNDTRIP_TOLERANCE_M
from tools.endpoint_multicross_experiment import CASES82, RotatedSynthetic, truth_for
from tools.endpoint_radial_experiment import ORIGIN, P, local
from tools.live_smoke import dump

METHODS = {'g0': {}, 's1': {'diverse_batches': True},
           's2': {'coverage_first': True}, 's1s2': {'diverse_batches': True, 'coverage_first': True},
           'stagnation': {'edge_queue_policy': 'stagnation'},
           'diversity': {'edge_queue_policy': 'diversity'}}


def distances(rings, target):
    values = []
    for ring in rings:
        n = max(1, math.ceil(ring.length / 5))
        values.extend(ring.interpolate((i+.5)/n, normalized=True).distance(target) for i in range(n))
    return dict(p95_m=float(np.quantile(values, .95)), max_m=max(values)) if values else None


def polygons(g):
    return [g] if g.geom_type == 'Polygon' else list(g.geoms) if g.geom_type == 'MultiPolygon' else []


def metrics(result, truth):
    geometry = result.get('geometry')
    pred = local(geometry) if geometry is not None else Polygon()
    # Geography and inverse projection are checked separately: neither is repaired silently.
    if not pred.is_valid or (geometry is not None and not shape(geometry).is_valid):
        return dict(valid=False, failed=True, failure='invalid_output_geometry')
    pp, tp = polygons(pred), polygons(truth)
    outer = unary_union([p.exterior for p in pp])
    truth_outer = unary_union([p.exterior for p in tp])
    unknown = local(result['unknownRegion']) if result.get('unknownRegion') else None
    records = result['observationEvidence']
    negative_covered = sum(shape(geometry).covers(Point(e['route_destination'])) for e in records
        if geometry and e['accepted'] and e['observed_duration'] > 900)
    seen, duplicates = set(), Counter()
    for e in records:
        if not e['accepted']:
            continue
        key = (tuple(e['route_origin']), tuple(e['route_destination']))
        if key in seen:
            duplicates[e['kind']] += 1
        seen.add(key)
    return dict(valid=True, failed=negative_covered > 0, empty=pred.is_empty,
        iou=pred.intersection(truth).area / pred.union(truth).area,
        false_positive_m2=pred.difference(truth).area, false_negative_m2=truth.difference(pred).area,
        truth_to_outer=None if pred.is_empty else distances([p.exterior for p in tp], outer),
        outer_to_truth=None if pred.is_empty else distances([p.exterior for p in pp], truth_outer),
        holes_to_truth=None if pred.is_empty else distances([r for p in pp for r in p.interiors], truth.boundary),
        components=len(pp), holes=sum(len(p.interiors) for p in pp),
        local_unknown_m2=unknown.area if unknown is not None else None,
        negative_covered=negative_covered, repeated_actual=sum(duplicates.values()),
        repeated_by_phase=dict(duplicates), initial_unfinished=sum(d['status'] != 'localized' for d in result['directions']),
        directions_without_bracket=sum(not d.get('bracket') for d in result['directions']),
        queue=result.get('localRepair', {}).get('queuePolicy'))


class StressProvider:
    network = False
    def __init__(self, geometry, mode):
        self.geometry, self.mode, self.calls = geometry, mode, 0
        self.identity = ('e83-stress', mode)

    async def query_walking_time(self, origin, destination, deadline):
        self.calls += 1
        x, y = P.to_local(destination)
        if self.mode == 'offset60':
            x += 60
        if self.mode == 'snap80':
            x, y = round(x/80)*80, round(y/80)*80
        duration = 400 if self.geometry.covers(Point(x, y)) else 1900
        offset = math.dist((x, y), P.to_local(destination))
        return RouteObservation(destination, duration, observed_duration=duration,
            route_origin=origin, route_destination=P.to_geographic((x, y)), request_origin=origin,
            reason='endpoint_offset' if offset > 50 else None, origin_offset_m=0, destination_offset_m=offset)


def cases(stress):
    if stress == 'none':
        return [(case, truth_for(case), lambda c=case: RotatedSynthetic(c)) for case in CASES82]
    angles = (0, 11, 33) if stress == 'development' else (5.5, 16.875, 27)
    rows = []
    for family in ('reentry', 'narrow'):
        for width in (25, 50, 100):
            disk = Point(0, 0).buffer(650, quad_segs=180)
            if family == 'reentry':
                geometry = unary_union([disk, Point(850, 250).buffer(200, quad_segs=90),
                    LineString([(450, 400), (850, 450)]).buffer(width/2)])
            else:
                geometry = unary_union([disk, LineString([(600, 0), (1000, 0)]).buffer(width/2)])
            for angle in angles:
                truth = rotate(geometry, angle, origin=(0, 0))
                for mode in ('none', 'offset60', 'snap80'):
                    name = f'{family}-w{width}-a{angle}-{mode}'
                    rows.append((name, truth, lambda t=truth, m=mode: StressProvider(t, m)))
    return rows


async def run(args):
    args.output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[2]
    paths = sorted([*repo.joinpath('backend/tools').glob('*.py'),
                    *repo.joinpath('life-circle-algorithm/src').rglob('*.py')])
    hashes = {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    specs = cases(args.stress)
    dump(args.output/'protocol.json', dict(budgets=args.budgets, methods=args.methods,
        method_options={m: METHODS[m] for m in args.methods}, overlay_grid_m=OVERLAY_GRID_M,
        export_tolerance_m=ROUNDTRIP_TOLERANCE_M,
        cases=[c for c, _, _ in specs], stress_split=args.stress, radial_step=100, target_m=25,
        edge_batch_size=30, concurrency=30, seed=20260911, live_calls=0,
        source_sha256=hashes, packages={p: importlib.metadata.version(p) for p in ('numpy', 'shapely', 'httpx')},
        scope='S1 local edge queue prototype; S2 coverage order prototype; no soft phase quotas or POI integration'))
    with zipfile.ZipFile(args.output/'source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for path in paths:
            archive.write(path, path.relative_to(repo))
    rows = []
    for case, truth, factory in specs:
        dump(args.output/f'{case}-truth.json', mapping(truth))
        for budget in args.budgets:
            for method in args.methods:
                provider = factory()
                request = IsochroneRequest(ORIGIN, 'bd09ll', budget=budget, extent=1200,
                    max_extent=1200, expand=False, concurrency=30)
                started = time.perf_counter()
                try:
                    result = await compute_multicross_boundary(request, provider, CancelToken(),
                        radial_step=100, parallel_sampling=True, edge_batch_size=30, **METHODS[method])
                    measured = metrics(result, truth)
                    dump(args.output/f'{case}-{budget}-{method}.json',
                         {k: v for k, v in result.items() if not k.startswith('_')})
                except GeometryError as error:
                    measured = dict(valid=False, failed=True, failure=str(error))
                row = dict(case=case, budget=budget, method=method, calls=provider.calls,
                           elapsed_s=time.perf_counter()-started, **measured)
                assert provider.calls <= budget
                rows.append(row)
                dump(args.output/'metrics.json', rows)
                print(json.dumps({k: row.get(k) for k in ('case', 'budget', 'method', 'calls', 'iou', 'failed', 'failure')}), flush=True)
    unchanged = all(hashlib.sha256((repo/name).read_bytes()).hexdigest() == h for name, h in hashes.items())
    dump(args.output/'audit.json', dict(runs=len(rows), synthetic_calls=sum(r['calls'] for r in rows),
        live_calls=0, source_hashes_unchanged=unchanged, failed=sum(r['failed'] for r in rows)))
    assert unchanged


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--budgets', type=int, nargs='+', default=[300])
    parser.add_argument('--methods', choices=METHODS, nargs='+', default=list(METHODS))
    parser.add_argument('--stress', choices=('none', 'development', 'holdout'), default='none')
    args = parser.parse_args()
    async def guarded():
        with no_network():
            await run(args)
    asyncio.run(guarded())


if __name__ == '__main__':
    main()
