"""Frozen early-POI and evidence-directed local search ablations; representative development only."""
import argparse
import asyncio
import hashlib
import importlib.metadata
import json
from pathlib import Path
import zipfile

from shapely.geometry import mapping

from tools.diagnostic_common import no_network
from tools.endpoint_e83_poi import run_task
from tools.endpoint_e83_poi_experiment import CASES, LOADS, catalog, demand_for, scene, evaluate, risk_groups
from tools.live_smoke import dump


async def run(output, cases, budgets, policies, baseline_budget=300):
    output.mkdir(parents=True, exist_ok=False)
    repo = Path(__file__).resolve().parents[2]
    paths = sorted([*repo.joinpath('backend/tools').glob('*.py'),
                    *repo.joinpath('backend/tests').glob('test_endpoint_e83*.py'),
                    *repo.joinpath('life-circle-algorithm/src').rglob('*.py')])
    hashes = {p.relative_to(repo).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest() for p in paths}
    fixtures = {}
    for case in cases:
        truth, _ = scene(case)
        for n, k in LOADS:
            pois, labels = catalog(case, n, k, truth)
            fixtures[f'{case}-{n}'] = dict(pois=pois, reference=labels, truth_local=mapping(truth))
    dump(output/'fixtures.json', fixtures)
    arms = [('independent', baseline_budget)] + [(policy, b) for b in budgets for policy in policies]
    protocol = dict(cases=cases, budgets=budgets, arms=arms, loads=LOADS, workload='single',
        source_sha256=hashes, fixture_sha256=hashlib.sha256((output/'fixtures.json').read_bytes()).hexdigest(),
        live_calls=0, target_m=25, circle_batch_size=30, guided_focus_batch_size=10,
        geometric_regression=dict(iou_drop=.01, either_outer_p95_increase_m=10),
        packages={p: importlib.metadata.version(p) for p in ('numpy', 'shapely', 'httpx')},
        scope='Same catalogs as round-03; single workload; no holdout; critical is previous shared policy; early changes POI timing; guided adds focus-first discovery; discover imports far positive POIs, discovers incident faces and preserves original edge order',
        validation_caps='V=N and V=min(N,50), deduplicated; no increased provider budget')
    dump(output/'protocol.json', protocol)
    with zipfile.ZipFile(output/'source.zip', 'w', zipfile.ZIP_DEFLATED) as archive:
        for p in paths:
            archive.write(p, p.relative_to(repo))
    rows = []
    for case in cases:
        truth, factory = scene(case)
        for n, k in LOADS:
            frozen = fixtures[f'{case}-{n}']; pois, labels = frozen['pois'], frozen['reference']
            demand = demand_for(pois, 'single')
            for cap in sorted({n, min(n, 50)}):
                risks = {}
                for mode, budget in arms:
                    identity = f'{case}-n{n}-v{cap}-{mode}-{budget}'
                    task = await run_task(case, factory(), pois, demand, circle_budget=budget, poi_budget=cap,
                        shared=mode != 'independent', poi_policy=mode if mode != 'independent' else 'critical',
                        run_id=identity)
                    if mode == 'independent':
                        risks = risk_groups(task['circle'], pois)
                        dump(output/f'{case}-n{n}-v{cap}-risk.json', risks)
                    measured = evaluate(task, pois, labels, truth, risks)
                    row = dict(id=identity, case=case, n=n, k=k, v=cap, workload='single', mode=mode,
                               budget=budget, **measured)
                    if task['circle']:
                        task['circle'] = {key: val for key, val in task['circle'].items() if not key.startswith('_')}
                    dump(output/f'{identity}.json', task)
                    rows.append(row)
                    dump(output/'metrics.json', rows)
                    print(json.dumps(dict(id=identity, calls=task['cost']['total_calls'],
                        iou=measured['circle'].get('iou'), failed=measured['circle']['failed'],
                        known_negative_covered=measured['all_known_negative_covered'])), flush=True)
    unchanged = all(hashlib.sha256((repo/p).read_bytes()).hexdigest() == h for p,h in hashes.items())
    unchanged_fixture = hashlib.sha256((output/'fixtures.json').read_bytes()).hexdigest() == protocol['fixture_sha256']
    dump(output/'audit.json', dict(runs=len(rows), synthetic_calls=sum(r['cost']['total_calls'] for r in rows),
        live_calls=0, source_hashes_unchanged=unchanged, fixture_unchanged=unchanged_fixture,
        geometry_failed=sum(bool(r['circle']['failed'] or r['layer_errors'] or r['circle_error']) for r in rows),
        negative_conflict_runs=sum(r['all_known_negative_covered'] > 0 for r in rows),
        wrong_minutes=sum(r['facility']['all']['wrong_minutes'] for r in rows)))
    assert unchanged and unchanged_fixture


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--cases', choices=CASES, nargs='+', default=list(CASES))
    parser.add_argument('--budgets', type=int, choices=(150, 200, 250, 300, 400), nargs='+', default=[250])
    parser.add_argument('--baseline-budget', type=int, choices=(150, 200, 250, 300, 400), default=300)
    parser.add_argument('--policies', choices=('critical', 'early', 'guided', 'discover'), nargs='+',
                        default=['critical', 'discover'])
    args = parser.parse_args()
    async def guarded():
        with no_network():
            await run(args.output, args.cases, args.budgets, args.policies, args.baseline_budget)
    asyncio.run(guarded())


if __name__ == '__main__':
    main()
