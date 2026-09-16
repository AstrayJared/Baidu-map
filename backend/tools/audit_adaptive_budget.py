"""Offline, fail-closed replay of frozen observations. Never constructs an HTTP client.

AST hooks observe existing decisions; they do not replace selection or geometry.
Run in a fresh process with --package-root to audit a historical source snapshot.
"""
import argparse
import ast
import asyncio
import hashlib
import inspect
import json
import math
import sys
import time
from collections import Counter
from pathlib import Path
from unittest.mock import patch


class ReplayMissing(BaseException):
    """Intentionally bypass Scheduler's Exception -> unknown conversion."""


class VirtualClock:
    def __init__(self):
        self.now = 10000.

    def time(self):
        return self.now

    async def sleep(self, seconds):
        self.now += seconds


class StrictReplay:
    network = True
    identity = ('strict-offline-ledger',)

    def __init__(self, events, observer=None):
        self.events, self.observer, self.calls = events, observer, []

    async def query_walking_time(self, origin, destination, deadline):
        from life_circle.coordinates import normalize
        from life_circle.models import RouteObservation
        point = normalize(destination)
        index = len(self.calls)
        if index >= len(self.events) or tuple(self.events[index]['destination']) != point:
            # No lookup by coordinate, fallback label, interpolation or network.
            raise ReplayMissing(f'replay sequence missing/mismatch at ordinal {index + 1}')
        event = self.events[index]
        self.calls.append(point)
        if self.observer:
            self.observer.attempt(index + 1, event)
        return RouteObservation(point, event.get('duration'),
            None if event['outcome'] == 'success' else event['outcome'],
            endpoint_verified=event.get('endpoint_verified', False),
            route_origin=event.get('route_origin'), route_destination=event.get('route_destination'),
            request_origin=origin)


def accounting(events):
    counts = Counter(tuple(e['destination']) for e in events)
    confirmed = sum('dispatchPerf' in e for e in events)
    return dict(reserved=len(events), confirmed=confirmed, unconfirmed=len(events)-confirmed,
                responses=sum('responsePerf' in e for e in events),
                retries=sum(n-1 for n in counts.values()))


def cell_evidence(mesh, cell, band=120):
    points = sorted({cell.center, *cell.corners, *(p for edge in mesh.edges(cell) for p in edge)})
    observations = [mesh.samples.get(p) for p in points]
    valid = [o.duration for o in observations if o is not None and o.duration is not None]
    score, candidate, residual = mesh.priority(cell, band)
    return dict(cell=[cell.x, cell.y, cell.size], raw_score=score, candidate=candidate,
        C=bool(valid and min(valid) <= 900 < max(valid)),
        N=max(0, 1-min(abs(t-900) for t in valid)/band) if valid else 0,
        R=residual, V=max(valid)-min(valid) if valid else 0,
        far=bool(observations and all(o is not None and o.duration is not None and o.endpoint_verified
                                     for o in observations) and min(valid) > 1020),
        observations=[dict(local=p, duration=o.duration if o else None,
            reason=o.reason if o else 'missing', verified=o.endpoint_verified if o else False)
            for p, o in zip(points, observations)])


def instrument_engine(source, namespace, hook):
    tree = ast.parse(source)
    counts = Counter()
    class Instrument(ast.NodeTransformer):
        def visit_Expr(self, node):
            text = ast.unparse(node)
            event = {'await measure(initial)': 'initial', 'scheduler.close()': 'finished'}.get(text)
            if event:
                counts[event] += 1
                return [ast.Expr(ast.Call(ast.Name('_audit_hook', ast.Load()),
                    [ast.Constant(event), ast.Call(ast.Name('locals', ast.Load()), [], [])], [])), node]
            return self.generic_visit(node)

        def visit_Assign(self, node):
            if ast.unparse(node) == '_, cell = min(queue)':
                counts['decision'] += 1
                return [node, ast.parse("_audit_hook('decision', locals())").body[0]]
            return self.generic_visit(node)

        def visit_For(self, node):
            node = self.generic_visit(node)
            if isinstance(node.iter, ast.Name) and node.iter.id == 'candidates':
                counts['exploration'] += 1
                node.body.insert(0, ast.parse("_audit_hook('explore_action', locals())").body[0])
                return [ast.parse("_audit_hook('exploration', locals())").body[0], node]
            return node
    tree = Instrument().visit(tree)
    if counts != Counter(initial=1, finished=1, decision=1, exploration=1):
        raise ValueError('Unsupported engine structure: audit hooks must match exactly once')
    ast.fix_missing_locations(tree)
    scope = dict(namespace, _audit_hook=hook)
    exec(compile(tree, '<audited-engine>', 'exec'), scope)
    return scope['compute_isochrone']


