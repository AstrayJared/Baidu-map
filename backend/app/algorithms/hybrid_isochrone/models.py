from dataclasses import asdict, dataclass, field
from enum import Enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator


class HybridConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, frozen=True)
    time_limit_seconds: Literal[900] = 900
    endpoint_offset_limit_m: float = Field(50, gt=0)
    initial_direction_count: int = Field(16, ge=4, le=64)
    analysis_half_width_m: float = Field(1200, gt=0, le=1200)
    initial_probe_distances_m: tuple[float, ...] = (400, 700, 1000, 1200)
    max_exploration_radius_m: float = Field(3200, gt=0, le=10000)
    boundary_tolerance_m: float = Field(50, gt=0)
    max_refinement_depth: int = Field(6, ge=0, le=12)
    max_baidu_requests: int = Field(400, ge=1, le=400)
    max_direction_count: int = Field(64, ge=4, le=256)
    angular_refinement_threshold_m: float = Field(250, gt=0)
    angular_refinement_ratio: float = Field(.25, gt=0)
    topology_risk_threshold: float = Field(.6, ge=0, le=1)
    topology_search_radius_m: float = Field(150, gt=0)
    max_triangle_edge_m: float = Field(400, gt=0)
    mixed_triangle_target_m: float = Field(100, gt=0)
    request_qps: float = Field(3, gt=0, le=20)
    deadline_seconds: float = Field(1800, gt=0, le=7200)
    failure_streak_limit: int = Field(5, ge=1, le=20)
    origin_endpoint_failure_streak_limit: int = Field(3, ge=1, le=10)
    interior_gap_radii_m: tuple[float, ...] = (25, 50, 100)
    bridge_display_width_m: float = Field(3, gt=0, le=10)

    @model_validator(mode="after")
    def consistent(self):
        import math
        ds = self.initial_probe_distances_m
        gaps = self.interior_gap_radii_m
        if any(not math.isfinite(v) or v <= 0 or v > 100 for v in gaps) or tuple(sorted(set(gaps))) != gaps:
            raise ValueError('gap radii must be unique increasing values in (0, 100]')
        if not ds or any(not math.isfinite(d) or d <= 0 for d in ds) or tuple(sorted(set(ds))) != ds:
            raise ValueError("probe distances must be positive, finite and strictly increasing")
        if ds[-1] > self.max_exploration_radius_m or self.max_direction_count < self.initial_direction_count:
            raise ValueError("inconsistent exploration limits")
        if self.mixed_triangle_target_m > self.max_triangle_edge_m:
            raise ValueError("mixed edge target exceeds support limit")
        return self


class Validity(str, Enum):
    REACHABLE = "VALID_REACHABLE"
    UNREACHABLE = "VALID_UNREACHABLE"
    OFFSET = "INVALID_ENDPOINT_OFFSET"
    API_ERROR = "API_ERROR"
    RATE_LIMIT = "RATE_LIMIT"
    UNKNOWN = "UNKNOWN"


@dataclass
class Evidence:
    validity: Validity = Validity.UNKNOWN
    reason: str | None = "unknown"
    duration: float | None = None
    returned_origin: tuple | None = None
    returned_destination: tuple | None = None
    origin_offset_m: float | None = None
    destination_offset_m: float | None = None
    http_status: int | None = None
    business_status: int | None = None
    transport_error_type: str | None = None

    @property
    def reachable(self):
        return True if self.validity == Validity.REACHABLE else False if self.validity == Validity.UNREACHABLE else None

    def to_dict(self):
        return {**asdict(self), "validity": self.validity.value, "reachable": self.reachable}


@dataclass
class Sample:
    point_id: str
    xy: tuple
    coordinate: tuple
    source: str
    reason: str
    refinement_level: int
    evidence: Evidence
    guidance: dict = field(default_factory=dict)

    @property
    def disagreement(self):
        return (self.evidence.reachable is not None and self.guidance.get("reachable") is not None
                and self.evidence.reachable != self.guidance["reachable"])

    def to_dict(self):
        return {"point_id": self.point_id, "xy": self.xy, "coordinate": self.coordinate,
                "request_coordinate": self.coordinate, "source": self.source, "reason": self.reason,
                "refinement_level": self.refinement_level, "evidence": self.evidence.to_dict(),
                "osm_guidance": self.guidance,
                "osm_baidu_disagreement": self.disagreement}
