"""Research query contracts: API observations and model estimates stay distinct."""
import copy
import json
import math
import hashlib
from fractions import Fraction
from numbers import Real
import time
from pathlib import Path

import shapely
from shapely.geometry import Point,Polygon,mapping

from tools.endpoint_preserved_triangulation import observations,exact_structure
from tools.endpoint_stable_triangulation import exact_audit
from tools.endpoint_fresh_mesh import check_cancel


def valid_xy(xy):
    if (not isinstance(xy,(tuple,list)) or len(xy)!=2
        or any(not isinstance(v,Real) or isinstance(v,bool) or not math.isfinite(v) for v in xy)):
        raise ValueError('Invalid query coordinates')
    return tuple(xy)


def origin_key(origin):
    return json.dumps(origin,sort_keys=True,allow_nan=False)


class ObservationIndex:
    def __init__(self,records):
        snapshot=copy.deepcopy(records)
        nodes,conflicts=observations(snapshot,lambda:False)
        self._origin=origin_key(snapshot[0]['origin']) if snapshot else None
        self._points={tuple(n['actual']):n for n in nodes.values()}
        self._conflicts={tuple(r['xy']):r for r in conflicts}

    def lookup(self,xy,*,origin):
        xy=valid_xy(xy)
        if self._origin is not None and origin_key(origin)!=self._origin:
            raise ValueError('Incompatible query origin')
        if xy in self._conflicts:
            return dict(evidence='unknown',reason='conflicting_observations',duration_seconds=None,
                        source_ids=list(self._conflicts[xy]['record_ids']),confidence=None)
        point=self._points.get(xy)
        if point is None:return None
        return dict(evidence='observed',observed_kind='api_route_observation',duration_seconds=point['duration'],
                    source_ids=list(point['record_ids']),actual_origin=copy.deepcopy(point['origin']),
                    actual_xy=list(point['actual']),confidence=None)


def cross(a,b,c):
    return (b[0]-a[0])*(c[1]-a[1])-(b[1]-a[1])*(c[0]-a[0])


def fraction_json(value):
    return dict(numerator=str(value.numerator),denominator=str(value.denominator))