class DecisionObserver:
    def __init__(self):
        self.decisions, self.exploration, self.owners = [], {}, {}
        self.seconds = 0.
        self.mesh = self.scheduler = None

    def action(self, state, cell):
        from life_circle.coordinates import normalize
        mesh, request, scheduler, projection = (state[k] for k in ('mesh', 'request', 'scheduler', 'projection'))
        kind, points = 'settle', []
        if cell.size > 100:
            kind, points = 'split', mesh.required_points(mesh.children(cell))
        else:
            active = mesh.active_candidates(cell, request.min_spacing) if request.active_sampling else []
            if active and scheduler.remaining:
                kind, points = 'active', active[:1]
            elif cell.size > request.min_size:
                kind, points = 'split', mesh.required_points(mesh.children(cell))
        coords = sorted({normalize(projection.to_geographic(p)) for p in points})
        missing = [p for p in coords if p not in scheduler.cache]
        return dict(type=kind, required=coords, cached=len(coords)-len(missing), new=len(missing),
                    cost=len(missing), affordable=bool(missing and len(missing) <= scheduler.remaining))

    def __call__(self, event, state):
        started = time.perf_counter()
        try:
            self.capture(event, state)
        finally:
            self.seconds += time.perf_counter()-started

    def capture(self, event, state):
        mesh, scheduler, request = (state[k] for k in ('mesh', 'scheduler', 'request'))
        self.mesh, self.scheduler = mesh, scheduler
        if event == 'finished':
            return
        if event == 'exploration':
            cells = sorted(mesh.leaves)
            size_ok = [c for c in cells if c.size > request.min_size]
            noncandidate = [c for c in size_ok if not mesh.priority(c, request.boundary_band)[1]]
            valid = [c for c in noncandidate if all(o is not None and o.duration is not None for o in mesh.observations(c))]
            self.exploration = dict(leaves=len(cells), size_eligible=len(size_ok),
                noncandidate=len(noncandidate), all_valid=len(valid), reserve=state['reserve'],
                ordered_candidates=[dict(cell=[c.x,c.y,c.size], action=self.action(state,c)) for c in state['candidates']])
            return
        row = dict(id=len(self.decisions), phase=event, consumed=scheduler.stats.requests,
                   remaining=scheduler.remaining, attempts=[], best_affordable_boundary=None)
        if event == 'initial':
            row.update(selected=None, action=dict(type='initial', cost=state['initial_queries']), reason='initialization')
        else:
            cell = state['cell']
            selected = cell_evidence(mesh, cell, request.boundary_band)
            action = self.action(state, cell)
            row.update(selected=selected, action=action,
                reason='selected' if action['affordable'] else 'no_new_action' if not action['cost'] else 'budget_blocked')
            if event == 'decision':
                candidates = []
                for _, other in sorted(state['queue']):
                    info = cell_evidence(mesh, other, request.boundary_band)
                    candidates.append((info['raw_score']/(4 if info['far'] else 1), other))
                    if other != cell and (info['C'] or info['N'] > 0) and row['best_affordable_boundary'] is None:
                        alternative = self.action(state, other)
                        if alternative['affordable']:
                            row['best_affordable_boundary'] = dict(evidence=info, action=alternative)
                chosen = min(candidates, key=lambda x: (-x[0], x[1]))[1]
                row['counterfactual_c1_selected'] = [chosen.x, chosen.y, chosen.size]
                row['ordering_differs'] = chosen != cell
                # Queue membership and candidate flag stay unchanged in this counterfactual.
                row['queue'] = [dict(cell=[c.x,c.y,c.size], c1_score=s) for s,c in candidates]
        self.decisions.append(row)

    def attempt(self, ordinal, event):
        row = self.decisions[-1]
        point = tuple(event['destination'])
        owner = self.owners.setdefault(point, row['id'])
        if owner != row['id']:
            raise ValueError('a retry escaped its first reserving action')
        row['attempts'].append(dict(ordinal=ordinal, point=point, owner=owner,
            retry=any(a['point'] == point for a in row['attempts']),
            confirmed='dispatchPerf' in event, response='responsePerf' in event,
            outcome=event['outcome']))


def summarize_decisions(decisions):
    result = {}
    for low, high in ((1,400),(401,800)):
        all_attempts = [(d,a) for d in decisions for a in d['attempts'] if low <= a['ordinal'] <= high]
        far = [(d,a) for d,a in all_attempts if d.get('selected') and d['selected']['far']]
        competing = [(d,a) for d,a in far if d['best_affordable_boundary'] is not None]
        result[f'{low}-{high}'] = dict(attempts=len(all_attempts), N_F=len(far), N_F_competing=len(competing),
            enough_for_complete_action=any(len(d['attempts']) >= d['best_affordable_boundary']['action']['cost']
                for d,a in competing), decisions=sorted({d['id'] for d,a in competing}))
    divergences = [d for d in decisions if d.get('ordering_differs')]
    result['first_ordering_divergence'] = ({k:divergences[0][k] for k in
        ('id','consumed','remaining','selected','counterfactual_c1_selected','best_affordable_boundary')}
        if divergences else None)
    return result


