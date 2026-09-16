"""E4.1.1 actual-point triangulation, preserving nonzero seed faces."""
from collections import defaultdict
import copy
import json
import time

import shapely
from shapely.geometry import MultiPoint

from tools.endpoint_fresh_mesh import check_cancel, filter_network, circumradius
from tools.endpoint_observations import finite
from tools.endpoint_stable_triangulation import integer_points, orient, legalize, exact_audit, seed_integrity


def observations(records,cancelled):
    groups=defaultdict(list);seen=set();origins=set()
    for row in records:
        check_cancel(cancelled);p=row.get('xy');i=row.get('id');duration=row.get('duration')
        if (not isinstance(i,str) or not i or i in seen or not isinstance(p,(list,tuple)) or len(p)!=2
            or not all(finite(v) for v in p) or not finite(duration) or duration<0 or row.get('origin') is None):
            raise ValueError('Invalid observation')
        seen.add(i);origins.add(json.dumps(row['origin'],sort_keys=True,allow_nan=False));groups[tuple(p)].append(row)
    if len(origins)>1:raise ValueError('Mixed actual origin cohorts')
    nodes={};conflicts=[]
    for xy,rows in sorted(groups.items()):
        values=sorted({r['duration'] for r in rows});ids=sorted(r['id'] for r in rows)
        if len(values)>1:
            conflicts.append(dict(xy=xy,record_ids=ids,durations=values));continue
        nodes[f'p{len(nodes):05d}']=dict(actual=xy,request=xy,duration=values[0],
                                       origin=copy.deepcopy(rows[0]['origin']),record_ids=ids)
    return nodes,conflicts


def exact_structure(nodes,faces,*,cancelled=lambda:False,deadline=None):
    audit=seed_integrity(nodes,faces,cancelled=cancelled)
    if not audit['valid']:return audit
    xy=dict(zip(sorted(nodes),integer_points([nodes[i]['actual'] for i in sorted(nodes)])))
    edge_set={tuple(sorted((t[i],t[j]))) for t in faces for i,j in ((0,1),(1,2),(2,0))}
    edges=sorted(edge_set);issues=[]
    if len(set(tuple(sorted(t)) for t in faces))!=len(faces):issues.append(dict(reason='duplicate_face'))
    bounds={e:(min(xy[e[0]][0],xy[e[1]][0]),min(xy[e[0]][1],xy[e[1]][1]),
               max(xy[e[0]][0],xy[e[1]][0]),max(xy[e[0]][1],xy[e[1]][1])) for e in edges}
    for ordinal,e in enumerate(edges):
        check_cancel(cancelled)
        if deadline is not None and time.monotonic()>deadline:raise TimeoutError('Exact structure deadline')
        a,b=(xy[i] for i in e);x0,y0,x1,y1=bounds[e]
        for i,p in xy.items():
            if i not in e and x0<=p[0]<=x1 and y0<=p[1]<=y1 and orient(a,b,p)==0:
                issues.append(dict(reason='t_junction',edge=e,point=i))
        for f in edges[ordinal+1:]:
            if e[0] in f or e[1] in f:continue
            u0,v0,u1,v1=bounds[f]
            if x1<u0 or u1<x0 or y1<v0 or v1<y0:continue
            c,d=(xy[i] for i in f)
            if orient(a,b,c)*orient(a,b,d)<0 and orient(c,d,a)*orient(c,d,b)<0:
                issues.append(dict(reason='crossing_edges',edges=[e,f]))
    return dict(audit,valid=not issues,issues=issues,exact_edges_checked=len(edges))


def preserved_network(records,domain,*,radius=None,cancelled=lambda:False,max_checks=100000,
                      max_flips=10000,seconds=120,audit_seconds=300):
    started=time.monotonic();deadline=started+seconds;check_cancel(cancelled)
    if radius is not None and (not finite(radius) or radius<=0):raise ValueError('Invalid radius')
    if not domain.is_valid or domain.area<=0:raise ValueError('Invalid domain')
    nodes,conflicts=observations(records,cancelled)
    prepared=dict(nodes=nodes,all_triangles=[],conflicts=conflicts,input_records=len(records),
                  face_circumradii_m=[],discarded_zero_area_faces=[])
    def failure(reason,**evidence):
        return dict(prepared,status='invalid_'+reason,triangles=[],field=None,used_points=0,used_records=0,
                    points=len(nodes),point_only=sorted(nodes),audit={'valid':False},**evidence)
    if len(nodes)<3:return filter_network(prepared,domain,radius=radius,cancelled=cancelled)
    ids=sorted(nodes);xy=dict(zip(ids,integer_points([nodes[i]['actual'] for i in ids])))
    lookup={tuple(n['actual']):i for i,n in nodes.items()};raw_faces=[];discarded=[]
    try:
        for polygon in shapely.delaunay_triangles(MultiPoint(list(lookup)),tolerance=0).geoms:
            check_cancel(cancelled)
            if time.monotonic()>deadline:raise TimeoutError('Seed extraction deadline')
            t=[lookup[tuple(p)] for p in list(polygon.exterior.coords)[:-1]]
            if len(t)!=3:raise ValueError('Non-triangle seed')
            sign=orient(*(xy[i] for i in t))
            if sign==0:
                discarded.append(dict(ids=t,reason='exact_zero_area'));continue
            if sign<0:t[1],t[2]=t[2],t[1]
            k=t.index(min(t));raw_faces.append(tuple(t[k:]+t[:k]))
        prepared.update(all_triangles=sorted(raw_faces),discarded_zero_area_faces=discarded)
        if not raw_faces:
            # Exact collinear inputs have no surface; missing noncollinear seed
            # is an execution failure, not evidence of an empty isochrone.
            if any(orient(xy[ids[0]],xy[ids[1]],xy[i]) for i in ids[2:]):return failure('missing_seed')
            return filter_network(prepared,domain,radius=radius,cancelled=cancelled)
        seed=exact_structure(nodes,raw_faces,cancelled=cancelled,deadline=deadline)
        if not seed['valid']:return failure('seed',seed_integrity=seed)
        faces,trace=legalize(nodes,raw_faces,cancelled=cancelled,max_checks=max_checks,max_flips=max_flips,
                             seconds=deadline-time.monotonic())
        final_structure=exact_structure(nodes,faces,cancelled=cancelled,deadline=deadline)
        if not final_structure['valid']:return failure('final_structure',seed_integrity=final_structure)
        if audit_seconds<=0:raise TimeoutError('Exact audit budget exhausted')
        audit_start=time.monotonic();audit=exact_audit(nodes,faces,cancelled=cancelled,seconds=audit_seconds)
        audit_elapsed=time.monotonic()-audit_start;deadline+=audit_elapsed
        if not audit['valid']:return failure('delaunay',delaunay_audit=audit)
        prepared.update(all_triangles=faces,face_circumradii_m=[circumradius([nodes[i]['actual'] for i in t]) for t in faces])
        result=filter_network(prepared,domain,radius=radius,cancelled=cancelled)
        if time.monotonic()>deadline:return failure('build_deadline')
        result.update(seed_integrity=seed,final_structure=final_structure,delaunay_audit=audit,legalization=trace,
                      raw_face_count=len(raw_faces),audit_seconds=audit_elapsed,build_seconds=time.monotonic()-started-audit_elapsed)
        return result
    except (TimeoutError,ValueError,ZeroDivisionError) as exc:
        return failure('construction',failure_type=type(exc).__name__,failure_reason=str(exc))
