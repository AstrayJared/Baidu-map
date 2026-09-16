"""Offline joint task: cold per-run evidence, physical attempt ledger, strict POI mapping."""
from dataclasses import asdict, dataclass, replace
from collections import Counter
import math
import time

from shapely.geometry import Point, Polygon, shape

from life_circle.coordinates import LocalProjection, normalize
from life_circle.field import GeometryError
from life_circle.models import CancelToken, IsochroneRequest
from tools.endpoint_boundary_band import connect_estimate
from tools.endpoint_geometry import business_geometry
from tools.endpoint_multicross_boundary import compute_multicross_boundary, close_patch_evidence, mixed_edges
from tools.endpoint_radial_experiment import ORIGIN, local


@dataclass(frozen=True)
class EvidenceContext:
    origin: tuple
    provider: tuple
    run_id: str
    coordinate_system: str = 'bd09ll'
    route_metric: str = 'duration'
    provider_options: tuple = ('walking', 'default-routes')
    actual_origin_policy: str = 'must-match-request-origin'


def evidence_key(context, destination, uid=None):
    return (context, normalize(destination), uid)


def same_endpoint(a, b):
    if a is None or b is None:
        return False
    try:
        return math.dist(LocalProjection(tuple(b)).to_local(a), (0, 0)) <= 1e-5
    except (TypeError, ValueError):
        return False


def confirmed_poi(observation, origin, destination):
    pending = dict(status='pending', seconds=None, reason='missing_observation')
    if observation is None:
        return pending
    if (not observation.endpoint_verified or observation.observed_duration is None
            or observation.reason not in (None, 'endpoint_offset')):
        return dict(pending, reason=observation.reason or 'invalid_evidence')
    if (not same_endpoint(observation.request_origin, origin)
            or not same_endpoint(observation.route_origin, origin)
            or not same_endpoint(observation.destination, destination)
            or not same_endpoint(observation.route_destination, destination)):
        return dict(pending, reason='endpoint_mapping_unconfirmed')
    return dict(status='verified_reachable' if observation.observed_duration <= 900
                else 'verified_unreachable', seconds=observation.observed_duration, reason=None)


def import_poi(session, entry, context):
    """Import actual spatial evidence, without inventing bracket shrink or free requests."""
    obs = entry['observation']
    if (entry['context'] != context or entry['uid'] is not None
            or not same_endpoint(obs.request_origin, context.origin)
            or not same_endpoint(obs.route_origin, context.origin)
            or obs.route_destination is None
            or not session.domain.covers(Point(session.projection.to_local(obs.route_destination)))):
        return False
    key = normalize(obs.destination)
    if key in session._requests:
        return False
    record, reason = session.ingest(obs, 'poi_shared')
    if record is None:
        return False
    session._requests[key] = (record, reason)
    session.scheduler.cache[key] = obs
    return True


