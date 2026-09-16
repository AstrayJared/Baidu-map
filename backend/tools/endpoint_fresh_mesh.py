"""E4 actual-only geometric candidate network, with no routing or old mesh input."""
from collections import defaultdict
import copy
import json
import math

import shapely
from shapely.geometry import MultiPoint, Polygon, mapping

from tools.endpoint_observations import ExperimentCancelled, finite
from tools.endpoint_support import audit_mesh, signed
from tools.endpoint_reconstruction import combine
from tools.fixed_evidence_representation import clip_triangle
from life_circle.field import multipolygon


def check_cancel(cancelled):
    if cancelled():
        raise ExperimentCancelled('E4 cancelled; candidate discarded')


def canonical(ids, nodes):
    ids=list(ids)
    if signed([nodes[i]['actual'] for i in ids])<0:
        ids[1],ids[2]=ids[2],ids[1]
    k=ids.index(min(ids))
    return tuple(ids[k:]+ids[:k])


def circumradius(points):
    area=abs(signed(points))/2
    return math.prod(math.dist(points[i],points[j]) for i,j in ((0,1),(1,2),(2,0)))/(4*area)


def field(nodes, triangles, domain):
    parts=[]
    for ids in triangles:
        points=[nodes[i]['actual'] for i in ids]
        footprint=Polygon(points).intersection(domain)
        if footprint.area>0:
            reach=clip_triangle(points,[nodes[i]['duration'] for i in ids])
            parts.append((footprint,multipolygon(reach.intersection(footprint))))
    return combine(parts,domain) if parts else None


def build_network(records, domain, *, radius=None, barriers=None, cancelled=lambda:False):
    """Records have id, xy (actual metres), duration and origin. No request geometry.

    Radius screens whole faces before interpolation. Support is a geometric
    candidate, never a guarantee of walking connectivity or calibrated accuracy.
    """
    check_cancel(cancelled)
    if radius is not None and (not finite(radius) or radius<=0):
        raise ValueError('Invalid radius')
    if not domain.is_valid or domain.area<=0:
        raise ValueError('Invalid domain')
    if barriers is not None and not barriers.is_valid:
        raise ValueError('Invalid independent barrier')
    groups=defaultdict(list); identities=set(); origins=set()
    for r in records:
        check_cancel(cancelled)
        p=r.get('xy'); identity=r.get('id'); duration=r.get('duration')
        if (not isinstance(identity,str) or not identity or identity in identities
            or not isinstance(p,(tuple,list)) or len(p)!=2 or not all(finite(v) for v in p)
            or not finite(duration) or duration<0 or r.get('origin') is None):
            raise ValueError('Invalid observation')
        identities.add(identity)
        origins.add(json.dumps(r['origin'],sort_keys=True,allow_nan=False))
        groups[tuple(p)].append(r)
    if len(origins)>1:
        raise ValueError('Mixed actual origin cohorts')
    nodes={}; conflicts=[]
    for xy,members in sorted(groups.items()):
        values=sorted({r['duration'] for r in members})
        aliases=sorted(r['id'] for r in members)
        if len(values)>1:
            conflicts.append(dict(xy=xy,record_ids=aliases,durations=values))
            continue
        identity=f'p{len(nodes):05d}'
        # The audit's two embeddings are both this NEW actual geometry. It does
        # not receive the former requested mesh, S0, or former unknown region.
        nodes[identity]=dict(actual=xy,request=xy,duration=values[0],
                             origin=copy.deepcopy(members[0]['origin']),record_ids=aliases)
    points=[n['actual'] for n in nodes.values()]
    hull=MultiPoint(points).convex_hull
    lookup={tuple(n['actual']):i for i,n in nodes.items()}
    all_faces=[]; discarded=[]
    if hull.area>0:
        for polygon in shapely.delaunay_triangles(MultiPoint(points),tolerance=0).geoms:
            check_cancel(cancelled)
            ids=[lookup[tuple(p)] for p in list(polygon.exterior.coords)[:-1]]
            if len(ids)!=3: raise ValueError('Non-triangle Delaunay output')
            ids=canonical(ids,nodes)
            area=abs(signed([nodes[i]['actual'] for i in ids]))/2
            if area<=5e-9:
                discarded.append(dict(ids=ids,area_m2=area,reason='zero_measure_at_fixed_audit_precision'))
            else:
                all_faces.append(ids)
    all_faces=sorted(all_faces)
    prepared=dict(nodes=nodes,all_triangles=all_faces,conflicts=conflicts,input_records=len(records),
                  face_circumradii_m=[circumradius([nodes[i]['actual'] for i in t]) for t in all_faces],
                  discarded_zero_area_faces=sorted(discarded,key=lambda r:r['ids']))
    return filter_network(prepared,domain,radius=radius,barriers=barriers,cancelled=cancelled)


