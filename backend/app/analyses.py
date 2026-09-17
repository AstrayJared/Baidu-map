"""Single-process, bounded analysis jobs. Never include transport errors in responses."""
import asyncio
import math
import time
from contextlib import AsyncExitStack
from dataclasses import dataclass, field
from typing import Literal
from uuid import uuid4

import httpx
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

from life_circle.coordinates import normalize
from life_circle.engine import compute_isochrone
from life_circle.models import CancelToken, IsochroneRequest, ProgressSnapshot, RouteObservation
from life_circle.providers import AnalyticProvider, BaiduProvider

from .baidu import silence_transport_logs
from .contracts import Data, Issue, Rules, TaskResultResponse, TaskStatusResponse, RouteEvidence, map_business_status
from .request_control import RequestStopped, request_slot
from .rules import DistanceRule
from .facilities import analyze_facilities


class Center(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    lng: float = Field(ge=-180, le=180, allow_inf_nan=False)
    lat: float = Field(gt=-85, lt=85, allow_inf_nan=False)


class AnalysisInput(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    center: Center
    coordinateSystem: Literal["bd09ll"]
    budget: int = 400
    clientRequestId: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")

    @field_validator("budget")
    @classmethod
    def budgets(cls, value):
        if value not in (200, 400, 800):
            raise ValueError("budget must be 200, 400 or 800")
        return value

    def fingerprint(self):
        return normalize((self.center.lng, self.center.lat)), self.budget


TERMINAL = {"completed", "cancelled", "failed"}


@dataclass
class Job:
    task_id: str
    payload: AnalysisInput
    data_source: str
    started: float = field(default_factory=time.monotonic)
    status: str = "running"
    token: CancelToken = field(default_factory=CancelToken)
    progress: ProgressSnapshot | None = None
    result: dict | None = None
    error: str | None = None
    finished_at: float | None = None
    task: asyncio.Task | None = None
    route_clicks: int = 0
    route_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def business_status(self):
        if self.status == "failed":
            return "failed"
        if self.status != "completed" or not self.result:
            return None
        return self.result.get("businessStatus", "partial")

    def view(self):
        return TaskStatusResponse(
            task_id=self.task_id, status=self.status, business_status=self.business_status(),
            stage=self.status if self.status in TERMINAL or self.status == "cancelling"
            else self.progress.stage if self.progress else "initializing",
            requests=self.progress.requests if self.progress else 0,
            network_requests=self.progress.network_requests if self.progress else 0,
            budget=self.payload.budget,
            elapsed_seconds=max(0, (self.finished_at or time.monotonic()) - self.started),
            data_source=self.data_source, error=self.error,
        ).model_dump(by_alias=True)


class RateGate:
    """Shared across jobs, including retries; space attempts after completion."""
    def __init__(self, qps, *, clock=time.monotonic, sleep=asyncio.sleep, spacing_clock=None):
        self.qps = qps
        self.interval = 1 / qps if qps else 0
        self.next_send = 0
        self.lock = asyncio.Lock()
        self.attempt_lock = asyncio.Lock()
        self.clock, self.sleep = clock, sleep
        # On Python 3.11/Windows monotonic can be quantized to 15.625 ms.
        # Deadlines keep their original epoch; pacing uses the precise counter.
        self.spacing_clock = spacing_clock or (time.perf_counter if clock is time.monotonic else clock)

    async def wait(self, deadline, *, cost=1):
        if type(cost) not in (int, float) or cost <= 0:
            raise ValueError("rate-limit cost must be positive")
        async with self.lock:
            now = self.spacing_clock()
            when = max(now, self.next_send)
            if self.clock() + when - now >= deadline:
                return False
            await self.sleep(max(0, when - now))
            # asyncio timers can wake before their requested time. Recheck the
            # clock under the lock rather than treating sleep as a permit.
            while self.spacing_clock() < when:
                if self.clock() >= deadline:
                    return False
                await self.sleep(max(when - self.spacing_clock(), time.get_clock_info('monotonic').resolution))
            if self.clock() >= deadline:
                return False
            # Round the deadline outward: repeated 1/3-second additions can
            # otherwise admit four requests into a strict rolling second.
            self.next_send = self.after(self.spacing_clock(), self.interval * cost)
            return True

    def completed(self, reason, *, cost=1):
        # Cool down all subsequent attempts, not just this destination's retry.
        base = self.interval * cost
        cooldown = max(base, 1) if reason in ('rate_limit', 'timeout', 'interrupted') else base
        self.next_send = max(self.next_send, self.after(self.spacing_clock(), cooldown))

    @staticmethod
    def after(now, delay):
        when = now + delay
        return math.nextafter(when, math.inf) if when - now < delay else when


class LimitedProvider:
    network = True

    def __init__(self, provider, gate):
        self.provider, self.gate = provider, gate
        self.identity = provider.identity
        self.batch = bool(getattr(provider, "batch", False))
        if self.batch:
            configured = math.floor(gate.qps) if gate.qps else 1
            self.batch_size = min(provider.max_batch_size, max(1, configured))

    async def query_walking_time(self, origin, destination, deadline):
        # A permit alone cannot control server arrivals after DNS/TLS/pool delays.
        # Keep the shared slot through the response and start spacing afterwards.
        async with self.gate.attempt_lock:
            if not await self.gate.wait(deadline):
                return RouteObservation(destination, reason="deadline")
            reason = 'interrupted'
            try:
                result = await self.provider.query_walking_time(origin, destination, deadline)
                reason = result.reason
                return result
            finally:
                self.gate.completed(reason)

    async def query_walking_times(self, origin, destinations, deadline):
        if not self.batch:
            return [await self.query_walking_time(origin, point, deadline) for point in destinations]
        cost = len(destinations)
        async with self.gate.attempt_lock:
            if not await self.gate.wait(deadline, cost=cost):
                return [RouteObservation(point, reason="deadline", endpoint_verified=False) for point in destinations]
            reason = "interrupted"
            try:
                results = await self.provider.query_walking_times(origin, destinations, deadline)
                reason = next((item.reason for item in results if item.reason), None)
                return results
            finally:
                self.gate.completed(reason, cost=cost)


class AnalysisManager:
    def __init__(self, settings, provider_factory=None):
        self.settings, self.provider_factory = settings, provider_factory
        self.jobs = {}
        self.gate = RateGate(settings.analysis_qps)

    def prune(self):
        terminal = sorted((job for job in self.jobs.values() if job.status in TERMINAL and job.finished_at is not None), key=lambda job: job.finished_at)
        for index, job in enumerate(terminal):
            if time.monotonic() - job.finished_at >= 1800 or index < len(terminal) - 20:
                del self.jobs[job.task_id]

    def get(self, task_id):
        self.prune()
        if task_id not in self.jobs:
            raise HTTPException(404, "任务不存在或已过期")
        return self.jobs[task_id]

    def create(self, payload):
        self.prune()
        for job in self.jobs.values():
            if job.payload.clientRequestId == payload.clientRequestId:
                if job.payload.fingerprint() != payload.fingerprint():
                    raise HTTPException(409, "请求标识已用于不同分析")
                return job
        if any(job.task and not job.task.done() for job in self.jobs.values()):
            raise HTTPException(409, "分析服务忙，请等待当前任务完成或取消后重试")
        if self.settings.analysis_provider == "baidu" and not self.provider_factory:
            if not self.settings.ak_configured or self.settings.analysis_qps is None:
                raise HTTPException(503, "请在后端配置步行服务 AK 和 ANALYSIS_QPS")
            if (payload.budget - 1) / self.settings.analysis_qps >= 600:
                raise HTTPException(503, "配置的 QPS 无法在截止时间内完成初始化")
        source = "synthetic" if self.settings.analysis_provider == "synthetic" else "baidu_walking"
        job = Job(str(uuid4()), payload, source)
        self.jobs[job.task_id] = job
        job.task = asyncio.create_task(self.run(job))
        return job

    def update(self, job, progress):
        if job.status == "running":
            job.progress = progress

    async def run(self, job):
        try:
            business = None
            async with AsyncExitStack() as stack:
                origin, budget = job.payload.fingerprint()
                if self.provider_factory:
                    provider = self.provider_factory(origin)
                    if hasattr(provider, "__aenter__"):
                        provider = await stack.enter_async_context(provider)
                elif self.settings.analysis_provider == "synthetic":
                    provider = AnalyticProvider(origin, lambda x, y: math.hypot(x, y) / 1.2)
                else:
                    silence_transport_logs()
                    client = await stack.enter_async_context(httpx.AsyncClient(trust_env=False, follow_redirects=False))
                    provider = LimitedProvider(
                        BaiduProvider(self.settings.baidu_map_ak.get_secret_value(), client=client),
                        self.gate,
                    )
                request = IsochroneRequest(origin, "bd09ll", budget=budget,
                    qps=self.settings.analysis_qps if provider.network else None)
                result = await compute_isochrone(request, provider, job.token, on_progress=lambda p: self.update(job, p))
                if not self.provider_factory and provider.network and result.quality != "insufficient" and not job.token.cancelled:
                    self.update(job, ProgressSnapshot("facilities", result.statistics.requests,
                        result.statistics.network_requests, budget, time.monotonic() - job.started))
                    business = await analyze_facilities(result, client,
                        self.settings.baidu_map_ak.get_secret_value(), self.gate, job.token,
                        deadline=job.started + 600)
            # Commit only after transport cleanup; cancellation during cleanup wins.
            if job.token.cancelled:
                job.status = "cancelled"
            elif result.stop_reason == "geometry_error":
                job.status, job.error = "failed", "几何重建失败，请重试或检查采样证据"
            else:
                payload = result.to_dict()
                # The current algorithm only supplies the isochrone.  The
                # normalized business status therefore remains partial until
                # facility/report modules are connected.  Insufficient evidence
                # is a failed business result even though the async task ran.
                business_status = map_business_status(quality=payload["quality"],
                    facilities_status=business[2].status if business else "not_integrated",
                    facilities=business[0] if business else None)
                warnings = [Issue(code="ALGORITHM_WARNING", message=message,
                                   scope="isochrone") for message in payload["warnings"]]
                errors = ([Issue(code="INSUFFICIENT_EVIDENCE",
                                 message="没有足够步行证据生成等时圈。", scope="isochrone", severity="error")]
                           if business_status == "failed" else [])
                confirmation = "baidu_sampled" if provider.network else "implementation_only"
                job.result = TaskResultResponse(
                    task_id=job.task_id, task_status="completed", status=business_status,
                    business_status=business_status,
                    data_source=job.data_source, center={"lng": origin[0], "lat": origin[1]},
                    generated_at=time.time(), facilities_status=business[2].status if business else "not_integrated",
                    facility_analysis=business[2] if business else None,
                    rules=Rules(time_confirmation=confirmation, distance=DistanceRule(metric="walking_route", threshold_m=1000,
                        inclusive=True, tolerance_m=100, assessment_scope="isochrone",
                        category_policy="major_minor")),
                    data=Data(geometry=payload["geometry"], evidence_geometry=payload.get("evidenceGeometry"),
                              inferred_region=payload.get("inferredRegion"), uncertain_region=payload["uncertainRegion"],
                              unknown_region=payload["unknownRegion"], computation_extent=payload["computationExtent"]),
                    algorithm=payload, warnings=warnings, errors=errors, isochrone=payload,
                ).model_dump(by_alias=True)
                if business:
                    facilities, categories, evidence, report = business
                    job.result["data"].update(facilities=[f.model_dump(by_alias=True) for f in facilities], categories=[c.model_dump() for c in categories], report=report)
                    job.result["warnings"].extend(Issue(code="FACILITY_LIMITATION", message=message, scope="facilities").model_dump() for message in evidence.warnings)
                job.status = "completed"
        except asyncio.CancelledError:
            job.token.cancel()
            job.status = "cancelled"
        except Exception:
            job.status = "cancelled" if job.token.cancelled else "failed"
            job.error = None if job.token.cancelled else "分析执行失败，请检查后端配置后重试"
        finally:
            job.finished_at = time.monotonic()
            self.prune()

    def cancel(self, job):
        if job.status not in TERMINAL:
            job.status = "cancelling"
            job.token.cancel()
        return job

    async def close(self):
        tasks = []
        for job in list(self.jobs.values()):
            self.cancel(job)
            if job.task:
                tasks.append(job.task)
        if tasks:
            _, pending = await asyncio.wait(tasks, timeout=10)
            for task in pending:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)


def analysis_router(manager):
    router = APIRouter(prefix="/api/analyses", tags=["analyses"])

    @router.post("/{task_id}/routes/{facility_id}", response_model=RouteEvidence)
    async def facility_route(task_id: str, facility_id: str):
        job = manager.get(task_id)
        if job.status != "completed" or not job.result or not job.result.get("facilityAnalysis"):
            raise HTTPException(409, "设施结果尚未就绪")
        async with job.route_lock:
            evidence = job.result["facilityAnalysis"]
            if facility_id in evidence["routes"]:
                return evidence["routes"][facility_id]
            item = next((f for f in job.result["data"]["facilities"] if f["id"] == facility_id), None)
            if item is None:
                raise HTTPException(404, "设施不属于本次分析")
            if job.route_clicks >= 3:
                raise HTTPException(429, "本次分析的新增路线查询已达3次，请使用已有路线或重新分析")
            job.route_clicks += 1
            deadline = time.monotonic()+20
            origin = job.payload.fingerprint()[0]
            silence_transport_logs()
            async with httpx.AsyncClient(trust_env=False, follow_redirects=False) as client:
                provider = BaiduProvider(manager.settings.baidu_map_ak.get_secret_value(),client=client,destination_uid=facility_id,route_metric="distance")
                observed = await LimitedProvider(provider, manager.gate).query_walking_time(origin,(item["location"]["lng"],item["location"]["lat"]),deadline)
            from .poi_evidence import route_evidence
            value = route_evidence(observed, origin,
                (item["location"]["lng"], item["location"]["lat"]), facility_id).model_dump(by_alias=True)
            evidence["routes"][facility_id] = value
            # Update the stored result and route response together for this task.
            item["poiEvidence"] = value["poiEvidence"]
            evidence["network_requests"] += 1
            return value

    @router.post("", status_code=202, response_model=TaskStatusResponse)
    async def create(payload: AnalysisInput):
        return manager.create(payload).view()

    @router.get("/{task_id}", response_model=TaskStatusResponse)
    async def status(task_id: str):
        return manager.get(task_id).view()

    @router.post("/by-request/{client_request_id}/cancel", status_code=202, response_model=TaskStatusResponse)
    async def cancel_by_request(client_request_id: str):
        # Recover a lost create response without replaying POST and starting new work.
        manager.prune()
        for job in manager.jobs.values():
            if job.payload.clientRequestId == client_request_id:
                return manager.cancel(job).view()
        raise HTTPException(404, "任务不存在或已过期")

    @router.get("/{task_id}/result", response_model=TaskResultResponse)
    async def result(task_id: str):
        job = manager.get(task_id)
        if job.status != "completed":
            raise HTTPException(409, "任务尚未完成或没有可用结果")
        return job.result

    @router.post("/{task_id}/cancel", status_code=202, response_model=TaskStatusResponse)
    async def cancel(task_id: str):
        return manager.cancel(manager.get(task_id)).view()

    return router