async def run_task(case, provider, pois, demand, *, circle_budget, poi_budget, shared,
                   token=None, run_id='offline-task', poi_policy='critical'):
    if provider.network:
        raise ValueError('POI research accepts offline providers only')
    if circle_budget < 0 or poi_budget < 0:
        raise ValueError('Negative budget')
    if poi_policy not in ('critical', 'early', 'guided', 'discover') or (not shared and poi_policy != 'critical'):
        raise ValueError('Invalid POI guidance policy')
    token = token or CancelToken()
    started = time.perf_counter()
    context = EvidenceContext(ORIGIN, (*provider.identity, case), run_id)
    circle_cache, poi_cache, ledger = {}, {}, []
    counts = dict(circle_calls=0, poi_calls=0, retries=0)
    sharing = dict(circle_to_poi=0, poi_imported=0, late_poi_imported=0,
                   claimed_bracket_shrink_from_import=0)
    state = {p['id']: dict(status='pending', seconds=None, reason='not_queried') for p in pois}
    entries, first_coarse, critical_ready = {}, None, None
    retained_session = None
    by_id = {p['id']: p for p in pois}
    if len(by_id) != len(pois) or any(i not in by_id for i in demand):
        raise ValueError('Invalid POI task catalog')
    critical = [p['id'] for p in pois if p['critical']]

    async def transport(destination, purpose, deadline, attempt=1):
        if token.cancelled:
            return None
        counts[purpose+'_calls'] += 1
        event = dict(id=len(ledger)+1, purpose=purpose, uses=[purpose], attempt=attempt,
                     destination=list(destination))
        ledger.append(event)
        obs = await provider.query_walking_time(ORIGIN, destination, deadline)
        obs = replace(obs, attempts=attempt, request_origin=ORIGIN)
        event['observation'] = asdict(obs)
        entry = dict(context=context, uid=None, observation=obs, event=event)
        if obs.observed_duration is not None and obs.reason in (None, 'endpoint_offset'):
            cache = circle_cache if purpose == 'circle' else poi_cache
            cache[evidence_key(context, destination)] = entry
        return entry

    class CircleProvider:
        network = False
        identity = ('e83-joint', case)

        async def query_walking_time(self, origin, destination, deadline):
            if tuple(origin) != ORIGIN:
                raise ValueError('Unexpected circle origin')
            entry = await transport(destination, 'circle', deadline)
            return entry['observation']

    async def verify(identity):
        nonlocal critical_ready
        poi = by_id[identity]
        key = evidence_key(context, poi['coordinate'], poi.get('uid'))
        if poi.get('uid') is not None:
            raise ValueError('Synthetic workload has no UID provider; do not emulate UID requests')
        entry = poi_cache.get(key)
        if entry is None and shared:
            entry = circle_cache.get(key)
            if entry is not None:
                sharing['circle_to_poi'] += 1
                poi_cache[key] = entry
        if entry is None:
            for attempt in (1, 2):
                if counts['poi_calls'] >= poi_budget or token.cancelled:
                    break
                counts['retries'] += int(attempt > 1)
                entry = await transport(normalize(poi['coordinate']), 'poi', float('inf'), attempt)
                if entry['observation'].reason not in ('timeout', 'temporary', 'rate_limit'):
                    break
        if entry is not None:
            if 'poi' not in entry['event']['uses']:
                entry['event']['uses'].append('poi')
            entries[identity] = entry
        state[identity] = confirmed_poi(entry['observation'] if entry else None, ORIGIN, poi['coordinate'])
        if critical_ready is None and all(state[i]['status'].startswith('verified_') for i in critical):
            critical_ready = dict(total_calls=len(ledger), elapsed_s=time.perf_counter()-started)
        return entry

    async def before_local(session, rows):
        nonlocal first_coarse, retained_session
        retained_session = session
        # A diagnostic coarse preview is accepted only if valid and negative-safe.
        if session.origin:
            connection = connect_estimate(rows, (0, 0), session.domain, session._conflicts,
                                          witnesses=session.records)
            envelope = connection.get('envelope')
            if envelope is not None and not envelope.is_empty:
                try:
                    geometry = business_geometry(envelope, session.projection)
                    negative = [r for r in session.records if r['duration'] > 900]
                    if not any(shape(geometry).covers(Point(session.projection.to_geographic(r['xy'])))
                               for r in negative):
                        first_coarse = dict(total_calls=len(ledger), elapsed_s=time.perf_counter()-started)
                except GeometryError:
                    pass  # No display timestamp; the main algorithm still validates its own output.
        if shared:
            early = critical if poi_policy in ('critical', 'discover') else list(dict.fromkeys([*critical, *demand]))
            for identity in early:
                entry = await verify(identity)
                if entry and import_poi(session, entry, context):
                    sharing['poi_imported'] += 1
                    if 'circle' not in entry['event']['uses']:
                        entry['event']['uses'].append('circle')
            if poi_policy == 'discover':
                # Verify the fixed task now, but preserve the old local mesh unless
                # a positive actual endpoint reveals a gap beyond the 25 m target.
                connection = connect_estimate(rows, (0, 0), session.domain, session._conflicts,
                                              witnesses=session.records)
                candidate = connection.get('candidate')
                for identity in dict.fromkeys(demand):
                    entry = await verify(identity)
                    obs = entry['observation'] if entry else None
                    if (candidate is not None and obs and obs.observed_duration is not None
                            and obs.observed_duration <= 900 and obs.route_destination is not None
                            and candidate.distance(Point(session.projection.to_local(obs.route_destination))) > 25
                            and import_poi(session, entry, context)):
                        sharing['poi_imported'] += 1
                        if 'circle' not in entry['event']['uses']:
                            entry['event']['uses'].append('circle')

    circle, circle_error = None, None
    if circle_budget:
        request = IsochroneRequest(ORIGIN, 'bd09ll', budget=circle_budget, extent=1200,
                                  max_extent=1200, expand=False, concurrency=30)
        try:
            circle = await compute_multicross_boundary(request, CircleProvider(), token,
                radial_step=100, target=25, parallel_sampling=True, edge_batch_size=30,
                on_local_start=before_local, poi_guided=poi_policy == 'guided',
                poi_discovery_only=poi_policy == 'discover')
        except GeometryError as error:
            circle_error = str(error)
        if circle is not None:
            assert circle['calls'] == counts['circle_calls']
            if first_coarse is None and circle.get('geometry') and shape(circle['geometry']).is_valid:
                first_coarse = dict(total_calls=len(ledger), elapsed_s=time.perf_counter()-started)
    # The fixed task includes circle-exterior POIs. Prediction never filters this list.
    for identity in demand:
        if token.cancelled:
            break
        await verify(identity)
    if shared and circle is not None and retained_session is not None and not token.cancelled:
        session = retained_session
        for entry in entries.values():
            if import_poi(session, entry, context):
                sharing['late_poi_imported'] += 1
                if 'circle' not in entry['event']['uses']:
                    entry['event']['uses'].append('circle')
        if sharing['late_poi_imported'] and circle.get('candidateGeometry'):
            patch_geo = circle.get('localRepair', {}).get('patchGeometry')
            patch = local(patch_geo) if patch_geo else Polygon()
            blocked = list(session._conflicts) + [session.projection.to_local(e['destination'])
                                                  for e in session.log if not e['accepted']]
            conn = close_patch_evidence(session.records, patch, local(circle['candidateGeometry']),
                                        blocked, target=25, domain=session.domain)
            try:
                circle['geometry'] = (business_geometry(conn['estimate'], session.projection)
                                      if not conn['conflicts'] else None)
                circle['candidateGeometry'] = business_geometry(conn['combined_candidate'], session.projection)
                circle['unknownRegion'] = business_geometry(conn['unknown'], session.projection)
                circle['prePoiCompletion'] = circle['completion']
                circle['completion'] = dict(scope='observed_local_boundary_only', resolutionReached=False,
                    reason='poi_evidence_reconnected_without_extra_refinement',
                    unresolvedEdges=len(mixed_edges(session.records, conn['patch'], 25)),
                    budgetExhausted=counts['circle_calls'] >= circle_budget)
                circle['poiReconnection'] = dict(conflicts=conn['conflicts'], extra_calls=0,
                                                 unknown_m2=conn['unknown'].area)
            except GeometryError as error:
                circle_error = str(error)
                circle['geometry'] = None
        circle['observationEvidence'] = session.log
        circle['phases'] = dict(Counter(row['kind'] for row in session.log))
        candidate = shape(circle['candidateGeometry']) if circle.get('candidateGeometry') else Polygon()
        circle['negativeEvidence'] = [dict(id=r['id'], coordinate=list(session.projection.to_geographic(r['xy'])),
            coordinateLocal=r['xy'], durationSeconds=r['duration'], kind='over_threshold',
            physicalBarrierVerified=False,
            insideEstimate=candidate.covers(Point(session.projection.to_geographic(r['xy']))))
            for r in session.records if r['duration'] > 900]
    counts.update(total_calls=len(ledger), retrieval_calls=0, reference_calls=0)
    assert counts['circle_calls'] <= circle_budget and counts['poi_calls'] <= poi_budget
    assert len(ledger) == provider.calls == counts['circle_calls'] + counts['poi_calls']
    return dict(circle=circle, circle_error=circle_error, poi=state, cost=counts, sharing=sharing,
                first_coarse=first_coarse, critical_ready=critical_ready,
                elapsed_s=time.perf_counter()-started, ledger=ledger)
