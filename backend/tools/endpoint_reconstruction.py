"""E2 fixed-connectivity comparison. Research geometry only; no routing calls."""
from collections import Counter
import shapely
from shapely.geometry import Polygon, GeometryCollection

from life_circle.coordinates import LocalProjection
from life_circle.field import multipolygon
from tools.endpoint_observations import diagnose_triangle, ExperimentCancelled
from tools.fixed_evidence_representation import clip_triangle


def combine(parts,domain):
    support=shapely.union_all([s for s,g in parts])
    if sum(s.area for s,g in parts)-support.area>1e-6:
        raise ValueError('Overlapping support pieces')
    geometry=multipolygon(shapely.union_all([g for s,g in parts]))
    unknown=domain.difference(support)
    if not all(g.is_valid for g in (support,geometry,unknown)) or geometry.difference(support).area>1e-6:
        raise ValueError('Invalid geometry/support')
    return dict(geometry=geometry,support=support,unknown=unknown)


def reconstruct_pair(layer,triangles,domain,*,cancelled=lambda:False):
    projection=LocalProjection(layer['business_center'])
    records={r['id']:r for r in layer['records']}
    blocked={i for p in layer['points'] if p['conflict'] for i in p['record_ids']}
    origins={(tuple(r['request_origin']),tuple(r['actual_origin'])) for r in records.values() if r['usable']}
    counts=Counter(mixed_origins=int(len(origins)>1),input_triangles=len(triangles))
    requested=[];actual=[]
    for ids in sorted(triangles,key=lambda t:tuple(str(i) for i in t)):
        if cancelled(): raise ExperimentCancelled('Cancelled E2 reconstruction')
        items=[records.get(i) for i in ids]
        if len(items)!=3 or any(r is None or not r['usable'] or r['id'] in blocked for r in items) or len(origins)>1:
            counts['excluded_evidence']+=1
            continue
        vertices=[projection.to_local(r['request_destination']) for r in items]
        parent=Polygon(vertices)
        if not parent.is_valid or parent.area<=5e-9:
            counts['request_degenerate']+=1
            continue
        footprint=parent.intersection(domain)
        values=[r['duration'] for r in items]
        requested.append((footprint,multipolygon(clip_triangle(vertices,values).intersection(footprint))))
        counts['baseline_triangles']+=1
        diagnostic=diagnose_triangle(items,layer['business_center'])
        counts['actual_'+diagnostic['status']]+=1
        if diagnostic['status']!='regular': continue
        moved=[projection.to_local(r['actual_destination']) for r in items]
        moved_support=Polygon(moved).intersection(footprint)
        reachable=multipolygon(clip_triangle(moved,values).intersection(moved_support))
        actual.append((moved_support,reachable))
    if cancelled(): raise ExperimentCancelled('Cancelled E2 output discarded')
    before,after=combine(requested,domain),combine(actual,domain)
    if after['support'].difference(before['support']).area>1e-6:
        raise ValueError('Candidate expanded support')
    return dict(requested=before,actual=after,counts=dict(counts))


def area_metrics(field,truth):
    geometry=field['geometry']
    union=truth.union(geometry).area
    return dict(TP_m2=geometry.intersection(truth).area,FP_m2=geometry.difference(truth).area,
        FN_m2=truth.difference(geometry).area,FN_unknown_m2=truth.intersection(field['unknown']).area,
        iou=geometry.intersection(truth).area/union if union else None)


def describe(pair):
    result={'counts':pair['counts']}
    for name in ('requested','actual'):
        field=pair[name];g=field['geometry']
        result[name]=dict(reachable_area_m2=g.area,support_area_m2=field['support'].area,
            unknown_area_m2=field['unknown'].area,components=len(g.geoms),holes=sum(len(p.interiors) for p in g.geoms),
            result_kind='null' if field['support'].is_empty else 'empty' if g.is_empty else 'polygon')
    b,a=pair['requested'],pair['actual'];common=b['support'].intersection(a['support'])
    result['comparison']=dict(lost_support_m2=b['support'].difference(a['support']).area,
        common_support_m2=common.area,
        common_became_reachable_m2=a['geometry'].difference(b['geometry']).intersection(common).area,
        common_became_unreachable_m2=b['geometry'].difference(a['geometry']).intersection(common).area)
    return result
