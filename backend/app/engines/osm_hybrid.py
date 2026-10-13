"""OSM + Baidu Hybrid compute-only adapter.

Live Hybrid traffic refuses to run without a durable pre-send ledger
(``cache.py``: ``live_requests_require_durable_ledger``), so the task's own
artifact directory carries it. The ledger is engine audit evidence; the
immutable checkup revision is published separately.
"""
import asyncio
import time

from shapely.geometry import Point

from ..algorithms.hybrid_isochrone import HybridConfig, HybridIsochroneProvider
from ..algorithms.hybrid_isochrone.baidu_validator import StrictBaiduProvider
from ..algorithms.hybrid_isochrone.cache import ReplayProvider
from ..algorithms.hybrid_isochrone.extent import ALGORITHM_VERSION, computation_extent
from ..algorithms.hybrid_isochrone.hard_obstacles import load_obstacles
from ..algorithms.hybrid_isochrone.osm_guidance import OsmGuidance
from ..algorithms.osm_offline.lazy import resolve
from ..request_control import RequestStopped, wait_with_cancel
from ..geo.projection import MetricProjection
from life_circle.models import ProgressSnapshot

from .protocol import EngineCancelled, EngineCapabilities, EngineContext, IsochroneAsk, IsochroneSnapshot

# The Hybrid core caps generation at 400 attempts; the checkup offers no more.
BUDGET_TIERS = (200, 400)
DEFAULT_QPS = 3.0
#: An offline deployment has no Baidu endpoint evidence and no local stand-in for it,
#: so the refusal happens before a task exists rather than after it tried. These notes
#: replace the normal ones, the way ``SYNTHETIC_NOTES`` does for E8.2: the claim that
#: endpoints are validated against what Baidu returned must not survive unavailability.
OFFLINE_ENGINE_NOTES = [
    "本部署为离线合成模式：该引擎用百度实际返回的端点核验边界，本地没有替身",
    "提交该引擎会在建任务之前具名拒绝（checkup_engine_unavailable）",
]


