"""Versioned geographic wire contract; separate from the legacy canvas demo."""
from datetime import datetime, timezone
from typing import Literal
from uuid import uuid4

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, model_validator

from .rules import DistanceRule

MajorCategory = Literal["shopping", "medical", "education"]
MinorCategory = Literal["market", "supermarket", "pharmacy", "hospital_pharmacy", "school"]
# Kept as a public alias for older N04/N05 callers.
Category = MinorCategory
Status = Literal["complete", "partial", "failed", "empty"]
AsyncStatus = Literal["running", "cancelling", "completed", "cancelled", "failed"]

MINOR_TO_MAJOR: dict[str, str] = {
    "market": "shopping", "supermarket": "shopping",
    "pharmacy": "medical", "hospital_pharmacy": "medical",
    "school": "education",
}


def map_business_status(*, quality: str, facilities_status: str,
                        facilities: list | None = None) -> Status:
    """Map module evidence to the four business result states.

    A successful async transport is only ``partial`` while a module is not
    integrated. ``empty`` means an integrated module ran and returned no
    facilities; it is never used for missing or unknown evidence.
    """
    if quality in ("insufficient", "failed"):
        return "failed"
    if quality != "usable":
        return "partial"
    if facilities_status == "complete" and facilities is not None:
        return "empty" if facilities == [] else "complete"
    return "partial"


class WireModel(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False, populate_by_name=True)


class Origin(WireModel):
    lng: float = Field(ge=-180, le=180)
    lat: float = Field(gt=-85, lt=85)


class Geometry(WireModel):
    type: Literal["Polygon", "MultiPolygon"]
    coordinates: list
    coordinate_system: Literal["bd09ll"] = Field(
        default="bd09ll",
        alias="coordinateSystem",
        validation_alias=AliasChoices("coordinate_system", "coordinateSystem"),
    )


class Issue(WireModel):
    code: str
    message: str
    scope: str
    severity: Literal["warning", "error", "pending"] = "warning"


class CategoryLevels(WireModel):
    category: Category
    major_category: MajorCategory
    minor_category: MinorCategory

    @model_validator(mode="after")
    def consistent_categories(self):
        if self.category != self.minor_category or self.major_category != MINOR_TO_MAJOR[self.minor_category]:
            raise ValueError("Category fields must describe the same facility category")
        return self


class PoiEvidence(WireModel):
    """Strict requested-OD evidence, not geometry membership or 1km service coverage."""
    version: Literal["1.0"] = "1.0"
    facility_id: str = Field(alias="facilityId", min_length=1)
    status: Literal["pending", "verified_reachable", "verified_unreachable"]
    reason: str | None
    duration: float | None = Field(ge=0)
    observed_duration: float | None = Field(alias="observedDuration", ge=0)
    endpoint_verified: bool = Field(alias="endpointVerified")
    request_origin: tuple[float, float] = Field(alias="requestOrigin")
    destination: tuple[float, float]
    route_origin: tuple[float, float] | None = Field(alias="routeOrigin")
    route_destination: tuple[float, float] | None = Field(alias="routeDestination")
    origin_offset_m: float | None = Field(alias="originOffsetM", ge=0)
    destination_offset_m: float | None = Field(alias="destinationOffsetM", ge=0)

    @model_validator(mode="after")
    def consistent_evidence(self):
        from life_circle.coordinates import LocalProjection
        import math
        for point in (self.request_origin, self.destination, self.route_origin, self.route_destination):
            if point is not None:
                Origin(lng=point[0], lat=point[1])
        if self.endpoint_verified and (self.route_origin is None or self.route_destination is None):
            raise ValueError("Parsed endpoints must be present")
        if self.status == "pending":
            if self.duration is not None or self.reason is None:
                raise ValueError("Pending requires a reason and no effective duration")
        else:
            if self.reason is not None or self.duration is None or self.observed_duration != self.duration or not self.endpoint_verified:
                raise ValueError("Verified state requires valid requested-route evidence")
            for actual, requested in ((self.route_origin, self.request_origin), (self.route_destination, self.destination)):
                if math.dist(LocalProjection(requested).to_local(actual), (0, 0)) > 1e-5:
                    raise ValueError("Strict endpoints must match")
            if any(offset is not None and offset > 1e-5 for offset in (self.origin_offset_m, self.destination_offset_m)):
                raise ValueError("Strict offsets must match")
            if (self.duration <= 900) != (self.status == "verified_reachable"):
                raise ValueError("Status must match the 900s threshold")
        return self


