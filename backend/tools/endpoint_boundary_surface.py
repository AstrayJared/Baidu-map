"""E6.1 conservative two-dimensional connection of actual endpoint evidence."""
import asyncio
from dataclasses import asdict
import math
import random

from shapely.geometry import Point, Polygon, box
from shapely.ops import unary_union

from life_circle.field import business_geometry, multipolygon
from life_circle.mesh import Mesh
from life_circle.scheduler import Scheduler
from tools.endpoint_boundary import BoundarySession
from tools.endpoint_preserved_triangulation import preserved_network


def face_key(ids):
    return ':'.join(sorted(ids))


def connect_regions(network, domain, centers, witnesses=(), blocked_points=()):
    inside, outside = [], []
    nodes = network['nodes']
    blocked = [Point(p) for p in blocked_points]
    evidence = [(Point(r['xy']), r['duration'] <= 900) for r in witnesses]
    counts = dict(inside=0, outside=0, unknown=0)
    for ids in network['all_triangles']:
        polygon = Polygon([nodes[i]['actual'] for i in ids])
        center = centers.get(face_key(ids))
        labels = {nodes[i]['duration'] <= 900 for i in ids}
        valid = (len(labels) == 1 and center is not None
                 and polygon.contains(Point(center['xy']))
                 and (center['duration'] <= 900) in labels)
        if valid:
            label = next(iter(labels))
            valid = not any(polygon.covers(p) for p in blocked)
            valid = valid and not any(value != label and polygon.covers(p) for p, value in evidence)
        if not valid:
            counts['unknown'] += 1
            continue
        (inside if label else outside).append(polygon.intersection(domain))
        counts['inside' if label else 'outside'] += 1
    reachable = multipolygon(unary_union(inside))
    unreachable = multipolygon(unary_union(outside))
    unknown = multipolygon(domain.difference(unary_union([reachable, unreachable])))
    return dict(reachable=reachable, unreachable=unreachable, unknown=unknown, faces=counts)


def add_origin_condition(session):
    if session.origin is None:
        return None
    xy = tuple(session.projection.to_local(session.origin[1]))
    prior = session._points.get(xy)
    if prior is not None:
        # Never replace a measured nonzero value with a synthetic zero.
        if prior['duration'] != 0:
            session._conflicts.add(xy)
            return None
        return prior
    record = dict(id='origin-condition', xy=list(xy), duration=0,
                  origin=session.origin, request_xy=list(xy), source_kind='model_origin_condition')
    session._points[xy] = record
    return record