async def replay(stored, events, observe=True):
    import life_circle.engine as engine
    from life_circle.models import IsochroneRequest
    stored = json.loads(json.dumps(stored))
    observer = DecisionObserver() if observe else None
    provider = StrictReplay(events, observer)
    compute = instrument_engine(inspect.getsource(engine), vars(engine), observer) if observe else engine.compute_isochrone
    captured, geometry_seconds = {}, {}
    original = engine.reconstruct
    def reconstruct(mesh, size):
        started = time.perf_counter()
        field = original(mesh, size)
        geometry_seconds['reconstruct'] = time.perf_counter()-started
        captured.update(mesh=mesh, field=field)
        return field
    # The derived function has a copied global namespace, so replace its binding too.
    started = time.perf_counter()
    if observe:
        compute.__globals__['reconstruct'] = reconstruct
    with patch.object(engine, 'reconstruct', reconstruct):
        result = await compute(IsochroneRequest(**stored['config']), provider, clock=VirtualClock())
    compute_elapsed = time.perf_counter()-started
    serialize_start = time.perf_counter()
    wire = json.loads(json.dumps(result.to_dict(), allow_nan=False))
    serialize_elapsed = time.perf_counter()-serialize_start
    checks = dict(sequence=provider.calls == [tuple(e['destination']) for e in events],
        geometry=wire['geometry'] == stored['geometry'], unknown=wire['unknownRegion'] == stored['unknownRegion'],
        terminal=all(wire[k] == stored[k] for k in ('quality','stopReason','warnings')))
    if not all(checks.values()):
        raise ValueError(f'replay verification failed: {checks}')
    mesh = captured['mesh']
    payload = dict(checks=checks, accounting=accounting(events), result=wire,
        decisions=observer.decisions if observer else [], exploration=observer.exploration if observer else {},
        summary=summarize_decisions(observer.decisions) if observer else {},
        profiling=dict(scope='offline replay wall time; not historical live performance',
            compute_seconds=compute_elapsed, observer_seconds=observer.seconds if observer else 0,
            geometry_seconds=geometry_seconds['reconstruct'], serialization_seconds=serialize_elapsed,
            unclassified_seconds=compute_elapsed-geometry_seconds['reconstruct']-(observer.seconds if observer else 0)),
        leaves=[[c.x,c.y,c.size] for c in sorted(mesh.leaves)],
        samples=[dict(local=p, duration=o.duration, reason=o.reason, verified=o.endpoint_verified,
            owner=observer.owners.get(tuple(o.destination)) if observer else None) for p,o in sorted(mesh.samples.items())])
    # Shared benefit is separate from unique billing ownership, never summed as calls.
    if observer:
        from life_circle.coordinates import LocalProjection, normalize
        projection = LocalProjection(result.config.origin)
        payload['shared_benefit'] = [dict(cell=[c.x,c.y,c.size], owners=sorted({observer.owners[coord]
            for p in {c.center,*c.corners,*(p for edge in mesh.edges(c) for p in edge)}
            if (coord := normalize(projection.to_geographic(p))) in observer.owners})) for c in sorted(mesh.leaves)]
    return payload


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--stored', type=Path, required=True)
    parser.add_argument('--ledger', type=Path, required=True)
    parser.add_argument('--package-root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    args = parser.parse_args()
    sys.path.insert(0, str(args.package_root.resolve()))
    inputs = {str(p):sha(p) for p in (args.stored,args.ledger)}
    sources = {str(p):sha(p) for p in sorted((args.package_root/'life_circle').glob('*.py'))}
    stored = json.loads(args.stored.read_text(encoding='utf-8'))
    ledger = json.loads(args.ledger.read_text(encoding='utf-8'))
    events = [e for e in ledger['events'] if e['phase'] == 'adaptive' and 'dispatchPerf' in e]
    if args.output.exists():
        raise SystemExit('Refusing to overwrite an existing audit')
    plain = asyncio.run(replay(stored, events, observe=False))
    observed = asyncio.run(replay(stored, events, observe=True))
    observed['plain_profiling'] = plain['profiling']
    observed['input_sha256'], observed['source_sha256'] = inputs,sources
    observed['phase_accounting'] = accounting([e for e in ledger['events'] if e['phase']=='adaptive'])
    if any(sha(Path(p)) != h for p,h in {**inputs,**sources}.items()):
        raise SystemExit('Input/source changed during audit')
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(observed, ensure_ascii=False, indent=2, allow_nan=False), encoding='utf-8')
    print(json.dumps(dict(checks=observed['checks'], accounting=observed['phase_accounting'],
        summary=observed['summary'], output=str(args.output)), ensure_ascii=False))


if __name__ == '__main__':
    main()
