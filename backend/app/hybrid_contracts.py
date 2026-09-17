"""Typed Hybrid wire contract, independent of the legacy grid browser payload."""
from typing import Literal

from pydantic import Field

from .algorithms.hybrid_isochrone.models import HybridConfig
from .contracts import WireModel, Origin, Geometry, TaskResultResponse


class HybridRequest(WireModel):
    origin: Origin
    coordinate_system: Literal["bd09ll"]
    config: HybridConfig = Field(default_factory=HybridConfig)
    client_request_id: str = Field(min_length=1, max_length=100, pattern=r"^[a-zA-Z0-9_-]+$")


class HybridReadiness(WireModel):
    mode: Literal["full", "degraded"]
    graph_available: bool
    data_version_matches: bool
    coverage_available: bool
    origin_in_coverage: bool
    extent_in_coverage: bool
    obstacle_layer_available: bool
    risk_layer_available: bool
    warnings: list[str]


class HybridTimings(WireModel):
    preparation: float = Field(ge=0)
    obstacle_load: float = Field(ge=0)
    requests: float = Field(ge=0)
    geometry_rebuilds: float = Field(ge=0)
    compute_total: float = Field(ge=0)
    task_total: float = Field(ge=0)


class HybridIsochrone(WireModel):
    algorithm: Literal["hybrid"] = "hybrid"
    algorithm_version: str
    coordinate_system: Literal["bd09ll"] = "bd09ll"
    quality: Literal["usable", "partial", "insufficient"]
    coverage_policy: Literal["continuous_land_interior"]
    geometry: Geometry | None
    evidence_geometry: Geometry | None
    inferred_fill_geometry: Geometry | None
    unknown_region: Geometry | None
    evidence_unknown_region: Geometry | None
    computation_extent: Geometry
    extent_truncated: bool
    requests_used: int = Field(ge=0, le=400)
    valid_baidu_samples: int = Field(ge=0)
    invalid_baidu_samples: int = Field(ge=0)
    unknown_samples: int = Field(ge=0)
    boundary_error_estimate: float | None
    stop_reason: str
    warnings: list[str]
    config: HybridConfig
    readiness: HybridReadiness
    timing_seconds: HybridTimings


class HybridResultResponse(TaskResultResponse):
    algorithm: HybridIsochrone
    isochrone: HybridIsochrone
    config_hash: str
    result_hash: str


class HybridError(WireModel):
    code: str
    message: str
