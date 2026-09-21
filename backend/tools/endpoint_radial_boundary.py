"""E8 research: estimate an outer envelope; deliberately do not verify its interior."""
import asyncio
from collections import Counter
from dataclasses import asdict
import math

from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from tools.endpoint_geometry import business_geometry
from life_circle.scheduler import Scheduler
from life_circle.models import ProgressSnapshot
from tools.endpoint_boundary import BoundarySession
from tools.endpoint_boundary_surface import add_origin_condition

TAU = 2*math.pi


async def compute_radial_boundary(request, provider, token, *, directions=8,
                                  adaptive=False, max_directions=64,
                                  target=25, chord_target=100, boundary_bands=False,
                                  side_probe_limit=4, on_sampling_complete=None,
                                  on_session_start=None, coverage_first=False, on_progress=None):
    if directions < 4 or max_directions < directions or target <= 0 or chord_target <= 0:
        raise ValueError('Invalid direction configuration')
    scheduler = Scheduler(request, provider, token)
    stage = 'initializing'
    def report(next_stage=None):
        nonlocal stage
        if next_stage:
            stage = next_stage
        if on_progress:
            on_progress(ProgressSnapshot(stage, scheduler.stats.requests, scheduler.stats.network_requests,
                request.budget, max(0, scheduler.clock.time() - scheduler.started)))
    scheduler.on_progress = report
    report()
    domain = box(-request.extent, -request.extent, request.extent, request.extent)
    session = BoundarySession(scheduler, domain)
    rays = []
    if boundary_bands:
        from tools.endpoint_boundary_band import probe_sides, next_direction, connect_estimate
        if not 0<=side_probe_limit<=4:raise ValueError('Side probe limit must be between 0 and 4')

    def stopped():
        return scheduler._stopped() or scheduler.remaining <= 0

    async def search(angle, hint=300, coarse=False):
        ux, uy = math.cos(angle), math.sin(angle)
        # Keep the requested endpoint inside the domain after six-decimal rounding.
        cap = max(0, request.extent-.1)/max(abs(ux), abs(uy))
        radius = min(max(hint, 25), cap)
        inside = None
        row = dict(angle=angle, status='unknown', reason=None, bracket=None)
        for _ in range(24):
            if stopped():
                row['reason'] = scheduler.stop_reason or 'budget'
                return row
            record, error = await session.measure((radius*ux, radius*uy), 'direction_search')
            anchor = add_origin_condition(session)
            if record is None:
                row['reason'] = error
                return row
            if inside is None:
                inside = anchor
            if not domain.covers(Point(record['xy'])):
                row['reason'] = 'actual_endpoint_outside_domain'
                return row
            if record['duration'] > 900:
                if inside is None:
                    row['reason'] = 'missing_inside_anchor'
                    return row
                width = math.dist(inside['xy'], record['xy'])
                bracket = await session.refine(inside, record,
                    target=max(target, width+1e-6) if coarse else target)
                if coarse and width > target:
                    bracket.update(status='unfinished', reason='coarse_bracket')
                if boundary_bands and not coarse:
                    bracket, row['sideProbes'] = await probe_sides(session,bracket,target=target,limit=side_probe_limit)
                row.update(status=bracket['status'], reason=bracket['reason'], bracket=bracket)
                return row
            # No correction queries for a reachable internal endpoint.
            inside = record
            if radius >= cap-1e-7:
                row.update(reason='range_limit', status='truncated')
                return row
            radius = min(cap, radius*1.6)
        row['reason'] = 'search_limit'
        return row

    try:
        report('exploring')
        if on_session_start is not None and not token.cancelled:
            await on_session_start(session)
        for i in range(directions):
            row=await search(TAU*i/directions, coarse=coverage_first)
            if boundary_bands:row['committed']=True
            rays.append(row)
        if coverage_first:
            # Establish all coarse opposite pairs before spending on fine detail.
            for _ in range(16):
                active = [r for r in rays if r.get('bracket') and r['bracket']['reason']
                          in ('coarse_bracket', 'iteration_limit')]
                if not active or stopped():
                    break
                for row in active:
                    if stopped():
                        break
                    old = row['bracket']
                    bracket = await session.refine(old['left'], old['right'], target=target, max_rounds=1)
                    fine = bracket
                    if boundary_bands and bracket['reason'] == 'offset_stagnation':
                        bracket, row['sideProbes'] = await probe_sides(session, bracket,
                            target=target, limit=side_probe_limit)
                    history = old['history'] + (fine['history'] if bracket is not fine else []) + bracket['history']
                    bracket = dict(bracket, initial=old['initial'], history=history,
                        iterations=sum(step['accepted'] for step in history))
                    bracket['suspected_jump'] = bool(bracket['status'] == 'localized'
                        and bracket['iterations'] >= 3
                        and abs(bracket['left']['duration']-bracket['right']['duration']) > 120)
                    row.update(status=bracket['status'], reason=bracket['reason'], bracket=bracket)
        while adaptive and len(rays) < max_directions and not stopped():
            if boundary_bands:
                anchor=add_origin_condition(session)
                origin=anchor['xy'] if anchor else (0,0)
                proposal=next_direction(rays,origin,domain,session._conflicts,
                                        [r['angle'] for r in rays],chord_target)
                if proposal is None:break
                angle,hint=proposal
                row=await search(angle,hint)
                row['committed']=row['status']=='localized'
                rays.append(row)
                continue
            ordered = sorted(rays, key=lambda r:r['angle'])
            gaps = []
            for i, a in enumerate(ordered):
                b = ordered[(i+1) % len(ordered)]
                gap = (b['angle']-a['angle']) % TAU
                if a['status'] == b['status'] == 'localized':
                    left = a['bracket']['reachable_endpoint']['xy']
                    right = b['bracket']['reachable_endpoint']['xy']
                    score = math.dist(left, right)
                    hint = (math.hypot(*left)+math.hypot(*right))/2
                else:
                    score, hint = request.extent*gap, 300
                gaps.append((score, -a['angle'], (a['angle']+gap/2) % TAU, hint))
            score, _, angle, hint = max(gaps)
            if score <= chord_target:
                break
            rays.append(await search(angle, hint))
        rays.sort(key=lambda r:r['angle'])
        if on_sampling_complete is not None and not token.cancelled:
            report('refining')
            await on_sampling_complete(session,rays)
        report('reconstructing')
        anchor = add_origin_condition(session)
        origin = anchor['xy'] if anchor else (0, 0)
        faces, covered = [], 0
        for i, a in enumerate(rays):
            b = rays[(i+1) % len(rays)]
            if a['status'] != 'localized' or b['status'] != 'localized':
                continue
            left, right = a['bracket']['reachable_endpoint'], b['bracket']['reachable_endpoint']
            if any(tuple(p['xy']) in session._conflicts for p in (left, right)):
                continue
            gap = (b['angle']-a['angle']) % TAU
            angles = [math.atan2(p['xy'][1]-origin[1], p['xy'][0]-origin[0]) for p in (left, right)]
            actual_gap = (angles[1]-angles[0]) % TAU
            if not 0 < actual_gap < min(math.pi, 1.5*gap):
                continue
            triangle = Polygon([origin, left['xy'], right['xy']])
            if triangle.is_valid and triangle.area > 0:
                faces.append(triangle.intersection(domain)); covered += gap
        envelope = unary_union(faces) if faces else None
        cancelled = token.cancelled
        if boundary_bands:
            connected=await asyncio.to_thread(connect_estimate,rays,origin,domain,session._conflicts,target=target,
                                       chord_target=chord_target,witnesses=session.records)
            envelope=connected['envelope']
        result = dict(status='cancelled' if cancelled else 'completed',
            algorithm='outer-envelope-radial-e8', coordinateSystem='bd09ll',
            center=list(request.origin), calls=scheduler.stats.requests,
            phases=dict(Counter(e['kind'] for e in session.log)),
            quality='experimental_outer_envelope',
            assumption='interior islands ignored; approximately one radial crossing',
            geometry=business_geometry(envelope, session.projection) if envelope is not None and not cancelled else None,
            directions=rays, uncoveredAngleFraction=max(0, 1-covered/TAU),
            truncated=any(r['status']=='truncated' or r['reason']=='actual_endpoint_outside_domain' for r in rays),
            stopReason=scheduler.stop_reason or ('budget' if scheduler.remaining==0 else 'direction_limit_or_resolution'),
            originCondition=anchor, _observations=session.observations, _evidence=session.log)
        if boundary_bands:
            negatives=[]
            for evidence in connected['negative_evidence']:
                evidence=dict(evidence)
                evidence['coordinate']=list(session.projection.to_geographic(evidence.pop('xy')))
                negatives.append(evidence)
            jumps=[]
            for evidence in connected['jump_evidence']:
                evidence=dict(evidence)
                evidence['from']=list(session.projection.to_geographic(evidence.pop('start_xy')))
                evidence['to']=list(session.projection.to_geographic(evidence.pop('end_xy')))
                jumps.append(evidence)
            segments=[]
            if not cancelled:
                for segment in connected['segments']:
                    segment=dict(segment)
                    segment['from']=list(session.projection.to_geographic(segment.pop('start_xy')))
                    segment['to']=list(session.projection.to_geographic(segment.pop('end_xy')))
                    segments.append(segment)
            result.update(algorithm='outer-envelope-boundary-band-e81',
                negativeEvidence=negatives,jumpEvidence=jumps,
                observationEvidence=session.log,
                candidateGeometry=business_geometry(connected['candidate'],session.projection)
                    if connected['candidate'] is not None and not cancelled else None,
                geometryMeaning='experimental estimate; not confirmed reachable interior',
                uncertaintyBand=business_geometry(connected['band'],session.projection)
                    if connected['band'] is not None and not cancelled else None,
                uncertainty=dict(guaranteedCoverage=False,segments=segments,
                    meaning='estimated boundary strip, not proof of continuous boundary containment',
                    closedEstimate=connected['closed'] and not cancelled,
                    reason='cancelled' if cancelled else connected['reason'],
                    maxAngularGapRadians=connected['max_gap_radians']),
                uncoveredAngleFraction=None)
            if connected['reason']=='known_negative_inside_estimate' and not cancelled:
                result['quality']='experimental_evidence_conflict'
        scheduler.stats.total_seconds = max(0, scheduler.clock.time() - scheduler.started)
        result['_statistics'] = asdict(scheduler.stats)
        report()
        return result
    finally:
        scheduler.close()