def filter_network(prepared,domain,*,radius=None,barriers=None,cancelled=lambda:False):
    """Select faces from ONE actual Delaunay; never delete points and retriangulate."""
    check_cancel(cancelled)
    if radius is not None and (not finite(radius) or radius<=0): raise ValueError('Invalid radius')
    nodes=prepared['nodes']; all_faces=prepared['all_triangles']
    hull=MultiPoint([n['actual'] for n in nodes.values()]).convex_hull
    radii=dict(zip(all_faces,prepared['face_circumradii_m']))
    triangles=[]; radius_rejected=0; barrier_rejected=0
    for ids in all_faces:
        check_cancel(cancelled)
        if radius is not None and radii[ids]>radius:
            radius_rejected+=1; continue
        p=Polygon([nodes[i]['actual'] for i in ids])
        if barriers is not None and p.intersection(barriers).area>0:
            barrier_rejected+=1; continue
        if p.intersection(domain).area>0:
            triangles.append(ids)
    # Validate WHOLE input faces before clipping them to the evaluation domain.
    # A face's representative point can lie outside D although it intersects D;
    # using D here would falsely classify that boundary face as another component.
    audit=(audit_mesh(nodes,triangles,hull,cancelled=cancelled) if nodes else
           dict(valid=True,counts={},issues=[],faces=0,shared_edge_max_error_seconds=0))
    candidate=field(nodes,triangles,domain) if audit['valid'] else None
    used={i for t in triangles for i in t} if candidate is not None else set()
    edge_lengths=[math.dist(nodes[t[i]]['actual'],nodes[t[j]]['actual'])
                  for t in triangles for i,j in ((0,1),(1,2),(2,0))]
    out=dict(status='invalid_geometry' if not audit['valid'] else 'candidate' if candidate is not None else 'insufficient_geometry',
             nodes=nodes,triangles=triangles,all_triangles=all_faces,audit=audit,field=candidate,
             points=len(nodes),input_records=prepared['input_records'],used_points=len(used),
             used_records=sum(len(nodes[i]['record_ids']) for i in used),
             point_only=sorted(set(nodes)-used),conflicts=prepared['conflicts'],radius_m=radius,
             radius_rejected_faces=radius_rejected,barrier_rejected_faces=barrier_rejected,
             hull_area_m2=hull.intersection(domain).area,
             max_edge_m=max(edge_lengths,default=0),
             face_circumradii_m=[radii[t] for t in all_faces],
             discarded_zero_area_faces=prepared['discarded_zero_area_faces'],
             verified_area_m2=None,edge_evidence='geometric_only')
    if candidate is not None:
        closure=max(candidate['support'].union(candidate['unknown']).symmetric_difference(domain).area,
                    candidate['support'].intersection(candidate['unknown']).area,
                    candidate['geometry'].difference(candidate['support']).area)
        out['closure_error_m2']=closure
        if closure>1e-6: raise ValueError('Domain partition failed')
        if radius is None and barriers is None:
            out['hull_gap_m2']=candidate['support'].symmetric_difference(hull.intersection(domain)).area
            if out['hull_gap_m2']>1e-6: raise ValueError('Delaunay failed to cover hull')
    check_cancel(cancelled)
    return out


def export_result(result):
    out={k:v for k,v in result.items() if k!='field'}
    out['field']={k:mapping(v) for k,v in result['field'].items()} if result['field'] is not None else None
    return out
