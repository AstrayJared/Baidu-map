"""Local evidence and explicitly estimated boundary bands for E8.1."""
import math

from shapely.geometry import LineString, Point, Polygon
from shapely.ops import unary_union

TAU=2*math.pi


async def probe_sides(session, bracket, *, target, limit=4):
    """Try a bounded set near a stalled bracket; only actual shrinkage is progress."""
    probes=[]
    if bracket['reason']!='offset_stagnation' or limit<=0:
        return bracket,probes
    a,b=bracket['left'],bracket['right']
    width=math.dist(a['xy'],b['xy'])
    if not width or session.scheduler._stopped():return bracket,probes
    mid=[(x+y)/2 for x,y in zip(a['xy'],b['xy'])]
    normal=[-(b['xy'][1]-a['xy'][1])/width,(b['xy'][0]-a['xy'][0])/width]
    initial=bracket
    for fraction in (.3,-.3,.55,-.55)[:limit]:
        if session.scheduler._stopped() or session.scheduler.remaining<=0:break
        proposal=[x+fraction*width*n for x,n in zip(mid,normal)]
        before=session.scheduler.stats.requests
        record,error=await session.measure(proposal,'boundary_side_probe')
        step=dict(requested_xy=proposal,record=record,reason=error,accepted=False,
                  calls=session.scheduler.stats.requests-before)
        probes.append(step)
        if record is None:continue
        a,b=bracket['left'],bracket['right']
        other=b if (record['duration']<=900)==(a['duration']<=900) else a
        old_width=math.dist(a['xy'],b['xy']);new_width=math.dist(record['xy'],other['xy'])
        if (session.domain.covers(Point(record['xy'])) and new_width<=.9*old_width
                and max(math.dist(record['xy'],p['xy']) for p in (a,b))<=old_width+1e-6):
            step.update(accepted=True,before_width_m=old_width,after_width_m=new_width)
            bracket=await session.refine(record,other,target=target)
            if bracket['status']=='localized' or bracket['reason']!='offset_stagnation':break
    if probes:
        bracket=dict(bracket,side_probe_initial_width_m=initial['width_m'])
    return bracket,probes


def nodes_from_rows(rows,origin,domain,conflicts):
    points={}
    for row in rows:
        b=row.get('bracket')
        if (not b or not row.get('committed',True) or b['status']=='cancelled'
                or b.get('reason')=='conflicting_observations'):continue
        a,c=b['left'],b['right']
        if (a['origin']!=c['origin'] or (a['duration']<=900)==(c['duration']<=900)
                or any(tuple(p['xy']) in conflicts or not domain.covers(Point(p['xy'])) for p in (a,c))):continue
        mid=[(x+y)/2 for x,y in zip(a['xy'],c['xy'])]
        width=math.dist(a['xy'],c['xy'])
        if math.dist(mid,origin)<1e-6:continue
        key=tuple(round(x,6) for x in mid)
        node=dict(xy=mid,width_m=width,angle=math.atan2(mid[1]-origin[1],mid[0]-origin[0])%TAU,
                  row=row,bracket=b)
        if key not in points or width<points[key]['width_m']:points[key]=node
    # Two points on the same ray cannot define a nonzero angular segment.
    angular={}
    for node in points.values():
        key=round(node['angle'],10)
        if key not in angular or node['width_m']<angular[key]['width_m']:angular[key]=node
    return sorted(angular.values(),key=lambda n:n['angle'])


def next_direction(rows,origin,domain,conflicts,attempted,chord_target):
    nodes=nodes_from_rows(rows,origin,domain,conflicts)
    if len(nodes)<2:return None
    proposals=[]
    for i,a in enumerate(nodes):
        b=nodes[(i+1)%len(nodes)];gap=(b['angle']-a['angle'])%TAU
        angle=(a['angle']+gap/2)%TAU
        if any(abs((angle-old+math.pi)%TAU-math.pi)<1e-8 for old in attempted):continue
        chord=math.dist(a['xy'],b['xy'])
        if chord<=chord_target:continue
        score=chord+max(a['width_m'],b['width_m'])
        hint=(math.dist(a['xy'],origin)+math.dist(b['xy'],origin))/2
        proposals.append((score,-angle,angle,hint))
    if not proposals:return None
    _,_,angle,hint=max(proposals)
    return angle,hint


