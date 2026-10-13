"""E8.2 compute-only adapter: the boundary core without its facility stage.

The legacy manager runs ``analyze_facilities`` right after ``compute_e82``. That
full pipeline is deliberately not reused here; a checkup retrieves facilities
once, in its own stage, under its own budget.
"""
import logging
import math
from contextlib import AsyncExitStack

import httpx
from life_circle.models import IsochroneRequest
from life_circle.providers import BaiduProvider

from ..algorithms.baidu_e82 import (ALGORITHM, DEFAULT_REFINEMENT, REFINEMENT_VERSIONS,
                                    EndpointAnalyticProvider, compute_e82)
from ..analyses import LimitedProvider, effective_qps
from ..baidu import silence_transport_logs
from ..persistence import atomic_dump
from .protocol import EngineCapabilities, EngineContext, IsochroneAsk, IsochroneSnapshot

logger = logging.getLogger(__name__)

# The boundary core is tuned for this request shape; a checkup never overrides it.
MAX_EXTENT_M = 1600
BUDGET_TIERS = (200, 400, 800)
#: The synthetic stand-in's walking speed: time is straight-line distance at this
#: speed, so every boundary it draws is a circle of 900 s × 1.2 m/s = 1080 m.
SYNTHETIC_SPEED_MPS = 1.2
SYNTHETIC_NOTES = [
    "当前为离线合成模式（ANALYSIS_PROVIDER=synthetic）：边界按直线距离算出，"
    "是半径约 1080 米的正圆，不是百度实测",
    "合成模式下设施检索、服务覆盖与核验都不运行",
    "切回百度模式：去掉 ANALYSIS_PROVIDER 环境变量（或设为 baidu），重启后端",
]


def e82_request(origin, budget: int, *, qps: float | None = None,
                refinement: str = DEFAULT_REFINEMENT) -> IsochroneRequest:
    """The one request shape production uses; offline benchmarks build it here too.

    ``config_version`` names the refinement, so a result says which one drew it and
    enters the result hash with it. The 800 tier gets a longer deadline: at a
    conservative 2 QPS its attempts alone take about seven minutes.
    """
    return IsochroneRequest(
        origin, "bd09ll", budget=budget, max_extent=MAX_EXTENT_M, expand=False,
        time_bands=(15,), config_version=REFINEMENT_VERSIONS[refinement], qps=qps,
        deadline_seconds=600 if budget <= 400 else 1200)


#: 逐条观测证据的文件名。它与修订文件同住一个任务明细目录，也就与它们同时到期：
#: 保留期一到，整份证据和修订一起消失，任何汇总里都不会留下一条指向已删文件的路径。
EVIDENCE_NAME = 'boundary-observation-evidence.json'
EVIDENCE_SCHEMA = 'boundary-observation-evidence-v1'


def _coordinate(value):
    """一个坐标对，或 None。其余形状（缺字段、字符串、布尔、非有限数）一律不写进证据。"""
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        return None
    if any(isinstance(item, bool) or not isinstance(item, (int, float)) for item in value):
        return None
    if any(not math.isfinite(float(item)) for item in value):
        return None
    return [float(value[0]), float(value[1])]


def observation_evidence(evidence, *, task_id: str, engine_version: str, statistics: dict) -> dict:
    """逐条观测的落盘形态：问了什么、回了什么、被接收还是被哪个原因拒收。

    这里**不投影求解器的全部证据**。几何（候选面、未知区、不确定带）、负证据、AK、
    请求 URL 与原始 HTTP 响应都不在写入范围内；方向只留角度、状态与原因 —— 完整的方向
    记录里带着每一次二分的历史与坐标，那是几何的另一种写法。
    """
    rows = []
    for row in evidence.get('observationEvidence') or []:
        if not isinstance(row, dict):
            continue
        rows.append({
            'id': row.get('id'), 'kind': row.get('kind'),
            'accepted': bool(row.get('accepted')), 'rejection': row.get('rejected'),
            'outcomeReason': row.get('reason'),
            'requestOrigin': _coordinate(row.get('request_origin')),
            'requestDestination': _coordinate(row.get('destination')),
            'actualOrigin': _coordinate(row.get('route_origin')),
            'actualDestination': _coordinate(row.get('route_destination')),
            'durationSeconds': row.get('observed_duration'),
            'endpointVerified': bool(row.get('endpoint_verified')),
            'attempts': row.get('attempts'),
            'destinationOffsetM': row.get('destination_offset_m'),
        })
    directions = [{'angle': row.get('angle'), 'status': row.get('status'),
                   'reason': row.get('reason')}
                  for row in (evidence.get('directions') or []) if isinstance(row, dict)]
    return {'schemaVersion': EVIDENCE_SCHEMA, 'taskId': task_id, 'engineId': 'baidu_e82',
            'engineVersion': engine_version,
            'requests': statistics.get('requests'),
            'networkRequests': statistics.get('network_requests'),
            'observations': rows, 'directions': directions,
            'completion': evidence.get('completion') or {}}