class OsmHybridEngine:
    engine_id = "osm_hybrid"
    #: The task artifact a finished run can be rebuilt from (``compute(replay=...)``).
    ledger_name = "hybrid-ledger.json"

    def __init__(self, settings, gate, offline, provider_factory=None):
        self.settings, self.gate, self.offline = settings, gate, offline
        self.provider_factory = provider_factory

    @property
    def offline_without_provider(self) -> bool:
        """An offline deployment with nothing local to validate the boundary against."""
        return (self.provider_factory is None
                and self.settings.analysis_provider == "synthetic")

    def unavailable_reason(self) -> str | None:
        """Why a task could not run at all, known before it is admitted.

        This engine validates its boundary against endpoints Baidu actually
        returned. With no provider injected there is no local stand-in for that
        evidence, so an offline deployment must refuse by name instead of
        admitting a task that could only reach for the network and end as
        ``consecutive_api_failures``. A deployment with no walking key says so
        the same way E8.2 does, for the same reason.
        """
        if self.provider_factory is not None:
            return None
        if self.settings.analysis_provider == "synthetic":
            return "synthetic_mode_offline"
        return None if self.settings.ak_configured else "walking_ak_not_configured"

    def capabilities(self) -> EngineCapabilities:
        # An offline deployment must say so where the engine is chosen, the same way
        # E8.2 relabels its synthetic stand-in: a boundary with no Baidu endpoint
        # evidence must not look like one that was validated.
        offline = self.offline_without_provider
        notes = list(OFFLINE_ENGINE_NOTES) if offline else [
            "OSM 引导成圈，端点由百度实际返回核验",
            "displayGeometry 只用于外轮廓展示，不参与计数与评估范围",
            "缺图或缺风险层时标记 degraded，不冒充完整 Hybrid"]
        return EngineCapabilities(
            engine_id=self.engine_id,
            label="OSM＋百度" + ("· 离线不可用" if offline else ""),
            engine_version=ALGORITHM_VERSION,
            budgets=BUDGET_TIERS, default_budget=400, requires_osm_graph=True,
            notes=notes)

    def _config(self, budget: int) -> HybridConfig:
        qps = self.settings.analysis_qps or DEFAULT_QPS
        return HybridConfig(max_baidu_requests=budget, request_qps=max(1.0, min(20.0, qps)))

    async def _prepare(self, origin, config, step=lambda name, count=None, limit=None: None, *, token=None):
        """Resolve graph, guidance and obstacles; report what is missing.

        ``step`` is told as each of the three begins: none sends a request, and
        the first load of the city graph takes minutes, told as it goes.
        """
        step("graph")
        resolved = await resolve(self.offline, step, token=token)
        store, coverage = resolved.store, resolved.coverage
        projection = store.projection if store else MetricProjection(self.settings.osm_metric_crs)
        step("guidance")
        guidance = await wait_with_cancel(asyncio.to_thread(
            OsmGuidance, store, projection.origin(origin), config,
            risk_path=self.settings.hybrid_risk_path, speed=self.settings.walk_speed_mps), token, keep_running=True)
        extent = computation_extent(projection.origin(origin), config)
        step("obstacles")
        obstacles = await wait_with_cancel(asyncio.to_thread(
            load_obstacles, self.settings.hybrid_obstacle_path, projection,
            self.settings.osm_data_version, extent, getattr(self.settings, "water_review_dir", None)), token, keep_running=True)
        ready = dict(
            graph_available=store is not None,
            data_version_matches=bool(
                store and store.graph.graph.get("osm_data_version") == self.settings.osm_data_version),
            coverage_available=coverage is not None,
            origin_in_coverage=bool(coverage is not None and coverage.covers(Point(projection.origin(origin)))),
            extent_in_coverage=bool(coverage is not None and coverage.covers(extent)),
            obstacle_layer_available=bool(obstacles.source),
            risk_layer_available=not any(w in guidance.warnings for w in
                ("osm_risk_layer_invalid", "osm_water_railway_barrier_layer_unavailable")))
        warnings = sorted(set([f"hybrid_{k}_false" for k, v in ready.items() if not v]
                              + guidance.warnings + obstacles.warnings))
        return projection, guidance, obstacles, ready, warnings

    async def compute(self, ask: IsochroneAsk, context: EngineContext, *,
                      replay: dict | None = None) -> IsochroneSnapshot:
        """Run the engine, or rebuild a finished run from its ledger (``replay``).

        A replay restores every paid sample and asks nothing: the boundary is
        rebuilt from the same Baidu evidence against the current obstacle layer,
        which is what a correction to the water data changes. The run's own
        configuration is used, not today's defaults.
        """
        config = HybridConfig(**replay["config"]) if replay is not None else self._config(ask.budget)
        started = time.perf_counter()
        budget = config.max_baidu_requests
        # What the engine reports while it runs: the preparation steps (none an
        # attempt), then one tick per sample with the attempts so far and how
        # many of them left the process.
        network = False

        def report(stage, used=0):
            context.progress(ProgressSnapshot(stage, used, used if network else 0, budget,
                                              time.perf_counter() - started))
        try:
            projection, guidance, obstacles, ready, readiness_warnings = await self._prepare(
                ask.origin, config, context.step, token=context.token)
        except RequestStopped:
            raise EngineCancelled() from None
        if context.token.cancelled:
            raise EngineCancelled()
        # A replay writes no ledger: the stored one stays the audit record of what was paid.
        ledger_path = (context.artifact_dir / self.ledger_name
                       if context.artifact_dir is not None and replay is None else None)
        if replay is not None:
            provider = ReplayProvider(replay)
            engine = HybridIsochroneProvider(projection, provider, self.gate, guidance,
                                             obstacles=obstacles,
                                             progress=lambda used, sample: report("sampling", used))
            result = await engine.compute(ask.origin, config, token=context.token,
                                          seed_ledger=replay)
            # Every sample was restored, none continued: say "rebuilt from stored
            # samples" rather than the continuation notice.
            result["warnings"] = [w for w in result["warnings"]
                                  if w != "explicit_continuation_prior_samples_retained"]
            result["warnings"].append("rebuilt_from_stored_samples")
        else:
            provider = (self.provider_factory(projection, config) if self.provider_factory
                        else StrictBaiduProvider(self.settings.baidu_map_ak.get_secret_value(),
                                                 projection, config))
            network = bool(getattr(provider, "network", False))
            report("sampling")
            async with provider:
                engine = HybridIsochroneProvider(projection, provider, self.gate, guidance,
                                                 obstacles=obstacles,
                                                 progress=lambda used, sample: report("sampling", used))
                result = await engine.compute(ask.origin, config, ledger_path=ledger_path,
                                              token=context.token)
        warnings = sorted(set(result["warnings"] + readiness_warnings))
        quality = result["quality"]
        if readiness_warnings and quality == "usable":
            quality = "partial"
        # 离线步行图与本地合成 provider 都不出进程。它照样"用掉"成圈次数，但一次百度请求
        # 也没发 —— 本应用的预算池读的是后面那个数，两个计数不能混成同一个。
        network_requests = result["requests_used"] if getattr(provider, "network", False) else 0
        statistics = {
            "requestsUsed": result["requests_used"],
            "networkRequests": network_requests,
            "validBaiduSamples": result["valid_baidu_samples"],
            "invalidBaiduSamples": result["invalid_baidu_samples"],
            "unknownSamples": result["unknown_samples"],
            "boundaryErrorEstimate": result["boundary_error_estimate"],
            "computeSeconds": round(time.perf_counter() - started, 6),
        }
        return IsochroneSnapshot.build(
            engine_id=self.engine_id, engine_version=ALGORITHM_VERSION,
            # The boundary is drawn from the shell; counting and the assessment
            # domain use the computed geometry and its unknown region.
            algorithm=result["algorithm"], parameters=result["config"],
            geometry=result["geometry"], display_geometry=result.get("displayGeometry"),
            unknown_region=result["unknown_region"],
            uncertain_region=result.get("evidence_unknown_region"),
            computation_extent=result["computation_extent"], quality=quality,
            stop_reason=result["stop_reason"], warnings=warnings, statistics=statistics,
            requests_used=result["requests_used"], network_requests=network_requests)
