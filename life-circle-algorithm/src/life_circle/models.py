from __future__ import annotations

import asyncio
import math
import time
from dataclasses import asdict, dataclass, field
from typing import Protocol

Point = tuple[float, float]


@dataclass(frozen=True)
class IsochroneRequest:
    origin: Point
    coordinate_system: str
    threshold: float = 900
    extent: float = 1600
    max_extent: float = 3200
    coarse_size: float = 400
    min_size: float = 50
    raster_size: float = 25
    boundary_band: float = 120
    residual_target: float = 30
    min_spacing: float = 25
    budget: int = 400
    exploration_fraction: float = 0.1
    max_attempts: int = 2
    timeout: float = 8
    deadline_seconds: float = 600
    concurrency: int = 2
    qps: float | None = None
    seed: int = 20260911
    active_sampling: bool = True
    expand: bool = True
    time_bands: tuple[int, ...] = (5, 10, 15)
    config_version: str = "adaptive-v1"

    def __post_init__(self):
        if self.coordinate_system != "bd09ll":
            raise ValueError("首版仅支持显式 bd09ll 坐标系")
        if len(self.origin) != 2 or not all(type(v) in (int, float) and math.isfinite(v) for v in self.origin):
            raise ValueError("中心点必须为有限 [经度, 纬度]")
        if not (-180 <= self.origin[0] <= 180 and -85 < self.origin[1] < 85):
            raise ValueError("中心点超出局部投影支持范围")
        for name in ("extent", "max_extent", "coarse_size", "min_size", "raster_size", "boundary_band", "residual_target", "min_spacing", "timeout", "deadline_seconds"):
            value = getattr(self, name)
            if not math.isfinite(value) or value <= 0:
                raise ValueError(f"{name} 必须为有限正数")
        if self.threshold != 900:
            raise ValueError("首版业务阈值固定为 900 秒")
        if not 0 <= self.exploration_fraction <= 1:
            raise ValueError("探索比例必须在 0 到 1 之间")
        if self.qps is not None and (not math.isfinite(self.qps) or self.qps <= 0):
            raise ValueError("QPS 必须为有限正数")
        if any(type(getattr(self, n)) is not int or getattr(self, n) < 1 for n in ("budget", "concurrency", "max_attempts")):
            raise ValueError("预算、并发和尝试次数必须为正整数")
        if self.max_attempts > 2:
            raise ValueError("首版最多尝试两次")
        ratio = self.coarse_size / self.min_size
        if ratio < 1 or not math.log2(ratio).is_integer():
            raise ValueError("最小网格必须能由粗格二分得到")
        for extent in (self.extent, self.max_extent):
            if not (2 * extent / self.coarse_size).is_integer():
                raise ValueError("计算范围必须对齐粗格")
        if self.max_extent < self.extent:
            raise ValueError("最大范围不能小于初始范围")
        if not self.time_bands or any(type(value) is not int or value not in (5, 10, 15) for value in self.time_bands):
            raise ValueError("时间分层仅支持 5、10、15 分钟")
        if len(set(self.time_bands)) != len(self.time_bands):
            raise ValueError("时间分层不能重复")
        object.__setattr__(self, "origin", tuple(self.origin))
        object.__setattr__(self, "time_bands", tuple(sorted(self.time_bands)))


@dataclass(frozen=True)
class RouteObservation:
    destination: Point
    duration: float | None = None
    reason: str | None = None
    collected_at: float = field(default_factory=time.time)
    attempts: int = 0
    endpoint_verified: bool = True
    route_origin: Point | None = None
    route_destination: Point | None = None
    request_origin: Point | None = None
    distance_m: float | None = None
    route_path: list = field(default_factory=list)
    # Route evidence survives rejection at the requested coordinate.
    observed_duration: float | None = None
    origin_offset_m: float | None = None
    destination_offset_m: float | None = None

    def __post_init__(self):
        raw = self.observed_duration if self.observed_duration is not None else self.duration
        object.__setattr__(self, "observed_duration", raw if type(raw) in (int, float) and math.isfinite(raw) and raw >= 0 else None)
        if type(self.duration) not in (int, float) or not math.isfinite(self.duration) or self.duration < 0:
            object.__setattr__(self, "duration", None)
            object.__setattr__(self, "reason", self.reason or "invalid_duration")
        elif self.reason is not None:
            object.__setattr__(self, "duration", None)

    @property
    def reachable(self):
        return None if self.duration is None else self.duration <= 900


class CancelToken:
    def __init__(self):
        self.event = asyncio.Event()

    def cancel(self):
        self.event.set()

    @property
    def cancelled(self):
        return self.event.is_set()


class Provider(Protocol):
    network: bool
    identity: tuple

    async def query_walking_time(self, origin: Point, destination: Point, deadline: float) -> RouteObservation: ...


@dataclass
class Statistics:
    requests: int = 0
    network_requests: int = 0
    unique_positions: int = 0
    cache_hits: int = 0
    retries: int = 0
    failures: dict[str, int] = field(default_factory=dict)
    latencies: list[float] = field(default_factory=list)
    total_seconds: float = 0
    compute_seconds: float = 0
    network_wait_seconds: float = 0
    cells_by_size: dict[str, int] = field(default_factory=dict)
    unknown_area: float = 0
    unfinished_boundary: int = 0
    active_points: int = 0
    exploration_requests: int = 0
    matrix_route_pairs: int = 0
    detailed_route_requests: int = 0
    sends: int = 0
    responses: int = 0
    terminations: int = 0
    transport_failures: int = 0
    cancellations: int = 0
    endpoint_invalid: int = 0
    unfinished_boundary_length_m: float = 0


@dataclass(frozen=True)
class ProgressSnapshot:
    stage: str
    requests: int
    network_requests: int
    budget: int
    elapsed_seconds: float


@dataclass
class IsochroneResult:
    geometry: dict | None
    uncertain_region: dict
    unknown_region: dict
    computation_extent: dict
    quality: str
    stop_reason: str
    statistics: Statistics
    warnings: list[str]
    config: IsochroneRequest
    # Local Shapely objects support offline evaluation, excluded from the wire format.
    local_geometry: object = field(repr=False, default=None)
    local_unknown: object = field(repr=False, default=None)
    time_bands: list = field(default_factory=list)
    sample_observations: list = field(default_factory=list, repr=False)
    unreachable_region: dict | None = None
    metadata: dict = field(default_factory=dict)

    def to_dict(self):
        return {
            "coordinateSystem": "bd09ll", "geometry": self.geometry,
            "uncertainRegion": self.uncertain_region, "unknownRegion": self.unknown_region,
            "unreachableRegion": self.unreachable_region,
            "computationExtent": self.computation_extent, "quality": self.quality,
            "stopReason": self.stop_reason, "statistics": asdict(self.statistics),
            "warnings": self.warnings, "config": asdict(self.config),
            "timeBands": self.time_bands,
            **self.metadata,
        }
