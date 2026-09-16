"""E1/E2: predeclared risk diagnostic and bounded internal probes, not C2."""
import copy
import json
import time
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import shapely
from shapely.geometry import Polygon, box, mapping, shape

from life_circle.coordinates import LocalProjection, normalize
from life_circle.field import FieldResult, reconstruct
from life_circle.models import CancelToken, IsochroneRequest
from life_circle.scheduler import Scheduler
from tools.audit_adaptive_budget import VirtualClock, sha
from tools.diagnose_fixed_geometry import FixedTriangles, field_prediction, transitions
from tools.diagnostic_common import DiagnosticStop, read_json, write_json, write_csv, DEVELOPMENT
from tools.offline_accuracy import ORIGIN, confusion, predict, components


def features(state, layout):
    """Pure selector input: observed samples/triangles only. No oracle or labels."""
    observed = {tuple(s['local']): s['observation'] for s in state['samples']}
    rows = []
    for ordinal, triangle in enumerate(state['triangles']):
        vertices, values = triangle['vertices'], triangle['values']
        key = sorted(tuple(map(float, p)) for p in vertices)
        valid = all(t is not None and np.isfinite(t) and t >= 0 for t in values)
        verified = all(observed.get(tuple(p), {}).get('endpoint_verified', False) for p in vertices)
        span = float(max(values)-min(values)) if valid else None
        crossing = bool(valid and verified and min(values) <= 900 < max(values))
        rows.append(dict(ordinal=ordinal, id=layout+':'+json.dumps(key, separators=(',', ':')),
            key=key, vertices=vertices, values=values, valid=bool(valid), verified=bool(verified),
            span=span, area=Polygon(vertices).area, crossing=crossing, risk=bool(crossing and span > 300),
            linear_center=float(np.mean(values)) if valid else None))
    return rows


def risk_statistics(rows, xy, truth, prediction):
    rows = sorted(rows, key=lambda r: r['key'])
    tree = shapely.STRtree([Polygon(r['vertices']) for r in rows])
    matches = tree.query(shapely.points(xy), predicate='intersects')
    assigned = np.full(len(xy), len(rows), dtype=int)
    np.minimum.at(assigned, matches[0], matches[1])
    cross = np.array([r['crossing'] for r in rows]+[False])[assigned]
    risk = np.array([r['risk'] for r in rows]+[False])[assigned]
    valid = np.isfinite(np.asarray(truth).ravel())
    cross &= valid
    risk &= valid
    control = cross & ~risk
    severe = (np.asarray(truth).ravel() > 1020) & (np.asarray(prediction).ravel() == 1) & valid
    return dict(crossing_points=int(cross.sum()), risk_points=int(risk.sum()), control_points=int(control.sum()),
        crossing_severe=int((cross & severe).sum()), risk_severe=int((risk & severe).sum()),
        control_severe=int((control & severe).sum()), severe_total=int(severe.sum()))


def risk_gate(rows):
    keys = ('crossing_points', 'risk_points', 'control_points', 'crossing_severe', 'risk_severe', 'control_severe')
    totals = {k: sum(row[k] for row in rows.values()) for k in keys}
    capture = totals['risk_severe']/totals['crossing_severe'] if totals['crossing_severe'] else None
    enrichment = dict(value=None, status='undefined')
    if totals['risk_points'] and totals['control_points']:
        a = totals['risk_severe']/totals['risk_points']
        b = totals['control_severe']/totals['control_points']
        if b:
            enrichment = dict(value=a/b, status='finite')
        elif a:
            enrichment['status'] = 'positive_infinity'
    minimum = lambda row: row['crossing_points'] >= 20 and row['risk_points'] >= 5 and row['control_points'] >= 5
    witness = lambda row: minimum(row) and row['crossing_severe'] >= 1 and row['risk_severe'] >= 1
    structure_size = minimum(rows['jump-0']) and any(minimum(rows[n]) for n in ('detours-1', 'jump-1'))
    structure = witness(rows['jump-0']) and any(witness(rows[n]) for n in ('detours-1', 'jump-1'))
    enriched = enrichment['status'] == 'positive_infinity' or (enrichment['value'] is not None and enrichment['value'] >= 2)
    return dict(passed=bool(structure and capture is not None and capture >= .5 and enriched),
        sufficient=bool(structure_size), bridge_wall_witness=bool(structure), totals=totals,
        capture=capture, enrichment=enrichment)


