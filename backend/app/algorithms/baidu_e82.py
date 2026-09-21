"""Adapt the team's E8.2 core to task/POI contracts without changing its evidence."""
from dataclasses import replace

from shapely.geometry import MultiPolygon, box, shape
from shapely.ops import transform

from life_circle.coordinates import LocalProjection
from life_circle.models import IsochroneResult, Statistics
from life_circle.providers import AnalyticProvider
from tools.endpoint_geometry import business_geometry
from tools.endpoint_multicross_boundary import compute_multicross_boundary

ALGORITHM = 'local-multicross-e82'


class EndpointAnalyticProvider(AnalyticProvider):
    """Synthetic points have exact endpoints; never fabricate real route endpoints."""
    async def query_walking_time(self, origin, destination, deadline):
        observation = await super().query_walking_time(origin, destination, deadline)
        return replace(observation, request_origin=origin, route_origin=origin,
                       route_destination=destination, origin_offset_m=0, destination_offset_m=0)


async def compute_e82(request, provider, token, *, on_progress=None):
    raw = await compute_multicross_boundary(request, provider, token,
        allow_network=provider.network, on_progress=on_progress)
    projection = LocalProjection(request.origin)
    domain = box(-request.extent, -request.extent, request.extent, request.extent)
    empty = business_geometry(MultiPolygon(), projection)

    def local(geometry):
        if geometry is None:
            return None
        return transform(lambda x, y, z=None: projection.to_local((x, y)), shape(geometry))

    geometry = raw['geometry']
    local_geometry = local(geometry)
    # Only E8.2 explicitly labels unknown faces. Missing support is unknown,
    # never inferred to be unreachable from the absence of a polygon.
    unknown = raw.get('unknownRegion')
    if unknown is None:
        unknown = business_geometry(domain, projection)
    local_unknown = local(unknown)
    stats = Statistics(**raw['_statistics'])
    stats.unknown_area = local_unknown.area
    stats.unfinished_boundary = raw['completion']['unresolvedEdges']
    warnings = ['e82_experimental_estimate', 'interior_not_independently_verified']
    if raw.get('truncated'):
        warnings.append('range_truncated')
    if raw['quality'] == 'experimental_evidence_conflict':
        warnings.append('known_negative_inside_estimate')
    quality = 'insufficient' if local_geometry is None or local_geometry.is_empty else 'partial'
    return IsochroneResult(
        geometry=geometry, uncertain_region=raw.get('uncertaintyBand') or empty,
        unknown_region=unknown, computation_extent=business_geometry(domain, projection),
        quality=quality, stop_reason=raw['completion']['reason'], statistics=stats,
        warnings=warnings, config=request, local_geometry=local_geometry,
        local_unknown=local_unknown, time_bands=[dict(minutes=15, geometry=geometry)],
        sample_observations=raw['_observations'],
        metadata=dict(algorithm=ALGORITHM, validationStatus='not_independently_validated',
            completion=raw['completion'], assumption=raw['assumption'],
            evidence={k: v for k, v in raw.items() if not k.startswith('_')}))