def connect_estimate(rows,origin,domain,conflicts,*,target=25,chord_target=100,witnesses=()):
    nodes=nodes_from_rows(rows,origin,domain,conflicts)
    # Preserve evidence independently of whether it participates in the final bracket.
    negatives=[dict(id=p['id'],xy=list(p['xy']),durationSeconds=p['duration'],
        kind='over_threshold',physicalBarrierVerified=False,insideEstimate=None)
        for p in witnesses if p['duration']>900 and tuple(p['xy']) not in conflicts]
    jumps=[]
    for row in rows:
        b=row.get('bracket')
        if b and b.get('suspected_jump'):
            jumps.append(dict(sourceIds=[b['left']['id'],b['right']['id']],
                start_xy=b['left']['xy'],end_xy=b['right']['xy'],width_m=b['width_m'],
                kind='suspected_time_jump',physicalBarrierVerified=False))
    result=dict(envelope=None,band=None,segments=[],nodes=nodes,closed=False,
                reason='insufficient_brackets',max_gap_radians=None,candidate=None,
                negative_evidence=negatives,jump_evidence=jumps)
    if len(nodes)<3:return result
    gaps=[(nodes[(i+1)%len(nodes)]['angle']-a['angle'])%TAU for i,a in enumerate(nodes)]
    result['max_gap_radians']=max(gaps)
    if max(gaps)>=math.pi/2-1e-9:
        result['reason']='angular_gap_too_large';return result
    polygon=Polygon([n['xy'] for n in nodes])
    if not polygon.is_valid or polygon.area<=0:
        result['reason']='invalid_estimated_ring';return result
    polygon=polygon.intersection(domain)
    for evidence in negatives:
        evidence['insideEstimate']=polygon.covers(Point(evidence['xy']))
    bands=[]
    for i,a in enumerate(nodes):
        b=nodes[(i+1)%len(nodes)];gap=gaps[i]
        width=max(a['width_m'],b['width_m']);reasons=[]
        if width>target:reasons.append('bracket_not_localized')
        if math.dist(a['xy'],b['xy'])>chord_target:reasons.append('angular_sampling_gap')
        for node in (a,b):
            if node['bracket'].get('suspected_jump') and 'suspected_time_jump' not in reasons:
                reasons.append('suspected_time_jump')
            reason=node['row'].get('reason')
            if reason and reason not in reasons:reasons.append(reason)
        for row in rows:
            if row.get('committed',True) and row.get('bracket'):continue
            relative=(row['angle']-a['angle'])%TAU
            if relative<=gap:
                if 'unfinished_direction' not in reasons:reasons.append('unfinished_direction')
                if row.get('reason') and row['reason'] not in reasons:reasons.append(row['reason'])
        line=LineString([a['xy'],b['xy']])
        negative_ids=[]
        for evidence in negatives:
            angle=math.atan2(evidence['xy'][1]-origin[1],evidence['xy'][0]-origin[0])%TAU
            if evidence['insideEstimate'] and (angle-a['angle'])%TAU<=gap:
                negative_ids.append(evidence['id'])
        if negative_ids:reasons.append('known_negative_inside_estimate')
        bands.append(line.buffer(width/2).intersection(domain))
        result['segments'].append(dict(start_xy=a['xy'],end_xy=b['xy'],width_m=width,
            reasons=reasons,negativeEvidenceIds=negative_ids,guaranteedCoverage=False,
            widthMeaning='maximum adjacent measured bracket width; angular error unbounded'))
    # A point cannot establish a wall's shape: retain the candidate for diagnosis,
    # but do not publish a filled result contradicted by measured negative evidence.
    contradicted=any(e['insideEstimate'] for e in negatives)
    result.update(envelope=None if contradicted else polygon,candidate=polygon,
        band=unary_union(bands),closed=True,
        reason='known_negative_inside_estimate' if contradicted else None)
    return result
