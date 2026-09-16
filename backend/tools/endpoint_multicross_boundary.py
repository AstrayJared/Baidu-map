"""E8.2 offline research: local multi-crossing evidence and partial patch connection."""
import math

from shapely.geometry import Point, Polygon, MultiPoint, box
from shapely.ops import triangulate, unary_union

from tools.endpoint_geometry import business_geometry, polygon_union, polygon_difference, polygon_intersection
from tools.endpoint_boundary_band import connect_estimate, nodes_from_rows, TAU
from tools.endpoint_boundary_surface import add_origin_condition
from tools.endpoint_radial_boundary import compute_radial_boundary
from life_circle.coordinates import normalize


async def measure_batch(session,points,kind):
    """Commit results in requested order, never network completion order."""
    keys=[normalize(session.projection.to_geographic(p)) for p in points]
    pending=list(dict.fromkeys(k for k,p in zip(keys,points)
        if k not in session._requests and session.domain.covers(Point(p))))
    observations=await session.scheduler.observe_many(pending)
    if session.scheduler._stopped() and session.scheduler.stop_reason!='budget':
        return [(None,session.scheduler.stop_reason) for _ in keys]
    for key,obs in zip(pending,observations):
        if obs.attempts:
            session._requests[key]=session.ingest(obs,kind)
    results=[]
    for key,p in zip(keys,points):
        record,error=session._requests.get(key,(None,'budget' if session.domain.covers(Point(p)) else 'request_outside_domain'))
        if record and tuple(record['xy']) in session._conflicts:record,error=None,'conflicting_observations'
        results.append((record,error))
    return results


async def scan_ray(session,angle,start,end,*,step=50,target=25,parallel_samples=False):
    """Keep every observed change; a failed sample breaks adjacency."""
    if step<=0 or end<start:raise ValueError('Invalid radial interval')
    result=dict(angle=angle,brackets=[],unknown=[],samples=[])
    count=max(1,math.ceil((end-start)/step));previous=None
    ux,uy=math.cos(angle),math.sin(angle)
    radii=[start+(end-start)*i/count for i in range(count+1)]
    prefetched=await measure_batch(session,[(r*ux,r*uy) for r in radii],'local_radial_scan') if parallel_samples else None
    for i in range(count+1):
        if session.scheduler._stopped() or (session.scheduler.remaining<=0 and prefetched is None):break
        radius=radii[i]
        record,error=prefetched[i] if prefetched is not None else await session.measure((radius*ux,radius*uy),'local_radial_scan')
        result['samples'].append(record)
        if record is None:
            result['unknown'].append(dict(radius=radius,reason=error));previous=None;continue
        if previous and (previous['duration']<=900)!=(record['duration']<=900):
            result['brackets'].append(await session.refine(previous,record,target=target))
        previous=record
    return result


def patch_triangles(records):
    lookup={tuple(r['xy']):r for r in records}
    if len(lookup)<3:return []
    return [(p,[lookup[tuple(xy)] for xy in list(p.exterior.coords)[:3]])
            for p in triangulate(MultiPoint(list(lookup)))]


def mixed_edges(records,patch,target):
    edges={}
    for triangle,vertices in patch_triangles(records):
        if triangle.intersection(patch).area<=0:continue
        for i,a in enumerate(vertices):
            b=vertices[(i+1)%3];width=math.dist(a['xy'],b['xy'])
            if (a['duration']<=900)!=(b['duration']<=900) and width>target:
                edges[tuple(sorted((a['id'],b['id'])))]=(width,a,b)
    return edges


