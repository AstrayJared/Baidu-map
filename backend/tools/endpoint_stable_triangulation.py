"""E4.1 research predicates and independent exact audit; no routing calls."""
import time
import heapq
from collections import defaultdict

from shapely.geometry import MultiPoint

from tools.endpoint_fresh_mesh import check_cancel, build_network, filter_network, circumradius
from tools.endpoint_support import audit_mesh


def reject(result,reason,**evidence):
    return dict(result,status='invalid_'+reason,field=None,used_points=0,used_records=0,
                point_only=sorted(result['nodes']),**evidence)


def seed_integrity(nodes,triangles,*,cancelled=lambda:False):
    """Exact convex-hull boundary / point membership check before flipping.

    Complements the existing overlap, T-junction and manifold geometry audit.
    Area tolerances alone can accept zero-width interior boundary cracks.
    """
    ids=sorted(nodes);xy=dict(zip(ids,integer_points([nodes[i]['actual'] for i in ids])))
    points=sorted(xy.values());lower=[];upper=[]
    for chain,sequence in ((lower,points),(upper,reversed(points))):
        for p in sequence:
            while len(chain)>=2 and orient(chain[-2],chain[-1],p)<=0:chain.pop()
            chain.append(p)
    hull=lower[:-1]+upper[:-1]; boundary=list(zip(hull,hull[1:]+hull[:1]))
    adjacency=defaultdict(list);issues=[];used=set()
    def on_segment(a,b,p):
        return orient(a,b,p)==0 and min(a[0],b[0])<=p[0]<=max(a[0],b[0]) and min(a[1],b[1])<=p[1]<=max(a[1],b[1])
    for t in triangles:
        check_cancel(cancelled);used.update(t)
        if orient(*(xy[i] for i in t))==0:issues.append(dict(reason='exact_degenerate',face=t))
        for i,j in ((0,1),(1,2),(2,0)):adjacency[tuple(sorted((t[i],t[j])))].append(t)
    for e,adjacent in sorted(adjacency.items()):
        check_cancel(cancelled);a,b=(xy[i] for i in e)
        if len(adjacent)==1 and not any(on_segment(u,v,a) and on_segment(u,v,b) for u,v in boundary):
            issues.append(dict(reason='interior_boundary_edge',edge=e))
        if len(adjacent)>2:issues.append(dict(reason='non_manifold',edge=e))
        if len(adjacent)==2:
            c,d=[xy[next(i for i in t if i not in e)] for t in adjacent]
            if orient(a,b,c)*orient(a,b,d)>=0:issues.append(dict(reason='same_side_faces',edge=e))
    if set(nodes)!=used:issues.append(dict(reason='missing_points',ids=sorted(set(nodes)-used)))
    return dict(valid=not issues,issues=issues,boundary_edges=sum(len(v)==1 for v in adjacency.values()),
                exact_hull_vertices=len(hull))


def legalize(nodes,triangles,*,cancelled=lambda:False,max_checks=100000,max_flips=10000,
             seconds=120,clock=time.monotonic):
    started=clock(); ids=sorted(nodes)
    coords=dict(zip(ids,integer_points([nodes[i]['actual'] for i in ids])))
    def canon(t):
        t=list(t)
        sign=orient(*(coords[i] for i in t))
        if not sign: raise ValueError('Degenerate seed face')
        if sign<0:t[1],t[2]=t[2],t[1]
        k=t.index(min(t));return tuple(t[k:]+t[:k])
    faces={canon(t) for t in triangles}
    if len(faces)!=len(triangles): raise ValueError('Duplicate seed face')
    adjacency=defaultdict(set)
    edges=lambda t: [tuple(sorted((t[i],t[j]))) for i,j in ((0,1),(1,2),(2,0))]
    for t in faces:
        for e in edges(t):adjacency[e].add(t)
    queue=sorted(adjacency);queued=set(queue);seen={tuple(sorted(faces))};events=[];checks=0
    while queue:
        check_cancel(cancelled)
        if checks>=max_checks or clock()-started>=seconds: raise TimeoutError('Legalization check/time cap')
        e=heapq.heappop(queue);queued.remove(e); checks+=1
        adjacent=sorted(adjacency[e])
        if len(adjacent)>2:raise ValueError('Non-manifold seed')
        if len(adjacent)!=2:continue
        first,second=adjacent;a,b=e
        c=next(i for i in first if i not in e);d=next(i for i in second if i not in e)
        # Both diagonals must lie in a strictly convex quadrilateral.
        if orient(coords[a],coords[b],coords[c])*orient(coords[a],coords[b],coords[d])>=0:
            raise ValueError('Seed faces occupy same side of edge')
        if orient(coords[c],coords[d],coords[a])*orient(coords[c],coords[d],coords[b])>=0:continue
        if incircle_integer(coords[a],coords[b],coords[c],coords[d])<=0:continue
        if len(events)>=max_flips:raise TimeoutError('Legalization flip cap')
        new=sorted([canon((c,d,a)),canon((d,c,b))])
        for t in adjacent:
            faces.remove(t)
            for edge in edges(t):adjacency[edge].remove(t)
        for t in new:
            if t in faces:raise ValueError('Duplicate replacement face')
            faces.add(t)
            for edge in edges(t):adjacency[edge].add(t)
        events.append(dict(removed_edge=e,added_edge=tuple(sorted((c,d))),removed_faces=adjacent,added_faces=new))
        state=tuple(sorted(faces))
        if state in seen:raise ValueError('Repeated legalization state')
        seen.add(state)
        for edge in sorted({edge for t in new for edge in edges(t)}):
            if edge not in queued:heapq.heappush(queue,edge);queued.add(edge)
    check_cancel(cancelled)
    return sorted(faces),dict(checks=checks,flips=len(events),events=events)