def save_observation_evidence(context, payload: dict, statistics: dict) -> str | None:
    """把这一趟的逐条观测证据写进任务明细目录；写了就返回文件名，没写返回 None。

    写失败不改结论：这次成圈已经付过费、也已经算出结果，证据存不下来不该把它作废。
    调用方会在这种情况下留一条具名警告。
    """
    if context.artifact_dir is None:
        return None
    document = observation_evidence(payload.get('evidence') or {}, task_id=context.task_id,
                                    engine_version=ALGORITHM, statistics=statistics)
    try:
        atomic_dump(context.artifact_dir / EVIDENCE_NAME, document)
    except (OSError, TypeError, ValueError):
        logger.exception('boundary observation evidence was not saved: %s', context.task_id)
        return None
    return EVIDENCE_NAME


class BaiduE82Engine:
    engine_id = "baidu_e82"

    def __init__(self, settings, gate, provider_factory=None):
        self.settings, self.gate, self.provider_factory = settings, gate, provider_factory

    @property
    def synthetic(self) -> bool:
        """The built-in circle stand-in answers instead of Baidu."""
        return self.provider_factory is None and self.settings.analysis_provider == "synthetic"

    def capabilities(self) -> EngineCapabilities:
        # A synthetic deployment must say so where the engine is chosen: its circle
        # otherwise looks exactly like a real result.
        notes = SYNTHETIC_NOTES if self.synthetic else [
            "端点证据来自百度实际返回，不来自合成端点",
            "半径与网格策略由服务端控制，请求不能覆盖",
            "内部未独立核验，质量不会高于 partial"]
        return EngineCapabilities(
            engine_id=self.engine_id,
            label="百度边界搜索（E8.2）" + ("· 合成替身" if self.synthetic else ""),
            engine_version=ALGORITHM, budgets=BUDGET_TIERS, default_budget=400,
            requires_osm_graph=False, notes=list(notes))

    def unavailable_reason(self) -> str | None:
        """Why a task could not run at all, known before it is admitted."""
        if self.provider_factory is not None or self.settings.analysis_provider == "synthetic":
            return None
        return None if self.settings.ak_configured else "walking_ak_not_configured"

    async def _provider(self, stack, origin):
        if self.provider_factory:
            provider = self.provider_factory(origin)
            if hasattr(provider, "__aenter__"):
                provider = await stack.enter_async_context(provider)
            return provider
        if self.settings.analysis_provider == "synthetic":
            # Synthetic points have exact endpoints and never claim real ones.
            return EndpointAnalyticProvider(origin, lambda x, y: math.hypot(x, y) / SYNTHETIC_SPEED_MPS)
        if not self.settings.ak_configured:
            raise ValueError("walking_ak_not_configured")
        silence_transport_logs()
        client = await stack.enter_async_context(
            httpx.AsyncClient(trust_env=False, follow_redirects=False))
        # The shared gate keeps every attempt, including retries, paced against
        # the same account-level budget as the legacy endpoints.
        return LimitedProvider(
            BaiduProvider(self.settings.baidu_map_ak.get_secret_value(), client=client), self.gate)

    async def compute(self, ask: IsochroneAsk, context: EngineContext) -> IsochroneSnapshot:
        async with AsyncExitStack() as stack:
            provider = await self._provider(stack, ask.origin)
            request = e82_request(ask.origin, ask.budget,
                                 qps=effective_qps(self.settings, self.gate) if provider.network else None)
            result = await compute_e82(request, provider, context.token,
                                       on_progress=context.progress)
        payload = result.to_dict()
        # 修订带走的成圈统计里多一项诊断：一次没有圈面的运行因此能说出"哪些观测被拒收、
        # 各自什么原因"，而不是只剩一个 ``budget``。它只数求解器已经记下的东西。
        statistics = dict(payload["statistics"])
        diagnostics = payload.get("boundaryDiagnostics")
        if diagnostics is not None:
            statistics["boundaryDiagnostics"] = diagnostics
        warnings = list(result.warnings)
        saved = save_observation_evidence(context, payload, statistics)
        if saved is not None:
            statistics["observationEvidence"] = saved
        elif context.artifact_dir is not None:
            # 证据没落盘时报告里要有名字：一份没有逐条观测的修订和一份有的修订，读起来
            # 不能一样。这只是缺一份明细，成圈结果照常发布。
            warnings.append('boundary_evidence_not_saved')
        # E8.2 exposes no separate display shell; the computed boundary is drawn.
        return IsochroneSnapshot.build(
            engine_id=self.engine_id, engine_version=ALGORITHM, algorithm=ALGORITHM,
            parameters=payload["config"], geometry=payload["geometry"],
            display_geometry=payload["geometry"], unknown_region=payload["unknownRegion"],
            uncertain_region=payload["uncertainRegion"],
            computation_extent=payload["computationExtent"], quality=result.quality,
            stop_reason=result.stop_reason, warnings=warnings,
            statistics=statistics,
            requests_used=payload["statistics"]["requests"],
            network_requests=payload["statistics"]["network_requests"])