def connect_patch(records,domain,blocked=(),*,target=25):
    """No time interpolation. Unlocalized mixed triangles remain unknown."""
    inside=[];outside=[];candidates=[]
    blocks=[Point(p) for p in blocked]
    for triangle,vertices in patch_triangles(records):
        clipped=polygon_intersection(triangle,domain)
        if clipped.area<=0:continue
        if any(triangle.covers(p) for p in blocks):continue
        labels=[p['duration']<=900 for p in vertices]
        if all(labels):inside.append(clipped);candidates.append(clipped);continue
        if not any(labels):outside.append(clipped);continue
        crossings=[];widths=[]
        for i,a in enumerate(vertices):
            b=vertices[(i+1)%3]
            if (a['duration']<=900)!=(b['duration']<=900):
                crossings.append([(x+y)/2 for x,y in zip(a['xy'],b['xy'])])
                widths.append(math.dist(a['xy'],b['xy']))
        estimate=MultiPoint([p['xy'] for p in vertices if p['duration']<=900]+crossings).convex_hull
        candidates.append(polygon_intersection(estimate,domain))
        if max(widths)<=target:
            inside.append(polygon_intersection(estimate,domain))
            outside.append(polygon_intersection(polygon_difference(triangle,estimate),domain))
    reachable=polygon_union(inside);unreachable=polygon_union(outside)
    return dict(reachable=reachable,unreachable=unreachable,
        unknown=polygon_difference(domain,polygon_union([reachable,unreachable])),candidate=polygon_union(candidates))


def discover_patches(rows,origin,domain,conflicts,records,*,radial_trigger=100,margin=75):
    nodes=nodes_from_rows(rows,origin,domain,conflicts);specs=[];parts=[]
    for i,a in enumerate(nodes):
        b=nodes[(i+1)%len(nodes)];gap=(b['angle']-a['angle'])%TAU
        ra=math.dist(a['xy'],origin);rb=math.dist(b['xy'],origin)
        if abs(ra-rb)<=radial_trigger or gap>=math.pi/2:continue
        lo=max(0,min(ra,rb)-margin);hi=max(ra,rb)+margin
        # Dense arc points define the local sector extent, not measured boundary.
        angles=[a['angle']+gap*j/16 for j in range(17)]
        sector=Polygon([(r*math.cos(t),r*math.sin(t)) for r,seq in ((hi,angles),(lo,angles[::-1])) for t in seq]).intersection(domain)
        specs.append(dict(angle=a['angle'],gap=gap,start=lo,end=hi));parts.append(sector)
    connected=connect_estimate(rows,origin,domain,conflicts,witnesses=records)
    for e in connected['negative_evidence']:
        if e['insideEstimate']:
            x,y=e['xy'];parts.append(box(x-100,y-100,x+100,y+100).intersection(domain))
            r=math.hypot(x,y);angle=math.atan2(y,x)%TAU;gap=min(.5,150/max(r,1))
            specs.append(dict(angle=angle-gap/2,gap=gap,start=max(0,r-100),end=r+100))
    return specs,unary_union(parts),connected


def close_patch_evidence(records,patch,base,blocked,*,target=25,domain=None):
    """Revoke stale faces around newly observed negatives at the repair seam."""
    original_area=patch.area
    negatives=[r for r in records if r['duration']>900]
    # A negative vertex constrains every incident face, including faces that
    # extend beyond the initial repair window. No new provider queries here.
    ids={r['id'] for r in negatives if base.covers(Point(r['xy']))}
    incident=[triangle for triangle,vertices in patch_triangles(records)
              if any(r['id'] in ids for r in vertices)]
    patch=unary_union([patch,*incident])
    if domain is not None:patch=patch.intersection(domain)
    connected=connect_patch(records,patch,blocked,target=target)
    remainder=polygon_difference(base,patch)
    estimate=polygon_union([remainder,connected['reachable']])
    candidate=polygon_union([remainder,connected['candidate']])
    conflicts=[r['id'] for r in negatives if estimate.covers(Point(r['xy']))]
    return dict(**connected,patch=patch,estimate=estimate,combined_candidate=candidate,
        conflicts=conflicts,expanded_area_m2=patch.area-original_area)