def weights_at(vertices, point):
    vertices = np.asarray(vertices, dtype=float)
    return np.linalg.solve(np.vstack((vertices.T, np.ones(3))), np.r_[point, 1.])


def select_probes(state, layout, quota):
    projection = LocalProjection(ORIGIN)
    cached = {normalize(s['observation']['destination']) for s in state['samples']}
    rows = sorted((r for r in features(state, layout) if r['risk']), key=lambda r: (-r['span'], -r['area'], r['key']))
    selected, excluded = [], []
    for rank, row in enumerate(rows):
        if len(selected) == quota:
            break
        center = np.mean(row['vertices'], axis=0)
        destination = normalize(projection.to_geographic(center))
        actual = projection.to_local(destination)
        weights = weights_at(row['vertices'], actual)
        reason = 'cached_or_duplicate' if destination in cached else 'outside_or_weights' if min(weights) < .2 or not Polygon(row['vertices']).contains(shapely.Point(actual)) else None
        if reason:
            excluded.append(dict(id=row['id'], reason=reason))
            continue
        selected.append(dict(layout=layout, ordinal=row['ordinal'], id=row['id'], vertices=row['vertices'], values=row['values'],
            rank=rank, span=row['span'], area=row['area'], original_center=center.tolist(), destination=list(destination),
            actual_local=list(actual), weights=weights.tolist(), linear_duration=float(np.dot(weights, row['values']))))
        cached.add(destination)
    return dict(complete=len(selected) == quota, quota=quota, points=selected, exclusions=excluded)


def split_state(state, selected, durations):
    if len(selected) != len(durations) or len({p['ordinal'] for p in selected}) != len(selected):
        raise DiagnosticStop('Invalid probe map')
    replacements = {p['ordinal']: (p, t) for p, t in zip(selected, durations)}
    result = copy.deepcopy(state)
    result['triangles'] = []
    for ordinal, triangle in enumerate(state['triangles']):
        if ordinal not in replacements:
            result['triangles'].append(copy.deepcopy(triangle))
            continue
        probe, duration = replacements[ordinal]
        if triangle['vertices'] != probe['vertices'] or triangle['values'] != probe['values']:
            raise DiagnosticStop('Parent triangle changed')
        if not Polygon(triangle['vertices']).contains(shapely.Point(probe['actual_local'])):
            raise DiagnosticStop('Probe outside parent')
        for i, j in ((0, 1), (1, 2), (2, 0)):
            result['triangles'].append(dict(vertices=[triangle['vertices'][i], triangle['vertices'][j], probe['actual_local']],
                values=[triangle['values'][i], triangle['values'][j], duration]))
    return result


def raw_support(state):
    return shapely.union_all([Polygon(t['vertices']) for t in state['triangles'] if all(v is not None for v in t['values'])])


def compare_representation(before_state, after_state, selected, before, after, axis):
    xx, yy = np.meshgrid(axis, axis)
    mismatch = int(np.sum(field_prediction(before, xx, yy) != field_prediction(after, xx, yy)))
    old_geometries = dict(geometry=before.geometry, unknown=before.unknown, support=before.support, raw_support=raw_support(before_state))
    new_geometries = dict(geometry=after.geometry, unknown=after.unknown, support=after.support, raw_support=raw_support(after_state))
    domain_area = (2*before_state['extent'])**2
    differences = {k: old_geometries[k].symmetric_difference(new_geometries[k]) for k in old_geometries}
    areas = {k: g.area for k, g in differences.items()}
    local = []
    for probe in selected:
        a, b, c = probe['vertices']
        p = probe['actual_local']
        parent = Polygon([a, b, c])
        children = [Polygon([a, b, p]), Polygon([b, c, p]), Polygon([c, a, p])]
        union = shapely.union_all(children)
        checks = dict(union_error=union.symmetric_difference(parent).area,
            overlap=sum(g.area for g in children)-union.area, area_error=abs(sum(g.area for g in children)-parent.area),
            boundary_error=union.boundary.hausdorff_distance(parent.boundary),
            geometry_differences={k: g.intersection(parent).area for k, g in differences.items()})
        checks['passed'] = bool(max(checks['union_error'], abs(checks['overlap']), checks['area_error'],
            *checks['geometry_differences'].values()) <= 1e-8*parent.area and checks['boundary_error'] <= 1e-9)
        local.append(checks)
    return dict(passed=bool(mismatch == 0 and all(v <= 1e-8*domain_area for v in areas.values()) and all(c['passed'] for c in local)),
                prediction_mismatches=mismatch, global_area_differences=areas, local=local)