class QueryModel:
    """Immutable-by-copy research model; validates geometry before any query.

    Source records are supplied separately from the model snapshot. Radius is
    explicit, and None denotes the unfiltered candidate, not a confidence level.
    """
    def __init__(self,records,network,domain,*,radius,cancelled=lambda:False):
        check_cancel(cancelled)
        if radius is not None and (not isinstance(radius,Real) or not math.isfinite(radius) or radius<=0):
            raise ValueError('Invalid radius policy')
        # JSON/CLI numeric spelling must not change the identity of one policy.
        if radius is not None and radius == int(radius):radius = int(radius)
        if not domain.is_valid or domain.area<=0:raise ValueError('Invalid query domain')
        if network.get('status','').startswith('invalid_'):raise ValueError('Invalid model status')
        snapshot=copy.deepcopy(records);expected,conflicts=observations(snapshot,cancelled)
        nodes=copy.deepcopy(network['nodes'])
        if set(nodes)!=set(expected):raise ValueError('Model source node mismatch')
        for i,n in nodes.items():
            e=expected[i]
            if (list(n['actual'])!=list(e['actual']) or n['duration']!=e['duration']
                or sorted(n['record_ids'])!=e['record_ids'] or origin_key(n['origin'])!=origin_key(e['origin'])):
                raise ValueError('Model source binding mismatch')
        faces=[tuple(t) for t in network['all_triangles']]
        if any(len(t)!=3 or len(set(t))!=3 or any(i not in nodes for i in t) for t in faces):
            raise ValueError('Invalid model face indices')
        if faces:
            if not exact_structure(nodes,faces,cancelled=cancelled,deadline=time.monotonic()+300)['valid']:
                raise ValueError('Invalid model structure')
            if not exact_audit(nodes,faces,cancelled=cancelled)['valid']:raise ValueError('Invalid model empty circle')
        elif len(nodes)>=3:
            xy=[tuple(Fraction(v) for v in n['actual']) for n in nodes.values()]
            if any(cross(xy[0],xy[1],p) for p in xy[2:]):raise ValueError('Missing noncollinear model faces')
        supplied_radii=network.get('face_circumradii_m')
        if supplied_radii is not None and len(supplied_radii)!=len(faces):raise ValueError('Invalid model radius count')
        radius_by_face=dict(zip(faces,supplied_radii)) if supplied_radii is not None else {}
        self._nodes=nodes;self._faces=sorted(faces);self._domain=shapely.from_wkb(domain.wkb)
        self._observations=ObservationIndex(snapshot);self._radius=radius;self._failed=False
        self._origin=copy.deepcopy(snapshot[0]['origin']) if snapshot else None
        self._xy={i:tuple(Fraction(v) for v in n['actual']) for i,n in nodes.items()}
        self._values={i:Fraction(n['duration']) for i,n in nodes.items()}
        self._radii=[];self._max_edges=[]
        for t in self._faces:
            area=abs(cross(*(self._xy[i] for i in t)))/2
            if area==0:raise ValueError('Exact degenerate model face')
            lengths=[math.dist(nodes[t[a]]['actual'],nodes[t[b]]['actual']) for a,b in ((0,1),(1,2),(2,0))]
            r=math.prod(lengths)/(4*float(area))
            if not math.isfinite(r):raise ValueError('Unrepresentable model radius')
            if t in radius_by_face:
                frozen=radius_by_face[t]
                if not isinstance(frozen,Real) or not math.isfinite(frozen) or frozen<=0 or not math.isclose(frozen,r,rel_tol=1e-10,abs_tol=1e-10):
                    raise ValueError('Invalid frozen model radius')
                r=frozen
            self._radii.append(r);self._max_edges.append(max(lengths))
        self._kept={k for k,r in enumerate(self._radii) if radius is None or r<=radius}
        self._used={i for k in self._kept for i in self._faces[k]}
        polygons=[Polygon([nodes[i]['actual'] for i in t]) for t in self._faces]
        self._tree=shapely.STRtree(polygons)
        payload=dict(records=snapshot,faces=self._faces,face_radii=self._radii,domain=mapping(self._domain),radius=radius,coordinate_system='local_meters')
        # Record order is irrelevant to model identity as well as prediction.
        payload['records']=sorted(snapshot,key=lambda r:r['id'])
        self.model_id=hashlib.sha256(json.dumps(payload,sort_keys=True,allow_nan=False).encode()).hexdigest()
        check_cancel(cancelled)

    def _base(self,xy):
        return dict(model_id=self.model_id,query_xy=list(xy),coordinate_system='local_meters',
            actual_origin=copy.deepcopy(self._origin),radius_policy=self._radius,confidence=None,
            confidence_status='uncalibrated',duration_reference='common_actual_origin')

    def _unknown(self,base,reason,**extra):
        return dict(base,evidence='unknown',reason=reason,duration_seconds=None,within_threshold=None,
                    source_ids=[],has_interpolation_support=False,**extra)

    def query(self,xy,*,origin,coordinate_system='local_meters',cancelled=lambda:False):
        check_cancel(cancelled)
        if self._failed:raise ValueError('Model blocked after query inconsistency')
        if coordinate_system!='local_meters':raise ValueError('Incompatible coordinate system')
        xy=valid_xy(xy)
        # Validate the origin even if the location is outside the query domain.
        observed=self._observations.lookup(xy,origin=origin);base=self._base(xy)
        if not self._domain.covers(Point(xy)):
            result=self._unknown(base,'outside_domain')
        elif observed is not None:
            result=dict(base,**observed)
            result['within_threshold']=None if observed['duration_seconds'] is None else observed['duration_seconds']<=900
            result['has_interpolation_support']=any(tuple(self._nodes[i]['actual'])==xy for i in self._used)
        else:
            q=tuple(Fraction(v) for v in xy);candidates=[];contained=False
            for k in sorted(int(v) for v in self._tree.query(Point(xy))):
                check_cancel(cancelled)
                t=self._faces[k];a,b,c=(self._xy[i] for i in t);den=cross(a,b,c)
                weights=(cross(q,b,c)/den,cross(a,q,c)/den,cross(a,b,q)/den)
                if any(w<0 for w in weights):continue
                contained=True
                if k not in self._kept:continue
                if any(w.numerator.bit_length()+w.denominator.bit_length()>8192 for w in weights):
                    result=self._unknown(base,'numerical_unresolved');break
                value=sum((w*self._values[i] for w,i in zip(weights,t)),Fraction(0))
                candidates.append((k,weights,value))
            else:
                if not candidates:
                    reason='insufficient_geometry' if not self._faces else 'radius_filtered' if contained else 'outside_convex_hull'
                    result=self._unknown(base,reason)
                else:
                    if len({value for _,_,value in candidates})!=1:
                        self._failed=True;raise ValueError('Shared-face value inconsistency')
                    k,weights,value=candidates[0];t=self._faces[k]
                    duration=float(value)
                    if not math.isfinite(duration):result=self._unknown(base,'numerical_unresolved')
                    else:
                        result=dict(base,evidence='interpolated',reason=None,duration_seconds=duration,
                            within_threshold=value<=900,duration_exact=fraction_json(value),
                            has_interpolation_support=True,triangle_id=f't{k:05d}',vertex_ids=list(t),
                            source_ids=sorted(i for v in t for i in self._nodes[v]['record_ids']),
                            vertex_source_ids=[list(self._nodes[v]['record_ids']) for v in t],
                            weights=[float(w) for w in weights],weights_exact=[fraction_json(w) for w in weights],
                            face_radius_m=self._radii[k],max_edge_m=self._max_edges[k],
                            time_span_seconds=max(self._nodes[v]['duration'] for v in t)-min(self._nodes[v]['duration'] for v in t),
                            matching_retained_faces=len(candidates))
        check_cancel(cancelled);return result

    def query_many(self,points,*,origin,coordinate_system='local_meters',cancelled=lambda:False):
        check_cancel(cancelled)
        results=[self.query(p,origin=origin,coordinate_system=coordinate_system,cancelled=cancelled) for p in points]
        check_cancel(cancelled);return results


