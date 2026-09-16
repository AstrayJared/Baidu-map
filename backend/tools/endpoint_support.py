"""E2.1 offline geometry experiment; never repairs or queries a route."""
from collections import Counter, defaultdict
import copy
from itertools import combinations

import numpy as np
import shapely
from shapely.geometry import Polygon, Point, LineString, GeometryCollection
from shapely.strtree import STRtree

from life_circle.coordinates import LocalProjection
from life_circle.field import multipolygon
from tools.endpoint_observations import ExperimentCancelled
from tools.endpoint_reconstruction import combine
from tools.fixed_evidence_representation import clip_triangle

EPS = 1e-6


def register_nodes(entries):
    nodes = {}
    for identity, node in entries:
        if identity in nodes and any(nodes[identity][k] != node[k]
                                     for k in ('request','actual','duration','origin')):
            nodes[identity]['mapping_conflict'] = True
        elif identity not in nodes:
            nodes[identity] = copy.deepcopy(node)
    return nodes


def check_cancel(cancelled):
    if cancelled():
        raise ExperimentCancelled('E2.1 cancelled; no candidate published')


def signed(points):
    a, b, c = points
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def faces(nodes, triangles):
    result = []
    for ids in sorted(map(tuple, triangles)):
        request = [nodes[i]['request'] for i in ids]
        actual = [nodes[i]['actual'] for i in ids]
        before, after = signed(request), signed(actual)
        status = ('degenerate' if min(abs(before), abs(after)) <= 1e-8
                  else 'flipped' if before*after < 0 else 'regular')
        result.append(dict(ids=ids, request=Polygon(request), actual=Polygon(actual),
                           status=status, values=[nodes[i]['duration'] for i in ids]))
    return result


def intersecting_pairs(polygons):
    tree = STRtree(polygons)
    for i, polygon in enumerate(polygons):
        for j in sorted(map(int, tree.query(polygon, predicate='intersects'))):
            if j > i:
                yield i, j, polygon.intersection(polygons[j])


def audit_mesh(nodes, triangles, domain, *, cancelled=lambda: False):
    check_cancel(cancelled)
    fs = faces(nodes, triangles)
    support = shapely.union_all([f['request'] for f in fs]).intersection(domain)
    unknown = domain.difference(support)
    issues = []
    def issue(kind, **data):
        issues.append(dict(kind=kind, **data))
    if len({str(n['origin']) for n in nodes.values()}) != 1:
        issue('mixed_origins')
    locations = defaultdict(list)
    for identity, node in sorted(nodes.items()):
        if node.get('conflict'):
            issue('observation_conflict', node=identity)
        if node.get('mapping_conflict'):
            issue('mapping_conflict', node=identity)
        locations[tuple(node['actual'])].append(identity)
    for ids in locations.values():
        if len(ids) > 1:
            issue('node_collision', nodes=ids)
            if len({nodes[i]['duration'] for i in ids}) > 1:
                issue('observation_conflict', nodes=ids)
    components = list(multipolygon(support).geoms)
    edges = defaultdict(list)
    for i, f in enumerate(fs):
        check_cancel(cancelled)
        if f['status'] != 'regular':
            issue(f['status'], triangle=i, nodes=f['ids'])
        for edge in combinations(f['ids'], 2):
            edges[tuple(sorted(edge))].append(i)
        if f['actual'].is_valid:
            crossed = f['actual'].intersection(unknown).area
            if crossed > EPS:
                issue('unknown_crossing', triangle=i, area_m2=crossed)
            owner = next((j for j, c in enumerate(components)
                          if c.covers(f['request'].representative_point())), None)
            for j, c in enumerate(components):
                if j != owner and f['actual'].intersection(c).area > EPS:
                    issue('component_crossing', triangle=i, component=j)
    for edge, adjacent in sorted(edges.items()):
        if len(adjacent) > 2:
            issue('nonmanifold_edge', nodes=edge, triangles=adjacent)
    # Check both embeddings; no union is used to hide overlaps or T junctions.
    for side in ('request', 'actual'):
        for i, j, intersection in intersecting_pairs([f[side] for f in fs]):
            check_cancel(cancelled)
            shared = sorted(set(fs[i]['ids']) & set(fs[j]['ids']))
            if intersection.area > EPS:
                issue('overlap' if side == 'actual' else 'request_overlap',
                      triangles=[i,j], area_m2=intersection.area)
            if len(shared) == 2:
                expected = LineString([nodes[k][side] for k in shared])
            elif len(shared) == 1:
                expected = Point(nodes[shared[0]][side])
            else:
                expected = GeometryCollection()
            extra = intersection.difference(expected)
            if not extra.is_empty and (extra.area > EPS or extra.length > 1e-8
                                        or extra.geom_type in ('Point','MultiPoint')):
                issue('unexpected_intersection', side=side, triangles=[i,j])
    max_edge_error = 0.0
    for edge, adjacent in edges.items():
        if len(adjacent) != 2 or any(fs[i]['status'] != 'regular' for i in adjacent):
            continue
        coefficients = [np.linalg.solve(
            np.array([[*nodes[k]['actual'],1] for k in fs[i]['ids']]),fs[i]['values'])
            for i in adjacent]
        a, b = (np.asarray(nodes[k]['actual']) for k in edge)
        for t in (0,.25,.5,.75,1):
            p = np.r_[a*(1-t)+b*t,1]
            max_edge_error = max(max_edge_error, abs(float(p@coefficients[0]-p@coefficients[1])))
    if max_edge_error > EPS:
        issue('shared_edge_value_conflict', max_seconds=max_edge_error)
    check_cancel(cancelled)
    return dict(valid=not issues, counts=dict(Counter(i['kind'] for i in issues)),
                issues=issues, faces=len(fs), shared_edge_max_error_seconds=max_edge_error)


