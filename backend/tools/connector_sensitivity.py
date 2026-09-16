"""E0: frozen endpoint-connector sensitivity; synthetic only, never routing truth."""
import argparse
import asyncio
import inspect
import json
import math
import os
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import numpy as np
import shapely
from shapely.geometry import LineString, Point, Polygon, mapping

from life_circle.coordinates import LocalProjection, normalize
from life_circle.models import CancelToken, IsochroneRequest, RouteObservation
from life_circle.scenarios import TestRoadNetwork
from tools.audit_adaptive_budget import VirtualClock, instrument_engine, sha
from tools.diagnose_sampling_coverage import capture_state
from tools.diagnostic_common import (DiagnosticStop, RunLedger, read_json, write_json,
    runtime_manifest, verify_inputs, no_network, DEVELOPMENT)
from tools.offline_accuracy import ORIGIN, ROOT

VERSION = 'E0-E2-feasibility-v1'
ORDER = ('detours-1', 'jump-0', 'jump-1')
QUOTAS = {'jump-0': 6, 'detours-1': 5, 'jump-1': 5}
CAPS = {**{f'E0-{n}': 800 for n in ORDER}, **{f'E2-{n}': 2*q for n, q in QUOTAS.items()}}
OUTPUT = Path('D:/CodexOutputs/guodingyi-local-probe-feasibility-v1/run-01')
OLD = Path('D:/CodexOutputs/guodingyi-accuracy-v3/p1-frozen-20260913')
DIAGNOSTICS = Path('D:/CodexOutputs/guodingyi-diagnostics-after-c1/run-20260913-01')
PLAN = ROOT / 'backend/docs/国定一社区_D0-D3后下一轮实验计划.md'
PLAN_HASH = '4e2fedb88a43589e3860a7d1e5cf97e6974ab680f2df89903455952557f33193'


def allowed_edge(kind, a, b):
    if kind == 'river':
        return not ((a[0] <= 100 < b[0] or b[0] <= 100 < a[0]) and a[1] != 600)
    if kind != 'wall':
        raise ValueError('Unsupported barrier kind')
    inside = lambda p: 200 <= p[0] <= 900 and -400 <= p[1] <= 400
    return inside(a) == inside(b) or {tuple(a), tuple(b)} == {(900, 0), (950, 0)}


class ConnectorModel:
    def __init__(self, kind, angle=0., scale=1.):
        if kind not in ('river', 'wall') or not math.isfinite(angle) or not math.isfinite(scale) or scale <= 0:
            raise ValueError('Invalid sensitivity model definition')
        self.kind, self.angle, self.scale = kind, angle, scale
        self.graph = TestRoadNetwork(kind)
        segments = []
        for x in self.graph.axis:
            for y in self.graph.axis:
                for dx, dy in ((50, 0), (0, 50)):
                    a, b = (x, y), (x+dx, y+dy)
                    if b[0] > 1600 or b[1] > 1600 or allowed_edge(kind, a, b):
                        continue
                    mx, my = x+dx/2, y+dy/2
                    segments.append(LineString([(mx-dy/2, my-dx/2), (mx+dy/2, my+dx/2)]))
        self.barriers = shapely.union_all(segments)

    def visible(self, a, b):
        if tuple(a) == tuple(b):
            return self.barriers.distance(Point(a)) > 1e-9
        # Endpoints are separately required off barriers, so closed and open
        # connector intersection are equivalent for all admissible candidates.
        return not LineString([a, b]).intersects(self.barriers)

    def evaluate(self, x, y):
        if not math.isfinite(x) or not math.isfinite(y):
            return dict(duration=None, reason='endpoint_invalid', node=None)
        c, s = math.cos(self.angle), math.sin(self.angle)
        u, v = (c*x+s*y)/self.scale, (-s*x+c*y)/self.scale
        if abs(u) > 1600 or abs(v) > 1600:
            return dict(duration=None, reason='outside_domain', node=None)
        if self.barriers.distance(Point(u, v)) <= 1e-9:
            return dict(duration=None, reason='endpoint_invalid', node=None)
        candidates, visible_count = [], 0
        for i in range(max(0, math.ceil((u-50+1600)/50)), min(64, math.floor((u+50+1600)/50))+1):
            for j in range(max(0, math.ceil((v-50+1600)/50)), min(64, math.floor((v+50+1600)/50))+1):
                node = (float(self.graph.axis[i]), float(self.graph.axis[j]))
                connector = math.hypot(u-node[0], v-node[1])
                if connector > 50 or not self.visible((u, v), node):
                    continue
                visible_count += 1
                distance = float(self.graph.distances[j, i])
                if math.isfinite(distance):
                    candidates.append((distance+connector, node, distance, connector))
        if not candidates:
            return dict(duration=None, reason='no_path' if visible_count else 'no_connector', node=None)
        minimum = min(row[0] for row in candidates)
        cost, node, distance, connector = min((r for r in candidates if r[0]-minimum <= 1e-9), key=lambda r: r[1])
        return dict(duration=self.scale*cost/1.2, reason=None, node=list(node),
                    graph_m=distance, connector_m=connector, canonical=[u, v])