class Facility(CategoryLevels):
    id: str
    name: str
    category: Category
    major_category: MajorCategory
    minor_category: MinorCategory
    location: Origin
    in_circle: bool | None
    # Absent/null is an explicitly unsupported legacy contract, never pending.
    poi_evidence: PoiEvidence | None = Field(default=None, alias="poiEvidence", json_schema_extra={"x-legacy-optional": True})

    @model_validator(mode="after")
    def evidence_belongs_to_facility(self):
        if self.poi_evidence is not None:
            target = (round(self.location.lng, 6), round(self.location.lat, 6))
            if self.poi_evidence.facility_id != self.id or self.poi_evidence.destination != target:
                raise ValueError("POI evidence must belong to this facility")
        return self

    @model_validator(mode="before")
    @classmethod
    def fill_category_levels(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            minor = value.get("minor_category") or value.get("minorCategory") or value.get("category")
            if minor:
                value.setdefault("category", minor)
                value.setdefault("minor_category", minor)
                value.setdefault("major_category", MINOR_TO_MAJOR.get(minor))
        return value


class CategoryResult(CategoryLevels):
    category: Category
    major_category: MajorCategory
    minor_category: MinorCategory
    query_status: Literal["complete", "unknown", "failed"]
    count_in_circle: int | None = Field(ge=0)
    service_status: Literal["covered", "blind", "unknown"]

    @model_validator(mode="before")
    @classmethod
    def fill_category_levels(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            minor = value.get("minor_category") or value.get("minorCategory") or value.get("category")
            if minor:
                value.setdefault("category", minor)
                value.setdefault("minor_category", minor)
                value.setdefault("major_category", MINOR_TO_MAJOR.get(minor))
        return value


class BlindPoint(CategoryLevels):
    location: Origin
    category: Category
    major_category: MajorCategory
    minor_category: MinorCategory
    status: Literal["covered", "blind", "unknown"]
    evidence: str

    @model_validator(mode="before")
    @classmethod
    def fill_category_levels(cls, value):
        if isinstance(value, dict):
            value = dict(value)
            minor = value.get("minor_category") or value.get("minorCategory") or value.get("category")
            if minor:
                value.setdefault("category", minor)
                value.setdefault("minor_category", minor)
                value.setdefault("major_category", MINOR_TO_MAJOR.get(minor))
        return value


class CoverageEvidence(WireModel):
    category: MajorCategory
    status: Literal["covered", "blind", "unknown"]
    facility_id: str | None = None
    distance_m: float | None = None
    reason: str


class AssessmentPoint(WireModel):
    location: Origin
    duration_s: float
    categories: list[CoverageEvidence]


class QueryEvidence(WireModel):
    category: MinorCategory
    query: str
    status: Literal["complete", "partial", "failed", "truncated"]
    pages: int
    returned: int
    excluded: int
    invalid: int
    total: int | None
    reason: str | None


class RouteEvidence(WireModel):
    distance_m: float | None
    duration_s: float | None
    endpoint_verified: bool
    reason: str | None
    path: list[list[float]]
    poi_evidence: PoiEvidence | None = Field(default=None, alias="poiEvidence", json_schema_extra={"x-legacy-optional": True})

    @model_validator(mode="after")
    def valid_duration(self):
        if self.reason is not None and self.duration_s is not None:
            raise ValueError("Failure reason invalidates requested-route duration")
        if self.poi_evidence is not None and self.poi_evidence.status != "pending":
            if self.reason is not None or self.duration_s != self.poi_evidence.duration or not self.endpoint_verified:
                raise ValueError("Route and strict evidence must describe the same observation")
        return self


class FacilityAnalysis(WireModel):
    status: Literal["complete", "partial", "failed"]
    queries: list[QueryEvidence]
    assessments: list[AssessmentPoint]
    candidate_points: int
    assessed_points: int
    unassessed_points: int
    network_requests: int
    elapsed_seconds: float
    search_radius_m: int
    routes: dict[str, RouteEvidence] = Field(default_factory=dict)
    service_blind_regions: dict[MajorCategory, Geometry] = Field(default_factory=dict, alias="serviceBlindRegions")
    warnings: list[str]


class Data(WireModel):
    geometry: Geometry | None = None
    evidence_geometry: Geometry | None = Field(default=None, alias="evidenceGeometry")
    inferred_region: Geometry | None = Field(default=None, alias="inferredRegion")
    uncertain_region: Geometry | None = None
    unknown_region: Geometry | None = None
    computation_extent: Geometry | None = None
    facilities: list[Facility] | None = None
    categories: list[CategoryResult] = Field(default_factory=lambda: [
        CategoryResult(category=c, major_category=MINOR_TO_MAJOR[c], minor_category=c,
                       query_status="unknown", count_in_circle=None, service_status="unknown")
        for c in ("market", "pharmacy", "school")
    ])
    blind_points: list[BlindPoint] | None = None
    blind_region: Geometry | None = None
    report: str | None = None


class Rules(WireModel):
    version: str = "n04-v1"
    time_threshold_s: Literal[900] = 900
    time_inclusive: Literal[True] = True
    time_tolerance_s: float = 0
    time_confirmation: Literal[
        "implementation_only", "mock_only", "baidu_sampled",
    ] = "implementation_only"
    distance: DistanceRule = Field(default_factory=DistanceRule)


class AnalysisResponse(WireModel):
    schema_version: Literal["1.0"] = "1.0"
    analysis_id: str = Field(default_factory=lambda: str(uuid4()))
    generated_at: datetime = Field(default_factory=lambda: datetime.now(timezone.utc))
    status: Status
    source: Literal["mock", "synthetic", "system"]
    algorithm_version: str | None = None
    data_updated_at: datetime | None = None
    origin: Origin | None = None
    coordinate_system: Literal["bd09ll"] = "bd09ll"
    coordinate_order: Literal["longitude,latitude"] = "longitude,latitude"
    units: dict[str, str] = Field(default_factory=lambda: {"distance": "m", "duration": "s", "area": "m2"})
    rules: Rules = Field(default_factory=Rules)
    data: Data = Field(default_factory=Data)
    algorithm: dict | None = None
    warnings: list[Issue] = Field(default_factory=list)
    errors: list[Issue] = Field(default_factory=list)


class AnalysisRequest(WireModel):
    origin: Origin
    coordinate_system: Literal["bd09ll"]


class OsmOfflineRequest(AnalysisRequest):
    algorithm: Literal["osm_offline"] = "osm_offline"
    threshold: Literal[900] = 900


class SyntheticRequest(AnalysisRequest):
    scenario: Literal["plane", "local_failure", "global_failure"] = "plane"
    budget: int = Field(default=400, ge=144, le=800, strict=True)
    distance_rule: DistanceRule = Field(default_factory=DistanceRule)


class TaskStatusResponse(WireModel):
    """统一异步任务响应；business_status 是业务结果，status 是传输任务状态。"""
    schema_version: Literal["1.0"] = "1.0"
    response_type: Literal["task"] = Field(default="task", alias="responseType")
    task_id: str = Field(alias="taskId")
    status: AsyncStatus
    business_status: Status | None = Field(default=None, alias="businessStatus")
    stage: str
    requests: int = Field(ge=0)
    network_requests: int = Field(ge=0, alias="networkRequests")
    budget: int = Field(ge=1, le=800)
    elapsed_seconds: float = Field(ge=0, alias="elapsedSeconds")
    data_source: Literal["synthetic", "baidu_walking"] = Field(alias="dataSource")
    error: str | None = None


class TaskResultResponse(WireModel):
    """异步结果与旧地理分析契约共享坐标、单位和数据语义。"""
    schema_version: Literal["1.0"] = "1.0"
    response_type: Literal["result"] = Field(default="result", alias="responseType")
    task_id: str = Field(alias="taskId")
    task_status: Literal["completed"] = Field(alias="taskStatus")
    status: Status
    business_status: Status = Field(alias="businessStatus")
    data_source: Literal["synthetic", "baidu_walking"] = Field(alias="dataSource")
    center: Origin
    generated_at: float = Field(alias="generatedAt", gt=0)
    facilities_status: Literal["not_integrated", "complete", "partial", "failed"] = Field(alias="facilitiesStatus")
    facility_analysis: FacilityAnalysis | None = Field(default=None, alias="facilityAnalysis")
    coordinate_system: Literal["bd09ll"] = Field(default="bd09ll", alias="coordinateSystem")
    coordinate_order: Literal["longitude,latitude"] = Field(default="longitude,latitude", alias="coordinateOrder")
    units: dict[str, str] = Field(default_factory=lambda: {"distance": "m", "duration": "s", "area": "m2"})
    rules: Rules = Field(default_factory=Rules)
    data: Data = Field(default_factory=Data)
    algorithm: dict | None = None
    warnings: list[Issue] = Field(default_factory=list)
    errors: list[Issue] = Field(default_factory=list)
    # Compatibility payload consumed by the current async browser client.
    isochrone: dict

    @model_validator(mode="after")
    def evidence_belongs_to_task(self):
        center = (self.center.lng, self.center.lat)
        facilities = {f.id: f for f in self.data.facilities or []}
        for facility in facilities.values():
            if facility.poi_evidence is not None and facility.poi_evidence.request_origin != center:
                raise ValueError("Facility evidence must originate from the task center")
        for fid, route in (self.facility_analysis.routes if self.facility_analysis else {}).items():
            evidence = route.poi_evidence
            if evidence is not None:
                if evidence.facility_id != fid or evidence.request_origin != center:
                    raise ValueError("Route evidence must belong to the task and route key")
                if self.data.facilities is not None:
                    facility = facilities.get(fid)
                    if facility is None or facility.poi_evidence != evidence:
                        raise ValueError("Facility and route evidence must share a snapshot")
        return self
