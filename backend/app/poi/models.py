from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic.alias_generators import to_camel
from app.test_origin import TEST_ORIGIN

Category = Literal['market', 'pharmacy', 'primary_school']
CATEGORIES = ('market', 'pharmacy', 'primary_school')


class WireModel(BaseModel):
    model_config = ConfigDict(extra='forbid', populate_by_name=True, alias_generator=to_camel,
                             allow_inf_nan=False, validate_default=True)


class Point(WireModel):
    lng: float = Field(ge=-180, le=180, strict=True)
    lat: float = Field(gt=-85, lt=85, strict=True)


class PoiCollectRequest(WireModel):
    schema_version: Literal['poi-v1'] = 'poi-v1'
    center: Point = Field(default_factory=lambda: Point(lng=TEST_ORIGIN[0], lat=TEST_ORIGIN[1]))
    coordinate_system: Literal['bd09ll']  # Caller must declare the coordinate system.
    analysis_half_width_meters: float = Field(default=1600, gt=0, le=1600, strict=True)
    search_margin_meters: float = Field(default=1000, ge=0, le=1000, strict=True)
    categories: list[Category] = Field(default_factory=lambda: list(CATEGORIES), min_length=1, max_length=3)
    query_profile_version: Literal['guodingyi-poi-v1'] = 'guodingyi-poi-v1'

    @field_validator('categories')
    @classmethod
    def distinct(cls, value):
        if len(value) != len(set(value)):
            raise ValueError('duplicate categories')
        return value


class RuntimeConfig(WireModel):
    run_id: str = Field(pattern=r'^[a-zA-Z0-9_-]{1,80}$')
    phase: Literal['smoke', 'collect'] = 'collect'
    total_budget: int = Field(default=300, gt=0, le=10000, strict=True)
    category_budgets: dict[Category, int] = Field(default_factory=lambda: dict.fromkeys(CATEGORIES, 100))
    qps: float | None = Field(default=None, gt=0, strict=True)
    deadline_seconds: float = Field(default=600, gt=0, le=600, strict=True)
    authorized: bool = Field(default=False, strict=True)
    approved_config_hash: str | None = None
    window_start: datetime | None = None
    window_end: datetime | None = None

    @field_validator('category_budgets', mode='before')
    @classmethod
    def budgets(cls, value):
        if not isinstance(value, dict) or set(value) != set(CATEGORIES):
            raise ValueError('explicit budget for every category required')
        if any(type(n) is not int or not 0 < n <= 10000 for n in value.values()):
            raise ValueError('positive integer budgets required')
        return value

    @field_validator('window_start', 'window_end')
    @classmethod
    def aware(cls, value):
        if value is not None and value.tzinfo is None:
            raise ValueError('timezone required')
        return value


class CollectionConfig(WireModel):
    request: PoiCollectRequest
    runtime: RuntimeConfig


class PoiCollectionResult(WireModel):
    schema_version: Literal['poi-v1'] = 'poi-v1'
    collection_id: str
    provider: str
    api_version: Literal['3.0'] = '3.0'
    data_source: Literal['synthetic', 'baidu_place']
    center: Point
    coordinate_system: Literal['bd09ll'] = 'bd09ll'
    analysis_extent: dict
    search_extent: dict
    started_at: str
    finished_at: str
    query_profile_version: str
    config_hash: str
    query_status: Literal['completed', 'partial', 'failed', 'cancelled']
    catalog_completeness: Literal['unverified'] = 'unverified'
    pois: list[dict]
    review_candidates: list[dict]
    excluded_candidates: list[dict]
    quarantine: list[dict]
    query_coverage: list[dict]
    counts_by_category: dict[Category, int]
    statistics: dict
    warnings: list[str]
    stop_reason: str | None
