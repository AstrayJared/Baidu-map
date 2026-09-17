import asyncio
import math
import random
import time

from shapely import union_all
from shapely.errors import GEOSException
from shapely.geometry import box

from .coordinates import LocalProjection, normalize
from .field import GeometryError, business_geometry, reconstruct, contour_field
from .mesh import Mesh
from .models import CancelToken, IsochroneResult, ProgressSnapshot, RouteObservation
from .scheduler import Scheduler


async def compute_isochrone(request, provider, cancel_token=None, *, clock=None, method="adaptive", on_progress=None):
    engine_started = time.perf_counter()
    waiting_seconds = 0
    if method not in ("adaptive", "uniform"):
        raise ValueError("未知算法")
    if getattr(provider, "destination_uid", None) is not None:
        raise ValueError("面采样不能绑定单个目的设施 UID；请用 Scheduler 单独测时")
    token = cancel_token or CancelToken()
    scheduler = Scheduler(request, provider, token, clock)
    stage = "initializing"

    def report(next_stage=None):
        nonlocal stage
        if next_stage:
            stage = next_stage
        if on_progress:
            on_progress(ProgressSnapshot(stage, scheduler.stats.requests, scheduler.stats.network_requests,
                request.budget, max(0, scheduler.clock.time() - scheduler.started)))

    scheduler.on_progress = report
    report()
    projection = LocalProjection(scheduler.origin)
    mesh = Mesh(request.extent, request.coarse_size)
    if method == "uniform":
        n = math.isqrt(request.budget + 1)
        while n * n - int(n % 2 == 1) > request.budget:
            n -= 1
        if n < 2:
            raise ValueError("配置不足以完成初始化")
        mesh = Mesh(request.extent, 2 * request.extent / (n - 1))
    initial = mesh.required_points() if method == "adaptive" else sorted({p for c in mesh.leaves for p in c.corners})
    initial_queries = len({normalize(projection.to_geographic(p)) for p in initial} - {scheduler.origin})
    if initial_queries > request.budget or (request.qps and (initial_queries - 1) / request.qps >= request.deadline_seconds):
        raise ValueError("配置不足以完成初始化：检查预算、QPS 与截止时间")
    warnings = []
    budget_blocked = False

    async def measure(points, request_limit=None):
        nonlocal waiting_seconds
        points = sorted(set(points) - mesh.samples.keys())
        before = time.perf_counter()
        observations = await scheduler.observe_many([projection.to_geographic(p) for p in points], request_limit=request_limit)
        waiting_seconds += time.perf_counter() - before
        mesh.samples.update(zip(points, observations))

    def cost(points):
        return len({normalize(projection.to_geographic(p)) for p in points} - scheduler.cache.keys())

    async def split(cell, request_limit=None):
        nonlocal budget_blocked
        points = mesh.required_points(mesh.children(cell))
        if cost(points) > scheduler.remaining:
            budget_blocked = True
            return False
        mesh.split(cell)
        await measure(points, request_limit)
        return True

    await measure(initial)
    if method == "uniform":
        for cell in sorted(mesh.leaves):
            corners = [mesh.samples[p] for p in cell.corners]
            if cell.center == (0, 0):
                mesh.samples[cell.center] = scheduler.cache[scheduler.origin]
            else:
                # Derived support values, not provider observations or billable samples.
                value = sum(o.duration for o in corners) / 4 if all(o.duration is not None for o in corners) else None
                mesh.samples[cell.center] = RouteObservation(projection.to_geographic(cell.center), value, endpoint_verified=all(o.endpoint_verified for o in corners))
    outer = [o for p, o in mesh.samples.items() if abs(p[0]) == mesh.extent or abs(p[1]) == mesh.extent]
    expand_needed = any(o.duration is not None and o.duration <= 1020 for o in outer)
    if any(o.duration is None for o in outer):
        warnings.append("range_unknown")
    if expand_needed:
        report("expanding")
        if request.expand and method == "adaptive" and request.max_extent > mesh.extent:
            old = mesh.extent
            added = [c for c in Mesh.coarse_cells(request.max_extent, request.coarse_size) if not (-old <= c.x and c.x + c.size <= old and -old <= c.y and c.y + c.size <= old)]
            points = mesh.required_points(added)
            if cost(points) <= scheduler.remaining and not scheduler._stopped():
                mesh.expand(request.max_extent, request.coarse_size)
                await measure(points)
                outer = [o for p, o in mesh.samples.items() if abs(p[0]) == mesh.extent or abs(p[1]) == mesh.extent]
                if any(o.duration is None for o in outer):
                    warnings.append("range_unknown")
                if any(o.duration is not None and o.duration <= 1020 for o in outer):
                    warnings.append("range_truncated")
            else:
                warnings.append("range_truncated")
        else:
            warnings.append("range_truncated")

    settled = set()
    completed_checks = set()

    def active_residual(cell):
        observations = [mesh.samples[p] for edge in mesh.edges(cell) for p in edge if p in mesh.active_points]
        return max((abs(o.duration - 900) for o in observations if o.duration is not None), default=0)
    if method == "adaptive":
        report("exploring")
        # Execute the reserved non-boundary exploration deterministically first.
        reserve = min(int(request.budget * request.exploration_fraction), scheduler.remaining)
        exploration_start = scheduler.stats.requests
        candidates = [c for c in sorted(mesh.leaves)
                      if c.size > request.min_size and not mesh.priority(c, request.boundary_band)[1]
                      and all(o is not None and o.duration is not None for o in mesh.observations(c))]
        random.Random(request.seed).shuffle(candidates)
        candidates.sort(key=lambda c: -c.size)
        for cell in candidates:
            if scheduler._stopped():
                break
            points = mesh.required_points(mesh.children(cell))
            if cost(points) > reserve - (scheduler.stats.requests - exploration_start):
                break
            await split(cell, reserve - (scheduler.stats.requests - exploration_start))
        scheduler.stats.exploration_requests = scheduler.stats.requests - exploration_start

        report("refining")
        while not scheduler._stopped():
            queue = []
            for cell in mesh.leaves - settled:
                score, candidate, residual = mesh.priority(cell, request.boundary_band)
                if candidate:
                    queue.append((-score, cell))
            if not queue:
                break
            _, cell = min(queue)
            if cell.size > 100:
                if not await split(cell):
                    settled.add(cell)
                continue
            new_points = mesh.active_candidates(cell, request.min_spacing) if request.active_sampling else []
            if new_points and scheduler.remaining:
                point = new_points[0]
                await measure([point])
                mesh.active_points.add(point)
                observation = mesh.samples[point]
                if observation.duration is not None and abs(observation.duration - 900) <= request.residual_target:
                    # Other crossing intervals on this edge still get checked next iteration.
                    if not mesh.active_candidates(cell, request.min_spacing) and active_residual(cell) <= request.residual_target and mesh.priority(cell, request.boundary_band)[2] <= 60:
                        settled.add(cell)
                        completed_checks.add(cell)
                continue
            if cell.size > request.min_size:
                if not await split(cell):
                    settled.add(cell)
            else:
                settled.add(cell)

    scheduler.close()
    report("reconstructing")
    try:
        field = await asyncio.to_thread(reconstruct, mesh, request.raster_size)
        bands = []
        for minutes in request.time_bands:
            geometry = field.geometry if minutes == 15 else await asyncio.to_thread(contour_field, field.x, field.x, field.z, field.support, minutes * 60)
            bands.append({"minutes": minutes, "geometry": None if field.support.is_empty else business_geometry(geometry, projection)})
    except (GeometryError, GEOSException):
        # Keep evidence metadata; never silently repair and enlarge the result.
        warnings.append("geometry_error")
        scheduler.stats.total_seconds = scheduler.clock.time() - scheduler.started
        extent = box(-mesh.extent, -mesh.extent, mesh.extent, mesh.extent)
        scheduler.stats.unknown_area = extent.area
        scheduler.stats.compute_seconds = max(0, time.perf_counter() - engine_started - waiting_seconds)
        report("completed")
        return IsochroneResult(None, business_geometry(extent, projection), business_geometry(extent, projection), business_geometry(extent, projection), "insufficient", "geometry_error", scheduler.stats, warnings, request, local_unknown=extent)
    uncertain = []
    unfinished = 0
    for cell in sorted(mesh.leaves):
        score, candidate, residual = mesh.priority(cell, request.boundary_band)
        observations = mesh.observations(cell)
        unverified = any(o and o.duration is not None and not o.endpoint_verified for o in observations)
        if candidate or unverified:
            uncertain.append(cell.polygon)
        if candidate and ((cell.size > request.min_size and cell not in completed_checks) or residual > 60 or active_residual(cell) > request.residual_target):
            unfinished += 1
        key = f"{cell.size:g}"
        scheduler.stats.cells_by_size[key] = scheduler.stats.cells_by_size.get(key, 0) + 1
    if unfinished:
        warnings.append("unfinished_boundary")
    if any(o.duration is not None and not o.endpoint_verified for o in mesh.samples.values()):
        warnings.append("endpoints_unverified")
    if not field.geometry.is_empty and field.geometry.distance(box(-mesh.extent, -mesh.extent, mesh.extent, mesh.extent).boundary) < 1e-7:
        warnings.append("range_truncated")
    scheduler.stats.active_points = len(mesh.active_points)
    scheduler.stats.unknown_area = field.unknown.area
    scheduler.stats.unfinished_boundary = unfinished
    scheduler.stats.compute_seconds = max(0, time.perf_counter() - engine_started - waiting_seconds)
    scheduler.stats.total_seconds = scheduler.clock.time() - scheduler.started
    insufficient = field.support.is_empty
    quality = "insufficient" if insufficient else "partial" if warnings or field.unknown.area > 1e-7 else "usable"
    report("completed")
    return IsochroneResult(
        None if insufficient else business_geometry(field.geometry, projection),
        business_geometry(union_all(uncertain), projection), business_geometry(field.unknown, projection),
        business_geometry(box(-mesh.extent, -mesh.extent, mesh.extent, mesh.extent), projection), quality,
        scheduler.stop_reason or ("budget" if budget_blocked else "maximum_range" if "range_truncated" in warnings and mesh.extent == request.max_extent else "resolution_limit"), scheduler.stats, sorted(set(warnings)), request,
        None if insufficient else field.geometry, field.unknown,
        time_bands=bands, sample_observations=list(scheduler.cache.values()),
        unreachable_region=None if insufficient else business_geometry(field.support.difference(field.geometry), projection),
    )