class ModelProvider:
    def __init__(self, model):
        self.model = model
        self.projection = LocalProjection(ORIGIN)

    async def __call__(self, origin, destination, deadline):
        row = self.model.evaluate(*self.projection.to_local(destination))
        return RouteObservation(destination, row['duration'], row['reason'],
            endpoint_verified=row['duration'] is not None, request_origin=origin)


class AuditedProvider:
    network = False

    def __init__(self, inner, ledger, phase, identity, token):
        self.inner, self.ledger, self.phase, self.run_id = inner, ledger, phase, identity
        self.identity = (VERSION, identity)
        self.token, self.closed, self.events = token, False, []

    def checkpoint(self):
        try:
            write_json(self.ledger.directory / f'{self.run_id}-calls.json', self.events)
        except Exception:
            self.ledger.broken = True
            raise DiagnosticStop('Call audit write failed; no further sampling') from None

    def close(self):
        self.closed = True

    def accounting(self):
        used = self.ledger.data['runs'][self.run_id]['used']
        points = Counter(tuple(e['destination']) for e in self.events if e['confirmed'])
        return dict(reserved_attempts=used, confirmed_calls=sum(points.values()),
            unconfirmed_calls=used-sum(points.values()), complete_responses=sum(e['response'] for e in self.events),
            retries=sum(n-1 for n in points.values()),
            errors=dict(Counter(e['reason'] for e in self.events if e.get('reason'))))

    async def query_walking_time(self, origin, destination, deadline):
        if self.closed or self.token.cancelled or self.ledger.broken:
            raise DiagnosticStop('Closed or cancelled diagnostic provider')
        self.ledger.consume(self.phase, self.run_id, dict(point=destination))
        event = dict(ordinal=len(self.events)+1, destination=list(destination), confirmed=False, response=False)
        self.events.append(event)
        self.checkpoint()  # Durable reservation but NO confirmation before invocation.
        started = time.perf_counter()
        try:
            event['confirmed'] = True
            response = await self.inner(origin, destination, deadline)
            event.update(response=True, duration=response.duration, reason=response.reason,
                         endpoint_verified=response.endpoint_verified)
        except BaseException as exc:
            event['reason'] = 'cancelled' if isinstance(exc, asyncio.CancelledError) else 'execution_error'
            raise
        finally:
            event['elapsed_seconds'] = time.perf_counter()-started
            event['late'] = self.closed or self.token.cancelled
            self.checkpoint()
        if self.closed or self.token.cancelled:
            return RouteObservation(destination, reason='cancelled')
        return response


def reference_grid():
    axis = np.arange(-1600, 1601, 20.)
    xx, yy = np.meshgrid(axis, axis)
    return axis, xx, yy


def coverage_gate(rows):
    return len(rows) == 3 and all(r['status'] == 'completed' and r['valid_fraction'] >= .8 for r in rows)


def save_arrays(path, **arrays):
    try:
        with path.open('xb') as stream:
            np.savez_compressed(stream, **arrays)
            stream.flush()
            os.fsync(stream.fileno())
    except Exception:
        raise DiagnosticStop('Array checkpoint failed; no repeat') from None


def field_payload(field, state):
    raw = shapely.union_all([Polygon(t['vertices']) for t in state['triangles'] if all(v is not None for v in t['values'])])
    return {**{name: mapping(getattr(field, name)) for name in ('geometry', 'unknown', 'support')},
            'raw_support': mapping(raw)}