def score(truth, prediction):
    truth, prediction = np.asarray(truth), np.asarray(prediction)
    valid = np.isfinite(truth)
    return {**confusion(truth[valid] <= 900, prediction[valid]),
        'invalid_reference': int((~valid).sum()), 'reference_total': int(truth.size),
        'valid_fraction': float(valid.mean()),
        'severe_fp': int(((truth > 1020) & (prediction == 1) & valid).sum()),
        'above_960_fp': int(((truth > 960) & (prediction == 1) & valid).sum())}


def outcome(truth, before, after):
    truth, before, after = map(np.asarray, (truth, before, after))
    valid = np.isfinite(truth)
    positive = valid & (truth <= 900)
    old, new = score(truth, before), score(truth, after)
    new_severe = np.flatnonzero((valid & (truth > 1020) & (before != 1) & (after == 1)).ravel()).tolist()
    tp_unknown = np.flatnonzero((positive & (before == 1) & (after == -1)).ravel()).tolist()
    fn_change = new['FN_known']+new['FN_unknown']-old['FN_known']-old['FN_unknown']
    labels = components(positive) if positive.ndim == 2 else components(positive.reshape(1, -1)).reshape(positive.shape)
    lost = [int(k) for k in np.unique(labels) if k and np.any((labels == k) & (before == 1)) and not np.any((labels == k) & (after == 1))]
    return dict(before=old, after=new, fn_change=fn_change,
        direct_severe_fixed=int((valid & (truth > 1020) & (before == 1) & (after == 0)).sum()),
        new_severe_ids=new_severe, tp_to_unknown_ids=tp_unknown, lost_component_ids=lost,
        guardrail_failed=bool(new_severe or tp_unknown or fn_change > 0 or lost),
        transitions=transitions(truth[valid] <= 900, before[valid], after[valid]))


def e2_gate(rows, all_valid):
    if any(r['guardrail_failed'] for r in rows.values()):
        return 'guardrail_failed'
    if not all_valid or set(rows) != {'jump-0', 'jump-1', 'detours-1'}:
        return 'evidence_insufficient'
    if rows['jump-0']['direct_severe_fixed'] < 1 or not any(rows[n]['direct_severe_fixed'] >= 1 for n in ('detours-1', 'jump-1')):
        return 'mechanism_not_supported'
    return 'accuracy_candidate_evidence' if any(r['fn_change'] < 0 for r in rows.values()) else 'local_mechanism_supported'


def saved_prediction(saved, axis):
    xx, yy = np.meshgrid(axis, axis)
    if saved['field'] is None:
        return np.full(xx.shape, -1, dtype=np.int8)
    return predict(SimpleNamespace(local_geometry=shape(saved['field']['geometry']), local_unknown=shape(saved['field']['unknown'])), xx, yy)


def load_field(output, name, saved):
    with np.load(output/f'E0-{name}-field.npz') as data:
        axis, z = data['x'].copy(), data['z'].copy()
    return FieldResult(*(shape(saved['field'][k]) for k in ('geometry', 'unknown', 'support')), axis, z)


def counted_rebuild(ledger, identity, state):
    records = ledger.data.setdefault('reconstructions', {})
    if identity in records or len(records) >= 6:
        raise DiagnosticStop('Reconstruction already started or cap reached')
    records[identity] = dict(status='running')
    ledger.save()
    started = time.perf_counter()
    try:
        field = reconstruct(FixedTriangles(state), 25)
    except Exception:
        records[identity].update(status='failed', elapsed_seconds=time.perf_counter()-started)
        ledger.save()
        raise
    records[identity].update(status='completed', elapsed_seconds=time.perf_counter()-started)
    ledger.save()
    return field


def verify_selection(output, selections):
    if sha(output/'probe-selection.json') != read_json(output/'probe-selection.sha256.json')['sha256']:
        raise DiagnosticStop('Frozen probe file changed')
    if read_json(output/'probe-selection.json') != json.loads(json.dumps(selections, allow_nan=False)):
        raise DiagnosticStop('Frozen probe coordinates changed in memory')


