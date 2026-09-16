"""E1 offline evidence layer; never mutates requests or produces reachable area."""
import math
import re
from collections import Counter, defaultdict
from datetime import datetime

from life_circle.coordinates import LocalProjection, normalize


class ExperimentCancelled(Exception):
    pass


def finite(value):
    return type(value) in (int, float) and math.isfinite(value)


def coordinate(value):
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if not all(finite(v) for v in value) or not (-180 <= value[0] <= 180 and -85 < value[1] < 85):
        return None
    return list(value)


def record(event, source, center, extent):
    identity = str(event.get('id', ''))
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', identity):
        raise ValueError('Invalid event identity')
    result = dict(id=f'{source}:{identity}', issues=[])
    issues = result['issues']
    for name, key in (('request_origin', 'origin'), ('request_destination', 'destination'),
                      ('actual_origin', 'route_origin'), ('actual_destination', 'route_destination')):
        value = coordinate(event.get(key))
        result[name] = value
        if value is None:
            issues.append(('missing_' if event.get(key) is None else 'invalid_') + name)
    duration = event.get('duration')
    result['duration'] = duration if finite(duration) and duration >= 0 else None
    if result['duration'] is None:
        issues.append('invalid_duration')
    result['endpoint_verified'] = event.get('endpoint_verified') is True
    if not result['endpoint_verified']:
        issues.append('endpoints_unverified')
    if event.get('outcome') != 'success':
        # Only typed outcome categories survive; do not copy arbitrary messages.
        reason = event.get('outcome')
        result['upstream_reason'] = reason if reason in (
            'endpoint_offset', 'timeout', 'rate_limit', 'permission', 'quota', 'no_result',
            'invalid_parameter', 'network_error', 'cancelled_in_flight') else 'other'
        issues.append('upstream_rejected')
    else:
        result['upstream_reason'] = None
    result['recorded_at'] = None
    value = event.get('reservedAt')
    if isinstance(value, str) and len(value) <= 40:
        try:
            result['recorded_at'] = datetime.fromisoformat(value).isoformat()
        except ValueError:
            pass
    result['timestamp_basis'] = 'event_reservation'
    result['candidate_endpoints'] = []
    for candidate in event.get('route_endpoints', []) if isinstance(event.get('route_endpoints', []), list) else []:
        if isinstance(candidate, dict):
            start, end = coordinate(candidate.get('start')), coordinate(candidate.get('end'))
            if start is not None or end is not None:
                result['candidate_endpoints'].append(dict(start=start, end=end))
    projection = LocalProjection(center)
    for side in ('origin', 'destination'):
        requested, actual = result['request_' + side], result['actual_' + side]
        shift = math.dist(projection.to_local(requested), projection.to_local(actual)) if requested is not None and actual is not None else None
        result[side + '_offset_m'] = shift
        if shift is not None and shift > 50:
            issues.append('endpoint_offset')
    if result['actual_destination'] is not None and any(abs(v) > extent for v in projection.to_local(result['actual_destination'])):
        issues.append('outside_extent')
    result['issues'] = sorted(set(issues))
    result['usable'] = not result['issues']
    result['request_key'] = [list(normalize(result[k])) for k in ('request_origin', 'request_destination')] if all(result[k] is not None for k in ('request_origin', 'request_destination')) else None
    result['request_pair_exactly_supported'] = result['usable'] and all(result['request_' + side] == result['actual_' + side] for side in ('origin', 'destination'))
    return result


def build_layer(events, source, center, extent, *, cancelled=lambda: False):
    if not re.fullmatch(r'[a-zA-Z0-9_-]{1,100}', source) or coordinate(center) is None or not finite(extent) or extent <= 0:
        raise ValueError('Invalid layer configuration')
    records, seen = [], set()
    for event in events:
        if cancelled():
            raise ExperimentCancelled('Cancelled; partial layer discarded')
        item = record(event, source, center, extent)
        if item['id'] in seen:
            raise ValueError('Duplicate event identity')
        seen.add(item['id'])
        records.append(item)
    records.sort(key=lambda r: r['id'])
    groups = defaultdict(list)
    for item in records:
        if item['usable']:
            key = tuple(tuple(item[k]) for k in ('request_origin', 'actual_origin', 'actual_destination'))
            groups[key].append(item)
    points = []
    for key, members in sorted(groups.items()):
        durations = sorted(set(r['duration'] for r in members))
        points.append(dict(request_origin=list(key[0]), actual_origin=list(key[1]), actual_destination=list(key[2]),
            record_ids=[r['id'] for r in members], durations=durations, conflict=len(durations) > 1,
            crosses_threshold=min(durations) <= 900 < max(durations),
            duration=durations[0] if len(durations) == 1 else None))
    if cancelled():
        raise ExperimentCancelled('Cancelled; completed layer discarded')
    return dict(schema='endpoint-observations-e1', source=source, business_center=list(center),
        records=records, points=points, summary=dict(records=len(records), usable_records=sum(r['usable'] for r in records),
            rejected_records=sum(not r['usable'] for r in records), spatial_points=len(points),
            origin_groups=len({(tuple(p['request_origin']), tuple(p['actual_origin'])) for p in points}),
            conflicted_points=sum(p['conflict'] for p in points), threshold_conflicts=sum(p['crosses_threshold'] for p in points),
            consensus_points=sum(not p['conflict'] for p in points),
            duplicate_excess=sum(len(p['record_ids']) - 1 for p in points),
            exact_request_pairs=sum(r['request_pair_exactly_supported'] for r in records),
            issue_counts=dict(sorted(Counter(i for r in records for i in r['issues']).items()))))


def diagnose_triangle(records, center):
    if len(records) != 3 or any(not r['usable'] for r in records):
        return dict(status='invalid_evidence')
    if len({(tuple(r['request_origin']), tuple(r['actual_origin'])) for r in records}) != 1:
        return dict(status='mixed_origins')
    projection = LocalProjection(center)
    def signed_area(key):
        a,b,c = [projection.to_local(r[key]) for r in records]
        return (b[0]-a[0])*(c[1]-a[1]) - (b[1]-a[1])*(c[0]-a[0])
    before, after = signed_area('request_destination'), signed_area('actual_destination')
    return dict(status='degenerate' if min(abs(before), abs(after)) <= 1e-8 else 'flipped' if before*after < 0 else 'regular',
        request_signed_double_area_m2=before, actual_signed_double_area_m2=after,
        support_generated=False, obstacle_connectivity_verified=False)