def poi_patch_focus(session, candidate, patch, *, positive_only=False, gap_m=0):
    """Only observed POI contradictions trigger exploration; no truth labels or filled corridors."""
    if candidate is None:
        return patch, [], dict(positive_outside=0, negative_inside=0, incident_faces=0)
    locations = {tuple(session.projection.to_local(row['route_destination']))
                 for row in session.log if row['kind'] == 'poi_shared' and row['accepted']}
    focus = [r for r in session.records if tuple(r['xy']) in locations
             and (r['duration'] <= 900) != candidate.covers(Point(r['xy']))
             and (not positive_only or (r['duration'] <= 900 and candidate.distance(Point(r['xy'])) > gap_m))]
    identities = {r['id'] for r in focus}
    incident = [triangle for triangle, vertices in patch_triangles(session.records)
                if any(r['id'] in identities for r in vertices)]
    expanded = polygon_intersection(polygon_union([patch, *incident]), session.domain) if incident else patch
    return expanded, [r['xy'] for r in focus], dict(
        positive_outside=sum(r['duration'] <= 900 for r in focus),
        negative_inside=sum(r['duration'] > 900 for r in focus), incident_faces=len(incident))


async def compute_multicross_boundary(request,provider,token,*,radial_step=50,target=25,
                                     allow_network=False,parallel_sampling=False,edge_batch_size=1,
                                     on_checkpoint=None, diverse_batches=False, coverage_first=False,
                                     edge_queue_policy=None, on_local_start=None, poi_guided=False,
                                     poi_discovery_only=False):
    if provider.network and not allow_network:raise ValueError('E8.2 requires explicit real-provider enablement')
    if not math.isfinite(radial_step) or radial_step<=0:raise ValueError('Invalid radial step')
    if type(edge_batch_size) is not int or not 1<=edge_batch_size<=30:raise ValueError('Invalid edge batch size')
    if edge_queue_policy not in (None, 'combined', 'stagnation', 'diversity'):
        raise ValueError('Invalid edge queue policy')
    if diverse_batches and edge_queue_policy not in (None, 'combined'):
        raise ValueError('Conflicting edge queue policy')
    queue_policy = edge_queue_policy or ('combined' if diverse_batches else None)
    extension={}
    async def initialize(session):
        # First real request doubles as the east initial sample and auth check.
        first,error=await session.measure((300,0),'direction_search')
        if first is not None and not session.scheduler._stopped():
            points=[(300*math.cos(TAU*i/16),300*math.sin(TAU*i/16)) for i in range(1,16)]
            await measure_batch(session,points,'direction_search')
    async def repair(session,rows):
        if on_local_start is not None and not token.cancelled:
            await on_local_start(session, rows)
        anchor=add_origin_condition(session);origin=anchor['xy'] if anchor else (0,0)
        specs,patch,base=discover_patches(rows,origin,session.domain,session._conflicts,session.records)
        start_calls=session.scheduler.stats.requests
        extension['localRepair']=dict(patches=len(specs),scanRays=[],edgeBrackets=[],edgeBatches=[],calls=0,
            radialStepM=radial_step,targetM=target,physicalBarrierVerified=False)
        meta=extension['localRepair']
        focus = []
        if poi_guided or poi_discovery_only:
            patch, focus, guidance = poi_patch_focus(session, base['candidate'], patch,
                positive_only=poi_discovery_only, gap_m=target if poi_discovery_only else 0)
            meta['poiGuidance'] = dict(guidance, mode='discover' if poi_discovery_only else 'focus',
                batch_size=min(edge_batch_size, 10) if focus and poi_guided else edge_batch_size)
        if patch.is_empty or base['candidate'] is None:return
        checkpoint_at=400;attempted=set();planned_rays=0;scanned_rays=0
        async def checkpoint(phase,force=False):
            nonlocal checkpoint_at
            calls=session.scheduler.stats.requests
            if on_checkpoint is None or (calls<checkpoint_at and not force):return
            blocked=list(session._conflicts)+[session.projection.to_local(e['destination']) for e in session.log if not e['accepted']]
            conn=close_patch_evidence(session.records,patch,base['candidate'],blocked,target=target,domain=session.domain)
            edges=mixed_edges(session.records,conn['patch'],target)
            value=dict(calls=calls,phase=phase,unresolvedEdges=len(edges),
                pendingUnattemptedEdges=sum(k not in attempted for k in edges),
                pendingScanRays=max(0,planned_rays-scanned_rays),
                unknownAreaM2=conn['unknown'].area,areaM2=conn['estimate'].area,
                negativeConflicts=conn['conflicts'],
                geometry=business_geometry(conn['estimate'],session.projection) if not conn['conflicts'] else None,
                unknownRegion=business_geometry(conn['unknown'],session.projection))
            await on_checkpoint(value)
            checkpoint_at=max(checkpoint_at+200,(calls//200+1)*200)
        # Scan anomalous regions only. Deterministic interleaving across patches
        # keeps a single early patch from consuming every remaining query.
        plans=[]
        for spec in specs:
            count=max(1,math.ceil(spec['gap']*spec['end']/75))
            plans.append([(spec['angle']+spec['gap']*i/count,spec['start'],spec['end']) for i in range(count+1)])
        planned_rays=sum(map(len,plans))
        # Reserve roughly half the remaining calls for actual mixed-edge refinement.
        scan_limit=start_calls+int(session.scheduler.remaining*.5)
        done=set()
        for i in range(max(map(len,plans), default=0)):
            for plan in plans:
                if i>=len(plan) or session.scheduler._stopped() or session.scheduler.stats.requests>=scan_limit:continue
                angle,lo,hi=plan[i];key=(round(angle%TAU,8),round(lo,3),round(hi,3))
                if key in done:continue
                done.add(key)
                meta['scanRays'].append(await scan_ray(session,angle,lo,hi,step=radial_step,target=target,parallel_samples=parallel_sampling))
                scanned_rays+=1
                await checkpoint('local_scan')
        if queue_policy:
            from tools.endpoint_e83_sampling import DiverseEdgeQueue
            queue = DiverseEdgeQueue(spread=queue_policy != 'stagnation', pause=queue_policy != 'diversity')
        while not session.scheduler._stopped() and session.scheduler.remaining>0:
            edges={k:v for k,v in mixed_edges(session.records,patch,target).items() if k not in attempted}
            if not edges:break
            selected=sorted(edges,key=lambda k:(edges[k][0],k),reverse=True)[:edge_batch_size]
            if focus and poi_guided:
                def priority(key):
                    width, a, b = edges[key]
                    midpoint = [(x+y)/2 for x,y in zip(a['xy'], b['xy'])]
                    return (min(math.dist(midpoint, xy) for xy in focus), -width, key)
                selected = sorted(edges, key=priority)[:min(edge_batch_size, 10)]
            if queue_policy:
                queue.update(session)
                selected = queue.select(edges, edge_batch_size)
                if not selected:
                    break
            if edge_batch_size>1:
                proposals=[[(x+y)/2 for x,y in zip(edges[k][1]['xy'],edges[k][2]['xy'])] for k in selected]
                before=session.scheduler.stats.requests
                await measure_batch(session,proposals,'boundary_batch_midpoint')
                meta['edgeBatches'].append(dict(points=len(proposals),calls=session.scheduler.stats.requests-before,
                    cumulativeCalls=session.scheduler.stats.requests))
            for key in selected:
                if session.scheduler._stopped():break
                _,a,b=edges[key];attempted.add(key)
                bracket = await session.refine(a,b,target=target,max_rounds=1 if edge_batch_size>1 else 16)
                meta['edgeBrackets'].append(bracket)
                if queue_policy and bracket['iterations'] > 0:
                    queue.update(session)
                    queue.progress(queue.midpoint(edges[key]))
            if edge_batch_size>1:
                ids={p['id'] for p in session.records if p['duration']>900 and base['candidate'].covers(Point(p['xy']))}
                incident=[tri for tri,vertices in patch_triangles(session.records) if any(p['id'] in ids for p in vertices)]
                patch=unary_union([patch,*incident]).intersection(session.domain)
            await checkpoint('boundary_batches')
        if queue_policy:
            queue.update(session)
            meta['queuePolicy'] = queue.diagnostics()
        blocked=list(session._conflicts)+[session.projection.to_local(e['destination']) for e in session.log if not e['accepted']]
        connected=close_patch_evidence(session.records,patch,base['candidate'],blocked,target=target,domain=session.domain)
        patch=connected['patch'].intersection(session.domain)
        estimate=connected['estimate'].intersection(session.domain)
        candidate=connected['combined_candidate'].intersection(session.domain)
        conflicts=connected['conflicts']
        unresolved=mixed_edges(session.records,patch,target)
        pending=sum(k not in attempted for k in unresolved)
        initial_unfinished=sum(row.get('status')!='localized' for row in rows)
        resolution=(not unresolved and scanned_rays>=planned_rays and not initial_unfinished
            and not conflicts and connected['unknown'].area<1e-6 and not token.cancelled)
        extension['completion']=dict(scope='observed_local_boundary_only',resolutionReached=resolution,
            budgetExhausted=session.scheduler.remaining<=0,unresolvedEdges=len(unresolved),
            pendingUnattemptedEdges=pending,pendingScanRays=max(0,planned_rays-scanned_rays),
            initialUnfinishedDirections=initial_unfinished,
            reason='resolution_reached' if resolution else session.scheduler.stop_reason or
                ('budget' if session.scheduler.remaining<=0 else 'sampling_stalled_or_unsupported'))
        meta.update(calls=session.scheduler.stats.requests-start_calls,negativeConflicts=conflicts,
            seamExpandedAreaM2=connected['expanded_area_m2'],
            unknownAreaM2=connected['unknown'].area,
            patchGeometry=business_geometry(patch,session.projection),
            connectionAssumption='homogeneous observed vertices; no interior verification')
        extension.update(geometry=None if conflicts else business_geometry(estimate,session.projection),
            candidateGeometry=business_geometry(candidate,session.projection),
            unknownRegion=business_geometry(connected['unknown'],session.projection),
            quality='experimental_evidence_conflict' if conflicts else 'experimental_partial_local_repair',
            uncertaintyBand=None,
            uncertainty=dict(guaranteedCoverage=False,closedEstimate=False,segments=[],
                reason='known_negative_inside_estimate' if conflicts else 'local_unresolved_triangles',
                meaning='unknownRegion marks unlocalized local faces; no calibrated boundary band'))
        await checkpoint('final',force=True)
    result=await compute_radial_boundary(request,provider,token,directions=16,boundary_bands=True,
        target=target,on_sampling_complete=repair,on_session_start=initialize if parallel_sampling else None,
        coverage_first=coverage_first)
    result.update(extension)
    result['algorithm']='local-multicross-e82'
    from life_circle.coordinates import LocalProjection
    projection=LocalProjection(request.origin)
    for e in result.get('negativeEvidence',[]):
        e['coordinateLocal']=list(projection.to_local(e['coordinate']))
        if 'geometry' in extension:
            from shapely.geometry import shape
            e['insideEstimate']=bool(result['candidateGeometry'] and shape(result['candidateGeometry']).covers(Point(e['coordinate'])))
    result['assumption']='local observed-vertex connection; unsampled gaps and interior islands may be missed'
    result.setdefault('localRepair',dict(patches=0,calls=0,scanRays=[],edgeBrackets=[]))
    result.setdefault('completion',dict(scope='observed_local_boundary_only',resolutionReached=False,
        budgetExhausted=result['calls']>=request.budget,unresolvedEdges=0,pendingUnattemptedEdges=0,
        reason='no_local_repair_or_insufficient_initial_support'))
    if token.cancelled:
        result.update(status='cancelled',geometry=None,candidateGeometry=None,unknownRegion=None)
    return result