async def run_e2(output, ledger, saved, references, selections, models):
    from tools.connector_sensitivity import (QUOTAS, AuditedProvider, ModelProvider, save_arrays, field_payload)
    checks, before_fields = {}, {}
    verify_selection(output, selections)
    for name in QUOTAS:
        state, selected = saved[name]['state'], selections[name]['points']
        before = load_field(output, name, saved[name])
        before_fields[name] = before
        linear = split_state(state, selected, [p['linear_duration'] for p in selected])
        try:
            control = counted_rebuild(ledger, f'linear-{name}', linear)
        except Exception:
            checks[name] = dict(passed=False, reason='reconstruction_failed')
            return dict(status='representation_invalid', checks=checks, measurements='not_executed')
        checks[name] = compare_representation(state, linear, selected, before, control, references[name]['axis'])
        write_json(output/f'linear-{name}.json', dict(checks=checks[name], field=field_payload(control, linear)))
        if not checks[name]['passed']:
            return dict(status='representation_invalid', checks=checks, measurements='not_executed')
    observations, counts = {}, {}
    # All three linear controls pass before ANY probe can invoke a model.
    for name, quota in QUOTAS.items():
        verify_selection(output, selections)
        phase = f'E2-{name}'
        ledger.start(phase, phase, 2*quota)
        token = CancelToken()
        provider = AuditedProvider(ModelProvider(models[name]), ledger, phase, phase, token)
        request = IsochroneRequest(ORIGIN, 'bd09ll', budget=2*quota, expand=False)
        scheduler = Scheduler(request, provider, token, VirtualClock())
        try:
            observations[name] = await scheduler.observe_many([tuple(p['destination']) for p in selections[name]['points']])
        finally:
            scheduler.close()
            provider.close()
        counts[name] = provider.accounting()
        write_json(output/f'{phase}-observations.json', [dict(destination=o.destination, duration=o.duration, reason=o.reason,
            endpoint_verified=o.endpoint_verified) for o in observations[name]])
        ledger.finish(phase, 'cancelled' if token.cancelled else 'completed')
        if token.cancelled:
            raise DiagnosticStop('Probe task cancelled')
    outcomes = {}
    for name in QUOTAS:
        verify_selection(output, selections)
        state = split_state(saved[name]['state'], selections[name]['points'],
            [o.duration if o.endpoint_verified else None for o in observations[name]])
        try:
            field = counted_rebuild(ledger, f'actual-{name}', state)
        except Exception:
            field = None  # Keep the method as failed/all-unknown, never omit it.
        axis, truth = references[name]['axis'], references[name]['truth']
        xx, yy = np.meshgrid(axis, axis)
        before = field_prediction(before_fields[name], xx, yy)
        after = field_prediction(field, xx, yy) if field is not None else np.full(xx.shape, -1, dtype=np.int8)
        outcomes[name] = outcome(truth, before, after)
        write_json(output/f'actual-{name}.json', dict(field=field_payload(field, state) if field is not None else None,
            status='completed' if field is not None else 'failed', outcome=outcomes[name], counts=counts[name],
            unknown_area_fraction=field.unknown.area/(3200**2) if field is not None else 1.))
        save_arrays(output/f'actual-{name}-predictions.npz', before=before, after=after)
        valid = np.isfinite(truth)
        write_csv(output/f'{name}-transition-points.csv', [dict(index=int(i), x=float(xx.ravel()[i]), y=float(yy.ravel()[i]),
            truth=float(truth.ravel()[i]), before=int(before.ravel()[i]), after=int(after.ravel()[i]))
            for i in np.flatnonzero((valid & (before != after)).ravel())])
    all_valid = all(o.duration is not None and o.endpoint_verified for rows in observations.values() for o in rows)
    return dict(status=e2_gate(outcomes, all_valid), checks=checks, outcomes=outcomes, counts=counts, all_probes_valid=all_valid)