async def run_baseline(ledger, phase, identity, model, request, token=None):
    import life_circle.engine as engine
    token = token or CancelToken()
    ledger.start(phase, identity, request.budget)
    provider = AuditedProvider(ModelProvider(model), ledger, phase, identity, token)
    captured = dict(state=None, fields=[], reconstruction_seconds=0.)
    def hook(event, local):
        if event == 'finished':
            captured['state'] = capture_state(local['mesh'])
    def rebuild(mesh, raster):
        started = time.perf_counter()
        field = engine.reconstruct(mesh, raster)
        captured['reconstruction_seconds'] += time.perf_counter()-started
        captured['fields'].append(field)
        return field
    compute = instrument_engine(inspect.getsource(engine), dict(vars(engine), reconstruct=rebuild), hook)
    # Executing the frozen module performs its own import of reconstruct.
    # Replace only the isolated function namespace AFTER that import.
    compute.__globals__['reconstruct'] = rebuild
    started = time.perf_counter()
    row = dict(status='failed', result=None, field=None, state=None)
    try:
        result = await compute(request, provider, token, clock=VirtualClock(), experimental_far_discount=False)
        if token.cancelled or result.stop_reason == 'cancelled':
            row.update(status='cancelled', stop_reason='cancelled')
        else:
            row.update(status='completed', result=result.to_dict(), stop_reason=result.stop_reason, quality=result.quality,
                       scheduler_reservations=result.statistics.requests, cache_hits=result.statistics.cache_hits)
            if captured['fields']:
                field = captured['fields'][-1]
                row['field'] = field_payload(field, captured['state'])
                save_arrays(ledger.directory/f'{identity}-field.npz', x=field.x, z=field.z, valid=np.isfinite(field.z))
    except (DiagnosticStop, asyncio.CancelledError):
        provider.close()
        raise
    except Exception:
        row.update(status='failed', stop_reason='execution_error', quality='execution_failed')
    finally:
        provider.close()
    row.update(state=captured['state'], counts=provider.accounting(), elapsed_seconds=time.perf_counter()-started,
               reconstruction_count=len(captured['fields']), reconstruction_seconds=captured['reconstruction_seconds'], request=asdict(request))
    write_json(ledger.directory/f'{identity}.json', row)
    ledger.finish(identity, row['status'])
    return row


def audit_inputs():
    manifest = read_json(ROOT/'backend/docs/国定一社区_D0-D3冻结清单.json')
    inputs = {str(DIAGNOSTICS/name): digest for name, digest in manifest['artifacts'].items()}
    inputs.update(read_json(DIAGNOSTICS/'D0.json')['inputs'])
    for name, digest in manifest['runtime']['algorithm_sha256'].items():
        inputs[str(ROOT/'life-circle-algorithm/src/life_circle'/name)] = digest
    for name, digest in manifest['report_sha256'].items():
        inputs[str(ROOT/'backend/docs'/name)] = digest
    inputs[str(PLAN)] = PLAN_HASH
    verify_inputs(dict(inputs=inputs))
    return inputs


def model_definitions():
    old = read_json(OLD/'protocol.json')
    d = next(row for row in old['definitions'] if row['id'] == 'detours-1')
    rows = [dict(id='detours-1', kind='wall', angle=d['angle'], scale=1.)]
    for d in read_json(DIAGNOSTICS/'D3/diagnostic-layouts.json')['definitions']:
        if d['id'] in ('jump-0', 'jump-1'):
            rows.append({key: d[key] for key in ('id', 'kind', 'angle', 'scale')})
    if tuple(r['id'] for r in rows) != ORDER:
        raise DiagnosticStop('Unexpected development layout order')
    return rows