def stable_network(records,domain,*,radius=None,cancelled=lambda:False,max_checks=100000,
                   max_flips=10000,seconds=120,audit_seconds=300):
    started=time.monotonic()
    result=build_network(records,domain,cancelled=cancelled)
    nodes=result['nodes']; faces=result['all_triangles']
    if not faces:return result
    hull=MultiPoint([n['actual'] for n in nodes.values()]).convex_hull
    seed_audit=audit_mesh(nodes,faces,hull,cancelled=cancelled)
    seed=seed_integrity(nodes,faces,cancelled=cancelled)
    if not seed_audit['valid'] or not seed['valid']:
        return reject(result,'seed',seed_audit=seed_audit,seed_integrity=seed)
    try:
        faces,trace=legalize(nodes,faces,cancelled=cancelled,max_checks=max_checks,max_flips=max_flips,
                             seconds=seconds-(time.monotonic()-started))
        result.update(all_triangles=faces,face_circumradii_m=[circumradius([nodes[i]['actual'] for i in t]) for t in faces])
        audit_started=time.monotonic()
        audit=exact_audit(nodes,faces,cancelled=cancelled,seconds=audit_seconds)
        audit_elapsed=time.monotonic()-audit_started
        if not audit['valid']:return reject(result,'delaunay',delaunay_audit=audit,legalization=trace)
        final=filter_network(result,domain,radius=radius,cancelled=cancelled)
        final.update(delaunay_audit=audit,legalization=trace,seed_audit=seed_audit,seed_integrity=seed,
                     audit_seconds=audit_elapsed,build_seconds=time.monotonic()-started-audit_elapsed)
        if time.monotonic()-started-audit_elapsed>seconds:return reject(final,'build_deadline')
        return final
    except (TimeoutError,ValueError) as exc:
        # Return only research metadata and point evidence on bounded failure.
        return reject(result,'legalization',failure_type=type(exc).__name__,failure_reason=str(exc))


def integer_points(points):
    ratios=[[float(v).as_integer_ratio() for v in p] for p in points]
    scale=max((d for p in ratios for _,d in p),default=1)
    return [tuple(n*(scale//d) for n,d in p) for p in ratios]


def orient(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def incircle_integer(a,b,c,p):
    ax,ay=a[0]-p[0],a[1]-p[1]
    bx,by=b[0]-p[0],b[1]-p[1]
    cx,cy=c[0]-p[0],c[1]-p[1]
    det=(ax*ax+ay*ay)*(bx*cy-by*cx)-(bx*bx+by*by)*(ax*cy-ay*cx)+(cx*cx+cy*cy)*(ax*by-ay*bx)
    value=det*orient(a,b,c)
    return (value>0)-(value<0)


def incircle(a,b,c,p):
    return incircle_integer(*integer_points([a,b,c,p]))


def exact_audit(nodes,triangles,*,cancelled=lambda:False,seconds=300,clock=time.monotonic):
    """Independent circle-polynomial oracle, WITHOUT a float screening step.

    Uses lifted absolute coordinates and circle coefficients, not the translated
    incircle determinant used by the candidate. All non-vertex points are checked.
    """
    started=clock(); ids=sorted(nodes)
    xy=dict(zip(ids,integer_points([nodes[i]['actual'] for i in ids])))
    lifted={i:(x,y,x*x+y*y) for i,(x,y) in xy.items()}
    violations=[]; comparisons=0
    for ordinal,t in enumerate(triangles):
        check_cancel(cancelled)
        if clock()-started>seconds: raise TimeoutError('Exact audit deadline')
        a,b,c=[lifted[i] for i in t]
        A=(b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])
        if A==0:
            violations.append(dict(face=ordinal,ids=t,reason='exact_collinear'));continue
        B=-(b[2]-a[2])*(c[1]-a[1])+(c[2]-a[2])*(b[1]-a[1])
        C=-(b[0]-a[0])*(c[2]-a[2])+(c[0]-a[0])*(b[2]-a[2])
        D=-A*a[2]-B*a[0]-C*a[1]
        witnesses=[]
        for i,(x,y,z) in lifted.items():
            if i in t: continue
            comparisons+=1
            if (A*z+B*x+C*y+D)*A<0: witnesses.append(i)
        if witnesses:
            violations.append(dict(face=ordinal,ids=t,witness=witnesses[0],inside_observations=len(witnesses)))
    check_cancel(cancelled)
    return dict(valid=not violations,triangles_checked=len(triangles),comparisons=comparisons,
                violations=violations,oracle='exact_integer_circle_polynomial',float_prefilter=False)