def historical_risks(output):
    from tools.connector_sensitivity import OLD, DIAGNOSTICS
    historical = []
    for name in (*DEVELOPMENT, 'coverage-0', 'coverage-1', 'jump-0', 'jump-1'):
        is_old = name in DEVELOPMENT
        path = DIAGNOSTICS/f'D1/{name}-800-baseline.json' if is_old else DIAGNOSTICS/f'D3/D3-{name}-800-baseline.json'
        reference = OLD/f'{name}-evaluation.npz' if is_old else DIAGNOSTICS/f'D3/{name}-evaluation.npz'
        predictions = OLD/f'development/{name}-800-baseline-predictions.npz' if is_old else DIAGNOSTICS/f'D3/D3-{name}-800-baseline-predictions.npz'
        state = read_json(path)['state']
        with np.load(reference) as ref, np.load(predictions) as pred:
            xx, yy = np.meshgrid(ref['axis'], ref['axis'])
            rows = features(state, name)
            historical.append(dict(layout=name, group='historical', **risk_statistics(rows, np.column_stack([xx.ravel(), yy.ravel()]), ref['truth'], pred['prediction'])))
            write_json(output/f'historical-{name}-features.json', rows)
    return historical


async def execute_after_e0(output, ledger, protocol, saved):
    from tools.connector_sensitivity import ConnectorModel, QUOTAS
    references, metrics, risks, all_features = {}, [], {}, {}
    historical = historical_risks(output)
    for name, row in saved.items():
        with np.load(output/f'{name}-reference.npz') as ref:
            references[name] = {k: ref[k].copy() for k in ref.files}
        ref = references[name]
        prediction = saved_prediction(row, ref['axis'])
        metric = dict(layout=name, **score(ref['truth'], prediction), counts=row['counts'], status=row['status'],
            elapsed_seconds=row['elapsed_seconds'], quality=row.get('quality'), stop_reason=row.get('stop_reason'),
            unknown_area_fraction=shape(row['field']['unknown']).area/(3200**2) if row['field'] else 1.)
        metrics.append(metric)
        if row['state'] is None or row['field'] is None or row['status'] != 'completed':
            continue
        xx, yy = np.meshgrid(ref['axis'], ref['axis'])
        all_features[name] = features(row['state'], name)
        risks[name] = risk_statistics(all_features[name], np.column_stack([xx.ravel(), yy.ravel()]), ref['truth'], prediction)
        write_json(output/f'E1-{name}-features.json', all_features[name])
    write_json(output/'historical-risk.json', historical)
    write_csv(output/'historical-risk.csv', historical)
    write_json(output/'E0-metrics.json', metrics)
    write_csv(output/'E0-metrics.csv', metrics)
    if len(risks) != 3:
        summary = dict(status='evidence_insufficient', reason='E0_failed_or_unsupported', E2='not_executed')
    else:
        gate = risk_gate(risks)
        write_json(output/'E1-gate.json', dict(layouts=risks, **gate))
        if not gate['passed']:
            summary = dict(status='mechanism_not_supported' if gate['sufficient'] else 'evidence_insufficient',
                           reason='E1_fixed_trigger_gate', gate=gate, E2='not_executed')
        else:
            selections = {name: select_probes(saved[name]['state'], name, quota) for name, quota in QUOTAS.items()}
            write_json(output/'probe-selection.json', selections)
            write_json(output/'probe-selection.sha256.json', dict(sha256=sha(output/'probe-selection.json')))
            if not all(row['complete'] for row in selections.values()):
                summary = dict(status='evidence_insufficient', reason='probe_quota_shortage', E2='not_executed')
            else:
                models = {d['id']: ConnectorModel(d['kind'], d['angle'], d['scale']) for d in protocol['definitions']}
                summary = await run_e2(output, ledger, saved, references, selections, models)
    summary.update(E0=metrics, sampling_attempts=sum(ledger.data['used'].values()),
        extra_reconstructions=len(ledger.data.get('reconstructions', {})), live_requests=0, sealed_predictions=0)
    write_json(output/'summary.json', summary)


def write_report_data(output):
    """Only saved artifacts; safe to finish reporting without sampling/rebuilding."""
    summary = read_json(output/'summary.json')
    lines = ['# E0–E2 机器结果摘要', '', f"状态：{summary['status']}；新增采样：{summary['sampling_attempts']}；百度：0。", '',
        '| 布局 | 请求 | TP | FP | FN_known | FN_unknown | 严重 FP |', '| --- | ---: | ---: | ---: | ---: | ---: | ---: |']
    for row in summary.get('E0', []):
        if 'counts' in row:
            lines.append(f"| {row['layout']} | {row['counts']['confirmed_calls']} | {row['TP']} | {row['FP']} | {row['FN_known']} | {row['FN_unknown']} | {row['severe_fp']} |")
    (output/'summary.md').write_text('\n'.join(lines)+'\n', encoding='utf-8')
