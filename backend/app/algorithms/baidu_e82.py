"""Adapt the team's E8.2 core to task/POI contracts without changing its evidence."""
import math
from dataclasses import replace

from shapely.geometry import MultiPolygon, box, shape
from shapely.ops import transform

from life_circle.coordinates import LocalProjection
from life_circle.models import IsochroneResult, Statistics
from life_circle.providers import AnalyticProvider
from tools.endpoint_geometry import business_geometry
from tools.endpoint_multicross_boundary import compute_multicross_boundary

ALGORITHM = 'local-multicross-e82'
# The algorithm identity stays the same; the refinement is versioned in the
# request's config_version. E8.2.1 is the closed-loop refinement.
REFINEMENT_VERSIONS = {'legacy': ALGORITHM, 'loop': 'local-multicross-e82.1'}
DEFAULT_REFINEMENT = 'loop'


class EndpointAnalyticProvider(AnalyticProvider):
    """Synthetic points have exact endpoints; never fabricate real route endpoints."""
    async def query_walking_time(self, origin, destination, deadline):
        observation = await super().query_walking_time(origin, destination, deadline)
        return replace(observation, request_origin=origin, route_origin=origin,
                       route_destination=destination, origin_offset_m=0, destination_offset_m=0)


# 一次成圈里每条观测被接收还是被拒收。求解器运行时一直记着这些行，但修订此前只带走了
# 调度器的传输计数 —— 于是一次没有圈面的运行只留下一个 ``budget``。两次真实体检因此
# 无法分辨"上游没回端点"、"回来的端点对不上起点"和"边界本身没收敛"。这里只把已经记下
# 的东西数出来，不新增任何判断：诊断不改变判定。
def _tally(values) -> dict:
    """把取值数成计数，键按字典序固定；``None`` 归到 ``none``（没有要报的原因）。"""
    counts: dict[str, int] = {}
    for value in values:
        key = 'none' if value is None else str(value)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def _finite(value):
    """有限数或 None：NaN 与 Infinity 不是可发布的数（契约里 allow_inf_nan=False）。"""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value) if math.isfinite(float(value)) else None


def _ring_assembly(raw) -> dict | None:
    """环面合没合起来，没合起来是为什么；旧修订没有这一项，读作"未记录"。

    这句话求解器一直算着（相邻已定方向之间的最大角隙，以及角隙大到不能连成环时的具名
    原因），此前却不发布：一次没有几何的运行只剩一个 ``budget``。它不含坐标。
    """
    if not isinstance(raw, dict):
        return None
    nodes = raw.get('localizedNodes')
    return {
        'assembled': raw.get('assembled') if isinstance(raw.get('assembled'), bool) else None,
        'reason': raw.get('reason') if isinstance(raw.get('reason'), str) else None,
        'maxGapRadians': _finite(raw.get('maxGapRadians')),
        'localizedNodes': None if isinstance(nodes, bool) or not isinstance(nodes, int) else nodes,
    }


def boundary_diagnostics(raw) -> dict:
    """成圈这一趟接收／拒收了什么、各方向的结果，以及那个环是怎么合起来的。只计数，不含坐标。"""
    log = [row for row in (raw.get('observationEvidence') or []) if isinstance(row, dict)]
    rejected = [row for row in log if not row.get('accepted')]
    origins = {tuple(row['route_origin']) for row in log
               if isinstance(row.get('route_origin'), (list, tuple))}
    directions = [row for row in (raw.get('directions') or []) if isinstance(row, dict)]
    completion = raw.get('completion') or {}
    return {
        'observations': {
            'logged': len(log), 'accepted': len(log) - len(rejected), 'rejected': len(rejected),
            'rejectedReasons': _tally(row.get('rejected') for row in rejected),
            'kinds': _tally(row.get('kind') for row in log),
            # 实际起点有几个：多过一个就说明上游把同一次体检的路线吸到了不同的路上，而
            # 求解器只采纳第一个起点的一致性证据 —— 这正是"每次都回了结果、却没有圈面"
            # 的一种成因，只靠传输计数看不出来。
            'actualOriginGroups': len(origins),
        },
        'directions': {
            'total': len(directions),
            'byStatus': _tally(row.get('status') for row in directions),
            'byReason': _tally(row.get('reason') for row in directions),
        },
        'ringAssembly': _ring_assembly(raw.get('ringAssembly')),
        'completion': {key: completion.get(key) for key in
                       ('reason', 'resolutionReached', 'budgetExhausted', 'unresolvedEdges',
                        'pendingUnattemptedEdges', 'pendingScanRays',
                        'initialUnfinishedDirections', 'runComplete')},
    }


