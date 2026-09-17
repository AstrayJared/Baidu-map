"""Replay generation evidence and compare frozen circles on common points; no IO to Baidu."""
import asyncio
import json
import math
from pathlib import Path

from shapely.geometry import Point, shape

from app.algorithms.hybrid_isochrone.cache import ReplayProvider
from app.algorithms.hybrid_isochrone.engine import HybridIsochroneProvider
from app.algorithms.hybrid_isochrone.hard_obstacles import LocalObstacles
from app.algorithms.hybrid_isochrone.models import HybridConfig
from app.geo.projection import MetricProjection


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


class OfflineGate:
    interval = 0

    def __init__(self):
        self.attempt_lock = asyncio.Lock()

    async def wait(self, *args, **kwargs):
        return True

    def completed(self, reason):
        pass


def audit(root):
    runs = {}
    for version in ('v15', 'v16'):
        directory = root / '.hybrid-ledgers' / f'verification-{version}'
        task = directory / 'tasks' / read(directory / 'generation-task.json')['task_id']
        runs[version] = dict(ledger=read(task/'ledger.json'), diagnostics=read(task/'diagnostics.json')['diagnostics'],
                             plan=read(directory/'validation-plan.json'), validation=read(directory/'validation-ledger.json'))
    baseline = runs['v15']
    ledger, diagnostics = baseline['ledger'], baseline['diagnostics']

    class FrozenGuidance:
        warnings = []

        def candidates(self, count):
            points = []
            for s in [s for s in ledger['samples'] if s['reason'] == 'topology_proposal'][:count]:
                xy = tuple(s['xy'])
                # The original unrounded OSM proposal is not in the ledger.
                # Recover a proposal mapping to the exact frozen BD09 endpoint;
                # do not change that endpoint or approximate its response.
                for _ in range(10):
                    coordinate = engine.session.coordinate(xy)
                    if coordinate == tuple(s['request_coordinate']):
                        break
                    actual = engine.projection.origin(coordinate)
                    xy = tuple(xy[i] + s['xy'][i] - actual[i] for i in range(2))
                if engine.session.coordinate(xy) != tuple(s['request_coordinate']):
                    raise ValueError('could_not_recover_frozen_proposal')
                points.append(xy)
            return points

        def inspect(self, xy):
            row = min(ledger['samples'], key=lambda s: math.dist(xy, s['xy']))
            if math.dist(xy, row['xy']) > .001:
                raise ValueError('unrecorded_guidance')
            return row['osm_guidance']

    engine = HybridIsochroneProvider(MetricProjection(diagnostics['metric_crs']), ReplayProvider(ledger), OfflineGate(),
                                    FrozenGuidance(), obstacles=LocalObstacles(water=shape(diagnostics['metric_hard_obstacles'])))
    rebuilt = asyncio.run(engine.compute(tuple(ledger['origin']), HybridConfig(**ledger['config'])))
    geometry = {v: shape(r['diagnostics']['metric_geometry']) for v, r in runs.items()}
    replay = dict(same_request_sequence=[list(s.coordinate) for s in engine.session.samples] ==
                  [s['request_coordinate'] for s in ledger['samples']],
                  requests_replayed=rebuilt['requests_used'], new_network_requests=0,
                  symmetric_difference_m2=shape(rebuilt['diagnostics']['metric_geometry']).symmetric_difference(geometry['v15']).area)
    comparisons = []
    for reference, run in runs.items():
        observations = {tuple(s['request_coordinate']): s['evidence'] for s in run['validation']['samples']}
        cases = [(c, observations.get(tuple(c['coordinate']), {})) for c in run['plan']['cases']]
        cases = [(c, e) for c, e in cases if c.get('prediction') is not None and e.get('reachable') is not None]
        for version, g in geometry.items():
            wrong, tolerant_wrong = 0, 0
            for c, e in cases:
                inside = g.covers(Point(c['xy']))
                wrong += inside != (e['duration'] <= 900)
                tolerant_wrong += (inside and e['duration'] > 915) or (not inside and e['duration'] < 885)
            comparisons.append(dict(reference_set=reference, geometry=version, valid=len(cases),
                                    strict_errors=wrong, tolerant_errors=tolerant_wrong,
                                    strict_accuracy=1-wrong/len(cases), tolerant_accuracy=1-tolerant_wrong/len(cases)))
    return dict(replay=replay, comparisons=comparisons,
                scope='retrospective_common_point_membership_only_not_new_independent_acceptance')


if __name__ == '__main__':
    root = Path(__file__).resolve().parents[1]
    result = audit(root)
    (root/'.tmp/rollback-audit.json').write_text(json.dumps(result, indent=2), encoding='utf-8')
    print(json.dumps(result))
