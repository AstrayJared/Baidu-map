"""Controlled E5 integration of the verified actual-endpoint research model."""
from collections import Counter, defaultdict
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path

from shapely.geometry import box, MultiPolygon
from life_circle.coordinates import LocalProjection, normalize, RADIUS
from life_circle.field import business_geometry, multipolygon
from tools.endpoint_preserved_triangulation import preserved_network
from tools.endpoint_query import QueryModel
from tools.endpoint_fresh_mesh import export_result, check_cancel

VERSION = 'actual-endpoints-e5-v1'


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, allow_nan=False).encode()).hexdigest()


class EndpointModel:
    def __init__(self, records, network, metadata, observations, cancelled=lambda:False):
        self.records, self.network, self.metadata, self.observations = records, network, metadata, observations
        self.projection = LocalProjection(tuple(metadata['projection']['origin']))
        self.domain = box(*metadata['domain'])
        self.model = QueryModel(records, network, self.domain, radius=metadata['radiusM'], cancelled=cancelled)
        self.origin_key = records[0]['origin'] if records else None
        self.metadata['modelId'] = self.model.model_id

    def query_bd09(self, point, cancelled=lambda:False):
        # Validate without applying normalize's rounding to the actual point.
        if any(type(v) not in (int,float) for v in point):raise ValueError('Invalid coordinates')
        normalize(point)
        result = self.model.query(self.projection.to_local(point), origin=self.origin_key, cancelled=cancelled)
        return dict(result, inputCoordinateSystem='bd09ll', inputPoint=list(point), projection=self.metadata['projection'])

    def save(self, path):
        payload = dict(version=VERSION, records=self.records, network=export_result(self.network),
                       metadata=self.metadata, observations=self.observations)
        path=Path(path);path.parent.mkdir(parents=True, exist_ok=True)
        with path.open('x', encoding='utf-8') as stream:
            json.dump(dict(payload=payload, sha256=digest(payload)), stream, ensure_ascii=False, allow_nan=False)
            stream.flush();os.fsync(stream.fileno())

    def apply(self, result):
        field=self.network.get('field')
        support=field['support'] if field else MultiPolygon()
        geometry=field['geometry'] if field else multipolygon(support)
        unknown=field['unknown'] if field else self.domain
        result.geometry=None if support.is_empty else business_geometry(geometry,self.projection)
        result.local_geometry=None if support.is_empty else geometry
        result.local_unknown=unknown
        result.unknown_region=business_geometry(unknown,self.projection)
        result.unreachable_region=None if support.is_empty else business_geometry(support.difference(geometry),self.projection)
        result.uncertain_region=business_geometry(support,self.projection)
        result.computation_extent=business_geometry(self.domain,self.projection)
        result.statistics.unknown_area=unknown.area
        result.quality='insufficient' if support.is_empty else 'partial'
        result.warnings=sorted(set(result.warnings+['endpoint_model_estimate','radius_policy_unvalidated']))
        if not geometry.is_empty and geometry.distance(self.domain.boundary)<1e-7:
            result.warnings=sorted(set(result.warnings+['range_truncated']))
        result.time_bands=[dict(minutes=15,geometry=result.geometry)]
        return result


def build_endpoint_model(observations, origin, *, extent, radius, synthetic, cancelled=lambda:False):
    check_cancel(cancelled)
    projection=LocalProjection(origin);groups=defaultdict(list);excluded=Counter();evidence=[]
    for index,obs in enumerate(observations):
        check_cancel(cancelled)
        row={k:asdict(obs)[k] for k in ['destination','duration','reason','observed_duration','endpoint_verified',
             'route_origin','route_destination','request_origin','origin_offset_m','destination_offset_m','attempts']}
        row['id']=f'route-{index:05d}';evidence.append(row)
        if obs.attempts==0 and obs.duration==0 and obs.destination==tuple(origin) and obs.route_origin is None:
            excluded['derived_origin_anchor']+=1;continue
        if obs.observed_duration is None or obs.reason not in (None,'endpoint_offset'):
            excluded['invalid_measurement']+=1;continue
        start=obs.route_origin;end=obs.route_destination
        if synthetic:start=start or origin;end=end or obs.destination
        if start is None or end is None or (not synthetic and not obs.endpoint_verified):
            excluded['missing_endpoints']+=1;continue
        try:normalize(start);normalize(end)
        except (ValueError,TypeError):excluded['invalid_endpoints']+=1;continue
        key=json.dumps([list(obs.request_origin or origin),list(start)],sort_keys=True)
        groups[key].append(dict(id=row['id'],xy=list(projection.to_local(end)),duration=obs.observed_duration,origin=json.loads(key)))
    selected=min(groups,key=lambda k:(-len(groups[k]),k)) if groups else None
    records=groups[selected] if selected is not None else []
    excluded['different_origin']=sum(len(v) for k,v in groups.items() if k!=selected)
    domain=box(-extent,-extent,extent,extent)
    network=preserved_network(records,domain,radius=radius,cancelled=cancelled)
    if network['status'].startswith('invalid_'):raise ValueError('Actual endpoint geometry invalid')
    metadata=dict(version=VERSION,modelType='actual_endpoints',radiusM=radius,
        projection=dict(name='LocalProjection',version='local-equirectangular-v1',coordinateSystem='bd09ll',
                        origin=list(origin),earthRadiusM=RADIUS,sx=projection.sx,sy=projection.sy),
        domain=[-extent,-extent,extent,extent],selectedOrigin=json.loads(selected) if selected else None,
        selectionRule='largest exact origin cohort; lexicographic tie break',cohorts=len(groups),
        records=len(records),points=network['points'],conflicts=len(network['conflicts']),excluded=dict(excluded),
        synthetic=synthetic,confidence=None,accuracyValidated=False)
    check_cancel(cancelled)
    return EndpointModel(records,network,metadata,evidence,cancelled)


def load_endpoint_model(path):
    envelope=json.loads(Path(path).read_text(encoding='utf-8'));p=envelope['payload']
    if envelope['sha256']!=digest(p) or p['version']!=VERSION:raise ValueError('Invalid endpoint snapshot')
    projection=p['metadata']['projection'];expected=LocalProjection(tuple(projection['origin']))
    if projection['sx']!=expected.sx or projection['sy']!=expected.sy or projection['earthRadiusM']!=RADIUS:
        raise ValueError('Incompatible projection metadata')
    expected_id=p['metadata']['modelId']
    model=EndpointModel(p['records'],p['network'],p['metadata'],p['observations'])
    if model.model.model_id!=expected_id:raise ValueError('Model identity mismatch')
    return model