async def compute_e82(request, provider, token, *, on_progress=None, refinement=None):
    if refinement is None:
        # The request names its refinement; an older version string means legacy.
        refinement = 'loop' if request.config_version == REFINEMENT_VERSIONS['loop'] else 'legacy'
    raw = await compute_multicross_boundary(request, provider, token,
        allow_network=provider.network, on_progress=on_progress, refinement=refinement)
    projection = LocalProjection(request.origin)
    domain = box(-request.extent, -request.extent, request.extent, request.extent)
    empty = business_geometry(MultiPolygon(), projection)

    def local(geometry):
        if geometry is None:
            return None
        return transform(lambda x, y, z=None: projection.to_local((x, y)), shape(geometry))

    geometry = raw['geometry']
    local_geometry = local(geometry)
    established = local_geometry is not None and not local_geometry.is_empty
    # Without a boundary nothing is established: the whole square is unresolved,
    # never inferred to be unreachable. With one, the region is what the solver
    # reports as unresolved; a run that reports none has none.
    unknown = raw.get('unknownRegion')
    if not established:
        unknown = business_geometry(domain, projection)
    elif unknown is None:
        unknown = empty
    local_unknown = local(unknown)
    stats = Statistics(**raw['_statistics'])
    stats.unknown_area = local_unknown.area
    # 没有环面的那一趟，未决边数**没有被量过**（那两个数只在补丁窗口内量，而没有环面时
    # 窗口是空的，量出来只会是 0），因此这里是 None 而不是 0。旧 refinement 路径
    # （/api/analyses）给得出数字，所以旧契约照旧拿到 int。
    stats.unfinished_boundary = raw['completion']['unresolvedEdges']
    warnings = ['e82_experimental_estimate', 'interior_not_independently_verified']
    if raw.get('truncated'):
        warnings.append('range_truncated')
    if raw['quality'] == 'experimental_evidence_conflict':
        warnings.append('known_negative_inside_estimate')
    loop = raw.get('refinementLoop') or {}
    if loop.get('carvedNegatives'):
        warnings.append('known_negative_carved')
    if raw.get('status') == 'cancelled' or raw['completion'].get('runComplete') is False:
        warnings.append('run_incomplete')
    quality = 'partial' if established else 'insufficient'
    return IsochroneResult(
        geometry=geometry, uncertain_region=raw.get('uncertaintyBand') or empty,
        unknown_region=unknown, computation_extent=business_geometry(domain, projection),
        quality=quality, stop_reason=raw['completion']['reason'], statistics=stats,
        warnings=warnings, config=request, local_geometry=local_geometry,
        local_unknown=local_unknown, time_bands=[dict(minutes=15, geometry=geometry)],
        sample_observations=raw['_observations'],
        metadata=dict(algorithm=ALGORITHM, validationStatus='not_independently_validated',
            completion=raw['completion'], assumption=raw['assumption'],
            boundaryDiagnostics=boundary_diagnostics(raw),
            unknownRegionMeaning=('solver-unresolved region; may overlap geometry; not an error bound'
                                  if established else 'no boundary established'),
            unresolvedArea=dict(insideM2=local_unknown.intersection(local_geometry).area if established else 0,
                                outsideM2=local_unknown.difference(local_geometry).area if established
                                else local_unknown.area),
            evidence={k: v for k, v in raw.items() if not k.startswith('_')}))
