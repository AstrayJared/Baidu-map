"""Independent async geometry-only jobs, using existing wire envelopes."""
import asyncio
import hashlib
import json
import time
from dataclasses import dataclass, field
from uuid import uuid4

from fastapi import APIRouter, HTTPException, Request
from life_circle.models import CancelToken
from life_circle.coordinates import normalize

from .algorithms.hybrid_isochrone import HybridConfig, HybridIsochroneProvider
from .algorithms.hybrid_isochrone.baidu_validator import StrictBaiduProvider
from .algorithms.hybrid_isochrone.osm_guidance import OsmGuidance
from .contracts import TaskStatusResponse, Data, Issue, Rules, map_business_status
from .hybrid_contracts import HybridRequest, HybridResultResponse, HybridIsochrone, HybridReadiness, HybridError
from .geo.projection import MetricProjection
from .algorithms.hybrid_isochrone.hard_obstacles import load_obstacles
from shapely.geometry import Point
from .algorithms.hybrid_isochrone.extent import computation_extent
from .persistence import atomic_dump


def content_hash(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                    ensure_ascii=False, allow_nan=False).encode()).hexdigest()


def fail(status, code):
    raise HTTPException(status, detail={"code": code, "message": code})


@dataclass
class HybridJob:
    payload: HybridRequest
    task_id: str = field(default_factory=lambda: str(uuid4()))
    status: str = "running"
    token: CancelToken = field(default_factory=CancelToken)
    started: float = field(default_factory=time.monotonic)
    finished: float | None = None
    engine: object = None
    task: object = None
    result: dict | None = None
    error: str | None = None
    stage: str = "preparing"

    def view(self):
        requests = self.engine.session.requests_used if self.engine and self.engine.session else 0
        return TaskStatusResponse(task_id=self.task_id, status=self.status,
                                  business_status=self.result["businessStatus"] if self.result else None,
                                  stage=self.status if self.status != "running" else self.stage,
                                  requests=requests, network_requests=requests, budget=self.payload.config.max_baidu_requests,
                                  elapsed_seconds=(self.finished or time.monotonic()) - self.started,
                                  data_source="baidu_walking", error=self.error)