def load_snapshot(path,records,domain,*,radius,expected_sha256,cancelled=lambda:False):
    check_cancel(cancelled);content=Path(path).read_bytes()
    if hashlib.sha256(content).hexdigest()!=expected_sha256:raise ValueError('Model snapshot hash mismatch')
    return QueryModel(records,json.loads(content),domain,radius=radius,cancelled=cancelled)


if __name__=='__main__':
    import argparse
    from shapely.geometry import box
    from tools.diagnostic_common import no_network
    parser=argparse.ArgumentParser(description='Offline actual-endpoint query; explicit frozen model hash required')
    parser.add_argument('--model',required=True);parser.add_argument('--model-sha256',required=True)
    parser.add_argument('--records',required=True);parser.add_argument('--queries',required=True)
    parser.add_argument('--domain',nargs=4,type=float,required=True)
    parser.add_argument('--radius',required=True,help='metres or unlimited')
    parser.add_argument('--output',required=True);args=parser.parse_args()
    destination=Path(args.output)
    if destination.exists():raise FileExistsError('Refuse to overwrite query output')
    with no_network():
        records=json.loads(Path(args.records).read_text(encoding='utf-8'))
        queries=json.loads(Path(args.queries).read_text(encoding='utf-8'))
        model=load_snapshot(args.model,records,box(*args.domain),radius=None if args.radius=='unlimited' else float(args.radius),
                            expected_sha256=args.model_sha256)
        results=model.query_many(queries['points'],origin=queries['origin'],coordinate_system=queries['coordinate_system'])
        with destination.open('x',encoding='utf-8') as stream:
            json.dump(dict(model_id=model.model_id,results=results),stream,ensure_ascii=False,indent=2,allow_nan=False)