async def compute_boundary_surface(request, provider, cancel_token, on_progress=None):
    if request.threshold != 900:
        raise ValueError('E6.1 requires a 900-second threshold')
    scheduler = Scheduler(request, provider, cancel_token)
    domain = box(-request.extent, -request.extent, request.extent, request.extent)
    session = BoundarySession(scheduler, domain)
    phases = {}; brackets = []; centers = {}; attempted = set()

    def stopped():
        return scheduler._stopped() or scheduler.remaining <= 0

    def progress(phase):
        if on_progress:
            on_progress(dict(phase=phase, requests=scheduler.stats.requests,
                             elapsed=scheduler.clock.time()-scheduler.started))

    def room(limit, reserve=1):
        return not stopped() and scheduler.stats.requests + reserve <= limit

    async def measure(xy, kind):
        result = await session.measure(xy, kind)
        progress(kind)
        return result

    try:
        # Reserving worst-case retries keeps each phase within its allocation.
        sampling_limit = int(request.budget * .6)
        explore_calls = int(request.budget * request.exploration_fraction)
        initial_limit = max(0, sampling_limit-explore_calls)
        for xy in Mesh(request.extent, request.coarse_size).required_points():
            if xy == (0, 0):
                continue
            if not room(initial_limit, request.max_attempts):
                break
            await measure(xy, 'initial')
            add_origin_condition(session)
        phases['initial'] = scheduler.stats.requests
        rng = random.Random(request.seed)
        exploration_limit = min(sampling_limit, scheduler.stats.requests+explore_calls)
        while room(exploration_limit, request.max_attempts):
            await measure((rng.uniform(-request.extent, request.extent),
                           rng.uniform(-request.extent, request.extent)), 'exploration')
        phases['exploration'] = scheduler.stats.requests-phases['initial']
        before = scheduler.stats.requests
        for _ in range(32):
            if not room(sampling_limit, 2*request.max_attempts):
                break
            edges = await asyncio.to_thread(session.candidate_edges)
            batch = [(a,b) for a,b in edges if math.dist(a['xy'], b['xy']) > .5
                     and tuple(sorted((a['id'], b['id']))) not in attempted][:12]
            if not batch:
                break
            for a,b in batch:
                if not room(sampling_limit, 2*request.max_attempts):
                    break
                attempted.add(tuple(sorted((a['id'], b['id']))))
                brackets.append(await session.refine(a,b,max_rounds=1))
                progress('boundary_refinement')
        phases['boundary'] = scheduler.stats.requests-before
        network = await asyncio.to_thread(preserved_network, session.records, domain, radius=None)
        if network['status'].startswith('invalid_'):
            raise ValueError('Invalid final actual-endpoint mesh')
        nodes = network['nodes']
        faces = []
        for ids in network['all_triangles']:
            labels = {nodes[i]['duration'] <= 900 for i in ids}
            if len(labels) != 1:
                continue
            polygon = Polygon([nodes[i]['actual'] for i in ids])
            xy = list(polygon.centroid.coords)[0]
            if domain.covers(Point(xy)):
                faces.append((not next(iter(labels)), -polygon.area, face_key(ids), xy))
        before = scheduler.stats.requests
        for _, _, key, xy in sorted(faces):
            if not room(request.budget):
                break
            record, _ = await measure(xy, 'interior_check')
            if record is not None:
                centers[key] = record
        phases['interior_check'] = scheduler.stats.requests-before
        blocked = list(session._conflicts)
        blocked.extend(session.projection.to_local(r['destination']) for r in session.log if not r['accepted'])
        connected = await asyncio.to_thread(connect_regions, network, domain, centers,
                                            session.records, blocked)
        if cancel_token.cancelled:
            geometry = None
            quality = 'insufficient'
        else:
            geometry = business_geometry(connected['reachable'], session.projection)
            quality = 'partial' if connected['unknown'].area > 1e-6 else 'complete'
            if not centers:
                geometry = None
                quality = 'insufficient'
        elapsed = scheduler.clock.time()-scheduler.started
        scheduler.stats.total_seconds = elapsed
        scheduler.stats.unknown_area = connected['unknown'].area
        scheduler.stats.active_points = len(session.records)
        scheduler.stats.exploration_requests = phases['exploration']
        scheduler.stats.unfinished_boundary = sum(
            len({nodes[i]['duration'] <= 900 for i in ids}) > 1 for ids in network['all_triangles'])
        return dict(status='cancelled' if cancel_token.cancelled else 'completed',
            coordinateSystem='bd09ll', center=list(request.origin), thresholdSeconds=900,
            dataSource='baidu_walking' if provider.network else 'synthetic',
            geometry=geometry, unknownRegion=business_geometry(connected['unknown'],session.projection),
            uncertainRegion=business_geometry(connected['unknown'],session.projection),
            unreachableRegion=business_geometry(connected['unreachable'],session.projection),
            calculationExtent=business_geometry(domain,session.projection), quality=quality,
            stopReason=scheduler.stop_reason or ('budget' if scheduler.remaining==0 else 'sampling_complete'),
            statistics=asdict(scheduler.stats), elapsedSeconds=elapsed,
            boundaryModel=dict(algorithm='actual-endpoint-boundary-surface-e61', phases=phases,
                faces=connected['faces'], actualPoints=len(session.records), centerChecks=len(centers),
                reachableAreaM2=connected['reachable'].area, unknownAreaM2=connected['unknown'].area,
                unknownFraction=connected['unknown'].area/domain.area,
                components=len(connected['reachable'].geoms),
                holes=sum(len(p.interiors) for p in connected['reachable'].geoms),
                originCondition=next((r for r in session.records if r.get('source_kind')),None),
                truncated=connected['reachable'].distance(domain.boundary)<1e-7,
                unfinishedBoundary=True, assumption='homogeneous vertices plus actual interior witness',
                linearFill=False),
            _observations=session.observations, _evidence=session.log, _brackets=brackets)
    finally:
        scheduler.close()