class HybridManager:
    def __init__(self, settings, gate, provider_factory=None):
        self.settings, self.gate, self.provider_factory = settings, gate, provider_factory
        self.jobs = {}

    def prune(self):
        done = sorted((j for j in self.jobs.values() if j.finished is not None), key=lambda j: j.finished)
        for i, job in enumerate(done):
            if time.monotonic() - job.finished >= 1800 or i < len(done) - 20:
                del self.jobs[job.task_id]

    def get(self, task_id):
        self.prune()
        if task_id not in self.jobs:
            fail(404, "hybrid_task_not_found_or_expired")
        return self.jobs[task_id]

    def by_request(self, client_request_id):
        self.prune()
        for job in self.jobs.values():
            if job.payload.client_request_id == client_request_id:
                return job
        fail(404, "hybrid_request_not_found_or_expired")

    def cancel(self, job):
        if job.status == "running":
            job.token.cancel()
            job.status = "cancelling"
        return job.view()

    def create(self, payload, offline):
        self.prune()
        for job in self.jobs.values():
            if job.payload.client_request_id == payload.client_request_id:
                if job.payload != payload:
                    fail(409, "hybrid_request_id_conflict")
                return job
        if any(j.task and not j.task.done() for j in self.jobs.values()):
            fail(409, "hybrid_busy")
        if not self.provider_factory and not self.settings.ak_configured:
            fail(503, "baidu_walking_not_configured")
        job = HybridJob(payload)
        self.jobs[job.task_id] = job
        job.task = asyncio.create_task(self.run(job, offline))
        return job

    async def run(self, job, offline):
        config = job.payload.config
        origin = normalize((job.payload.origin.lng, job.payload.origin.lat))
        path = self.settings.hybrid_ledger_dir / job.task_id
        try:
            path.mkdir(parents=True, exist_ok=False)
            atomic_dump(path / "plan.json", job.payload.model_dump(mode="json"))
            prep_started = time.perf_counter()
            resolved = await asyncio.to_thread(offline.get) if hasattr(offline, "get") else offline
            store, coverage = resolved.store, resolved.coverage
            projection = store.projection if store else MetricProjection(self.settings.osm_metric_crs)
            guidance = await asyncio.to_thread(OsmGuidance, store, projection.origin(origin), config,
                                               risk_path=self.settings.hybrid_risk_path, speed=self.settings.walk_speed_mps)
            if job.token.cancelled:
                job.status = "cancelled"
                return
            origin_xy = projection.origin(origin)
            extent = computation_extent(origin_xy, config)
            obstacle_started = time.perf_counter()
            obstacles = await asyncio.to_thread(load_obstacles, self.settings.hybrid_obstacle_path,
                projection, self.settings.osm_data_version, extent)
            obstacle_seconds = time.perf_counter() - obstacle_started
            ready = dict(graph_available=store is not None,
                         data_version_matches=bool(store and store.graph.graph.get("osm_data_version") == self.settings.osm_data_version),
                         coverage_available=coverage is not None,
                         origin_in_coverage=bool(coverage is not None and coverage.covers(Point(origin_xy))),
                         extent_in_coverage=bool(coverage is not None and coverage.covers(extent)),
                         obstacle_layer_available=bool(obstacles.source),
                         risk_layer_available=not any(w in guidance.warnings for w in
                             ("osm_risk_layer_invalid", "osm_water_railway_barrier_layer_unavailable")))
            readiness_warnings = sorted(set([f"hybrid_{k}_false" for k, v in ready.items() if not v]
                                           + guidance.warnings + obstacles.warnings))
            readiness = HybridReadiness(**ready, mode="degraded" if readiness_warnings else "full",
                                        warnings=readiness_warnings)
            preparation = time.perf_counter() - prep_started
            if job.token.cancelled:
                job.status = "cancelled"
                return
            job.stage = "adaptive_geometry"
            provider = self.provider_factory(projection, config) if self.provider_factory else StrictBaiduProvider(
                self.settings.baidu_map_ak.get_secret_value(), projection, config)
            async with provider:
                job.engine = HybridIsochroneProvider(projection, provider, self.gate, guidance, obstacles=obstacles)
                result = await job.engine.compute(origin, config, ledger_path=path / "ledger.json", token=job.token)
                result['diagnostics']['timing_seconds']['obstacle_load'] = obstacle_seconds
            if job.token.cancelled:
                job.status = "cancelled"
                atomic_dump(path / "partial-result.json", result)
                return
            job.stage = "serializing_result"
            result["warnings"] = sorted(set(result["warnings"] + readiness_warnings))
            if readiness.mode == "degraded" and result["quality"] == "usable":
                result["quality"] = "partial"
            events = job.engine.session.events
            request_seconds = sum(max(0, e.get("completed_at", e.get("sent_at", 0)) - e.get("sent_at", 0))
                                  for e in events if "sent_at" in e)
            timings = {**result["diagnostics"]["timing_seconds"], "preparation": preparation,
                       "requests": request_seconds, "task_total": time.monotonic() - job.started}
            core = HybridIsochrone(**{k: v for k, v in result.items() if k != "diagnostics"},
                                   readiness=readiness, timing_seconds=timings)
            atomic_dump(path / "diagnostics.json", result)
            status = map_business_status(quality=result["quality"], facilities_status="not_integrated")
            response = HybridResultResponse(task_id=job.task_id, task_status="completed", status=status,
                business_status=status, data_source="baidu_walking", center=job.payload.origin,
                generated_at=time.time(), facilities_status="not_integrated", rules=Rules(time_confirmation="baidu_sampled"),
                data=Data(geometry=result["geometry"], unknown_region=result["unknown_region"],
                          computation_extent=result["computation_extent"]), algorithm=core, isochrone=core,
                config_hash=content_hash(job.payload.model_dump(mode="json", exclude={"client_request_id"})),
                result_hash=content_hash(core.model_dump(mode="json", by_alias=True)),
                warnings=[Issue(code=w.upper(), message=w, scope="isochrone") for w in result["warnings"]]
            ).model_dump(mode="json", by_alias=True)
            atomic_dump(path / "result.json", response)
            job.result = response
            job.status = "completed"
        except asyncio.CancelledError:
            job.status = "cancelled"
        except Exception as exc:
            job.status, job.error = "failed", "hybrid_execution_failed"
            job.result = None
            try:
                atomic_dump(path / "failure.json", dict(code=job.error, stage=job.stage,
                    exception_type=type(exc).__name__, elapsed_seconds=time.monotonic() - job.started))
            except OSError:
                job.error = "hybrid_persistence_failed"
        finally:
            job.finished = time.monotonic()

    async def close(self):
        active = [j for j in self.jobs.values() if j.task and not j.task.done()]
        for job in active:
            job.token.cancel()
            job.task.cancel()
        if active:
            await asyncio.gather(*(j.task for j in active), return_exceptions=True)


def hybrid_router(manager, *, prefix="/api/v1/analysis/hybrid"):
    router = APIRouter(prefix=prefix, tags=["Hybrid Isochrone"],
                       responses={n: {"model": HybridError} for n in (404, 409, 422, 503)})

    @router.post("", status_code=202, response_model=TaskStatusResponse)
    async def create(payload: HybridRequest, request: Request):
        offline = request.app.state.osm_offline
        return manager.create(payload, offline).view()

    @router.get("/by-request/{client_request_id}", response_model=TaskStatusResponse)
    async def by_request(client_request_id: str):
        return manager.by_request(client_request_id).view()

    @router.post("/by-request/{client_request_id}/cancel", response_model=TaskStatusResponse)
    async def cancel_by_request(client_request_id: str):
        return manager.cancel(manager.by_request(client_request_id))

    @router.get("/{task_id}", response_model=TaskStatusResponse)
    async def status(task_id: str):
        return manager.get(task_id).view()

    @router.get("/{task_id}/result", response_model=HybridResultResponse)
    async def result(task_id: str):
        job = manager.get(task_id)
        if job.result is None:
            fail(409, "hybrid_result_not_available")
        return job.result

    @router.post("/{task_id}/cancel", response_model=TaskStatusResponse)
    async def cancel(task_id: str):
        job = manager.get(task_id)
        return manager.cancel(job)

    return router