def compare(nodes, triangles, domain, *, cancelled=lambda: False):
    audit = audit_mesh(nodes, triangles, domain, cancelled=cancelled)
    if not audit['valid']:
        kinds = set(audit['counts'])
        status = ('blocked_unknown_crossing' if kinds <= {'unknown_crossing','component_crossing'}
                  else 'blocked_invalid_mesh')
        return dict(status=status, audit=audit, A=None, B=None, C=None)
    fs = faces(nodes, triangles)
    s0 = shapely.union_all([f['request'] for f in fs]).intersection(domain)
    parts = {k:[] for k in 'ABC'}
    for f in fs:
        check_cancel(cancelled)
        request = f['request'].intersection(domain)
        local = f['actual'].intersection(request)
        global_support = f['actual'].intersection(s0)
        req_reachable = clip_triangle([nodes[i]['request'] for i in f['ids']], f['values'])
        actual_reachable = clip_triangle([nodes[i]['actual'] for i in f['ids']], f['values'])
        for key, support, reachable in [('A',request,req_reachable),('B',local,actual_reachable),
                                         ('C',global_support,actual_reachable)]:
            parts[key].append((support,multipolygon(reachable.intersection(support))))
    result = {k:combine(v,domain) for k,v in parts.items()}
    check_cancel(cancelled)
    return dict(status='completed',audit=audit,**result)


def from_layer(layer, triangles):
    """Preserve E2 evidence exclusions and original node identities."""
    projection = LocalProjection(layer['business_center'])
    records = {r['id']:r for r in layer['records']}
    if len(records) != len(layer['records']):
        raise ValueError('Duplicate record identity')
    blocked = {i for p in layer['points'] if p['conflict'] for i in p['record_ids']}
    kept, excluded = [], []
    for ids in triangles:
        if any(i not in records or not records[i]['usable'] or i in blocked for i in ids):
            excluded.append(ids)
        else:
            kept.append(tuple(ids))
    nodes = {}
    for identity in sorted({i for t in kept for i in t}):
        r = records[identity]
        nodes[identity] = dict(request=projection.to_local(r['request_destination']),
                               actual=projection.to_local(r['actual_destination']), duration=r['duration'],
                               origin=(tuple(r['request_origin']),tuple(r['actual_origin'])))
    return nodes, kept, excluded


def partition_masks(region, masks):
    parts = {'none':region}
    for name, mask in masks:
        new = {}
        for label, part in parts.items():
            yes, no = part.intersection(mask), part.difference(mask)
            if not no.is_empty: new[label] = no
            if not yes.is_empty and yes.area > 0:
                new[name if label == 'none' else label+'+'+name] = yes
        parts = new
    return parts


def loss_partition(s0, sb, moved, degenerate, flipped, crossing, domain):
    """Coverage attribution only: moved union is NOT accepted prediction support."""
    union = shapely.union_all(moved)
    geometries = dict(L_total=s0.difference(sb), L_clip=s0.intersection(union).difference(sb),
                      L_uncovered=s0.difference(union))
    overlap = shapely.union_all([g for i,j,g in intersecting_pairs(moved) if g.area > 0])
    clip_parts = partition_masks(geometries['L_clip'], [('overlap',overlap),
                                ('unknown_crossing',shapely.union_all(crossing))])
    uncovered_parts = partition_masks(geometries['L_uncovered'],
                        [('degenerate',shapely.union_all(degenerate)),('flipped',shapely.union_all(flipped))])
    boundaries = defaultdict(list)
    inner = domain.difference(s0).boundary.difference(domain.boundary)
    for g in multipolygon(geometries['L_uncovered']).geoms:
        outer_touch = g.intersects(domain.boundary)
        inner_touch = g.intersects(inner)
        key = 'both' if outer_touch and inner_touch else 'outer' if outer_touch else 'inner' if inner_touch else 'neither'
        boundaries[key].append(g)
    boundary_parts = {k:shapely.union_all(v) for k,v in boundaries.items()}
    errors = [geometries['L_total'].symmetric_difference(
        geometries['L_clip'].union(geometries['L_uncovered'])).area]
    for region, parts in [(geometries['L_clip'],clip_parts),
                          (geometries['L_uncovered'],uncovered_parts),
                          (geometries['L_uncovered'],boundary_parts)]:
        merged = shapely.union_all(list(parts.values()))
        errors += [merged.symmetric_difference(region).area, abs(sum(g.area for g in parts.values())-merged.area)]
    if max(errors) > EPS:
        raise ValueError('Loss partition does not close')
    return dict(geometries=geometries,clip_parts=clip_parts,uncovered_parts=uncovered_parts,
                boundary_parts=boundary_parts,closure_error_m2=max(errors))