def freeze(output):
    if output.exists():
        raise DiagnosticStop('Existing output; no restart, overwrite or reset')
    if subprocess.check_output(['git', 'status', '--porcelain'], cwd=ROOT, text=True).strip():
        raise DiagnosticStop('Commit experimental tools before the formal run')
    inputs = audit_inputs()
    runtime = runtime_manifest()
    for name, digest in runtime['tool_sha256'].items():
        inputs[str(ROOT/'backend/tools'/name)] = digest
    for name in ('test_connector_sensitivity.py', 'test_local_probe_feasibility.py'):
        path = ROOT/'backend/tests'/name
        inputs[str(path)] = sha(path)
    definitions = model_definitions()
    ledger = RunLedger.create(output, CAPS)
    protocol = dict(version=VERSION, inputs=inputs, runtime=runtime, definitions=definitions,
        caps=CAPS, total_sampling_cap=2432, reference_positions=77763, extra_reconstructions_cap=6,
        evaluation_rule='np.arange(-1600,1601,20.); meshgrid; C order',
        origin=ORIGIN, seed=20260911, probe_quotas=QUOTAS, trigger='valid_verified & min<=900<max & span>300',
        network_requests=0, sealed_predictions=0, reference_hashes={})
    write_json(output/'protocol.json', protocol)  # rules exist before reference evaluations
    axis, xx, yy = reference_grid()
    save_arrays(output/'reference-points.npz', axis=axis, x=xx, y=yy)
    write_json(output/'reference-points.sha256.json', {'sha256': sha(output/'reference-points.npz')})
    for definition in definitions:
        model = ConnectorModel(definition['kind'], definition['angle'], definition['scale'])
        rows = [model.evaluate(float(x), float(y)) for x, y in zip(xx.ravel(), yy.ravel())]
        truth = np.array([r['duration'] if r['duration'] is not None else np.nan for r in rows]).reshape(xx.shape)
        reasons = np.array([r['reason'] or 'valid' for r in rows]).reshape(xx.shape)
        path = output/f"{definition['id']}-reference.npz"
        save_arrays(path, axis=axis, truth=truth, reasons=reasons)
        protocol['reference_hashes'][path.name] = sha(path)
        write_json(output/f"{definition['id']}-model.json", {**definition, 'barriers': mapping(model.barriers),
            'valid_fraction': float(np.isfinite(truth).mean()), 'reason_counts': dict(Counter(reasons.ravel()))})
        print(json.dumps(dict(stage='E0_reference', layout=definition['id'], positions=int(truth.size),
                              valid_fraction=float(np.isfinite(truth).mean()))), flush=True)
    write_json(output/'protocol.json', protocol)
    write_json(output/'protocol.sha256.json', dict(sha256=sha(output/'protocol.json')))
    return ledger, protocol


def verify_protocol(output, protocol):
    if sha(output/'protocol.json') != read_json(output/'protocol.sha256.json')['sha256']:
        raise DiagnosticStop('Frozen protocol changed')
    verify_inputs(protocol)
    for name, digest in protocol['reference_hashes'].items():
        if sha(output/name) != digest:
            raise DiagnosticStop('Reference changed')


async def execute(output):
    from tools.local_probe_feasibility import execute_after_e0, write_report_data
    ledger, protocol = freeze(output)
    rows = []
    for definition in protocol['definitions']:
        name = definition['id']
        valid = read_json(output/f'{name}-model.json')['valid_fraction']
        rows.append(dict(layout=name, valid_fraction=valid, status='completed'))
    if not coverage_gate(rows):
        write_json(output/'summary.json', dict(status='evidence_insufficient', reason='E0_reference_coverage', E0=rows,
            E1='not_executed', E2='not_executed', sampling_attempts=0, live_requests=0))
        return
    completed = {}
    for definition in protocol['definitions']:
        verify_protocol(output, protocol)
        name = definition['id']
        model = ConnectorModel(definition['kind'], definition['angle'], definition['scale'])
        row = await run_baseline(ledger, f'E0-{name}', f'E0-{name}', model,
                                 IsochroneRequest(ORIGIN, 'bd09ll', budget=800, expand=False))
        completed[name] = row
        print(json.dumps(dict(stage='E0', layout=name, status=row['status'], counts=row['counts']), ensure_ascii=False), flush=True)
    verify_protocol(output, protocol)
    await execute_after_e0(output, ledger, protocol, completed)
    verify_protocol(output, protocol)
    write_report_data(output)


def run_offline(factory):
    # Windows creates its self-pipe using a loopback socket pair. Initialize
    # that runtime plumbing before blocking sockets, but guard the experiment
    # AND cancellation/shutdown of every task. The factory has not run yet.
    runner = asyncio.Runner()
    runner.get_loop()
    with no_network():
        try:
            return runner.run(factory())
        finally:
            runner.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=OUTPUT)
    args = parser.parse_args()
    if args.output.resolve() != OUTPUT.resolve():
        raise SystemExit('Only frozen output path is allowed')
    try:
        run_offline(lambda: execute(args.output))
    except (DiagnosticStop, asyncio.CancelledError, KeyboardInterrupt):
        raise SystemExit('Diagnostic stopped. Preserve output and budget; do not restart.') from None


if __name__ == '__main__':
    main()
