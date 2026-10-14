from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from pydantic import Field, SecretStr, ValidationInfo, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]

# The quota tier that applies before the date below and the conservative tier
# that replaces it afterwards. The switch instant is an engineering choice, not
# a claim about when the console entitlement actually lapses.
DEFAULT_FALLBACK_AT = "2026-09-30T00:00:00+08:00"

# The account's own ceilings, as its console reported them on 2026-10-09:
# ``地点检索`` 50,000 calls/day at 10 QPS, ``步行路线规划(轻量)`` 5,000 calls/day at
# 3 QPS (balance ¥0.00, nothing purchased — these are the free-tier allowances).
# They are recorded here so a deployment's own settings can be checked against the
# real bound instead of against memory. This application's ledger bounds only what
# *this application* spends; a configured ceiling above the account's is not
# headroom, it is a setting that can only fail upstream.
VERIFIED_ENTITLEMENT = {
    "placeQps": 10.0,
    "placeDailyCalls": 50_000,
    "walkingQps": 3.0,
    "walkingDailyCalls": 5_000,
}


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )
    baidu_map_ak: SecretStr = SecretStr("")
    analysis_provider: Literal["baidu", "synthetic"] = "baidu"
    analysis_qps: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # OSM is an optional, region-specific runtime package. A deployment must
    # select one explicitly; never silently load Shanghai data for another area.
    osm_region_id: str | None = None
    osm_pbf_path: Path | None = None
    osm_graph_cache_path: Path | None = None
    osm_data_version: str = "unconfigured"
    osm_metric_crs: str = "EPSG:3857"
    walk_speed_mps: float = Field(default=1.3, gt=0, allow_inf_nan=False)
    snap_max_distance_m: float = Field(default=200, ge=0, allow_inf_nan=False)
    isochrone_buffer_m: float = Field(default=25, gt=0, allow_inf_nan=False)
    osm_coverage_boundary_path: Path | None = None
    osm_coverage_margin_m: float = Field(default=100, ge=0, allow_inf_nan=False)
    hybrid_ledger_dir: Path = BACKEND_DIR / ".hybrid-ledgers"
    checkup_dir: Path = BACKEND_DIR / ".checkups"
    # §3.4: how long a cached page, route or spatial result may be reused by a
    # task other than the one that stored it. Unconfigured means reuse stays
    # inside the storing task — a deployment's data-use agreement is never
    # assumed, and an entry is never permanently fresh.
    cache_freshness_seconds: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    # §5 B2 决策 2：保留期巡检的间隔。到期是一个事件，没有请求会因为它而到来，所以必须
    # 有东西定期去看；这个间隔就是"多久去看一眼"。它是部署参数而不是常数，因为一个测试
    # 必须能通过**真实的启动钩子**验证这件事，而不是另写一条只在测试里存在的调用路径。
    retention_maintenance_seconds: float = Field(default=60.0, gt=0, allow_inf_nan=False)
    # Per-service route and place pools. Both algorithms and the facility stages
    # draw from these; nothing allocates quota outside this entry. Both ceilings are
    # set to what the account actually grants (``VERIFIED_ENTITLEMENT``): the walking
    # gate used to ask for 16 QPS against a 3 QPS entitlement, which is a 429 waiting
    # for a busy day rather than headroom.
    baidu_direction_qps: float = Field(default=3, gt=0, allow_inf_nan=False)
    baidu_place_qps: float = Field(default=10, gt=0, allow_inf_nan=False)
    baidu_direction_max_inflight: int = Field(default=1, ge=1, le=1)
    baidu_place_max_inflight: int = Field(default=1, ge=1, le=1)
    # What *this application* may spend in a day — 4% of the account's 50,000, so the
    # browser SDK, other applications and future manual use keep the rest. It is no
    # longer 80: the operator raised it on 2026-10-09 so that one checkup can reach the
    # 80% coverage goal, which the measured cost of a moderate area (203 dispatches)
    # never could under 80. Raising it further is a business decision, not a code one.
    baidu_place_daily_budget: int = Field(default=2000, ge=0)
    baidu_matrix_enabled: Literal[False] = False
    baidu_geoconv_qps: float = Field(default=1, gt=0, allow_inf_nan=False)
    baidu_geoconv_daily_budget: int = Field(default=100, ge=0)
    baidu_quota_fallback_at: datetime = DEFAULT_FALLBACK_AT
    # The two tiers now carry the same numbers. The switch was introduced on the
    # assumption that the console entitlement would lapse, and the console shows it has
    # not; what used to be a 20x cliff at a clock boundary is therefore gone. The knob
    # stays because removing a configuration surface is its own decision, and it is
    # still the place a future downgrade would be expressed.
    baidu_fallback_direction_qps: float = Field(default=3, gt=0, allow_inf_nan=False)
    baidu_fallback_place_qps: float = Field(default=10, gt=0, allow_inf_nan=False)
    baidu_fallback_place_daily_budget: int = Field(default=2000, ge=0)
    quota_ledger_path: Path = BACKEND_DIR / ".quota/quota.sqlite3"
    hybrid_risk_path: Path | None = None
    hybrid_obstacle_path: Path | None = None
    # Field-reviewed corrections to the obstacle layer (water_review.py). Each file
    # applies to one OSM extract and only inside its own extent.
    water_review_dir: Path | None = None

    @field_validator("osm_pbf_path", "osm_graph_cache_path", "osm_coverage_boundary_path", "hybrid_ledger_dir", "hybrid_risk_path", "hybrid_obstacle_path", "water_review_dir", "checkup_dir", "quota_ledger_path", mode="before")
    @classmethod
    def osm_paths(cls, value):
        if value is None or value == "":
            return None
        path = Path(value)
        return path if path.is_absolute() else BACKEND_DIR / path
    cors_origins: list[str] = [
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    ]

    @field_validator("baidu_map_ak", mode="before")
    @classmethod
    def strip_ak(cls, value):
        if isinstance(value, str):
            return value.strip()
        return value

    @field_validator("analysis_qps", "cache_freshness_seconds", mode="before")
    @classmethod
    def empty_optional_numbers(cls, value):
        # An empty value means "unconfigured", which is not the same as zero.
        return None if isinstance(value, str) and not value.strip() else value

    @field_validator("osm_region_id", "osm_data_version", mode="before")
    @classmethod
    def empty_optional_osm_values(cls, value, info: ValidationInfo):
        if isinstance(value, str) and not value.strip():
            return None if info.field_name == "osm_region_id" else "unconfigured"
        return value

    @field_validator("baidu_quota_fallback_at")
    @classmethod
    def explicit_fallback_instant(cls, value):
        # An offset is required: the switch instant is a wall-clock instant in
        # the deployment's own zone, not a naive local timestamp.
        if value.tzinfo is None:
            raise ValueError("BAIDU_QUOTA_FALLBACK_AT must include an UTC offset")
        return value

    @field_validator("cors_origins")
    @classmethod
    def explicit_origins(cls, values):
        for value in values:
            url = urlsplit(value)
            if (
                url.scheme not in {"http", "https"}
                or not url.hostname
                or "*" in value
                or url.username is not None
                or url.password is not None
                or url.path
                or url.query
                or url.fragment
            ):
                raise ValueError("CORS_ORIGINS must contain explicit origins only")
            _ = url.port
        return values

    @property
    def ak_configured(self) -> bool:
        return bool(self.baidu_map_ak.get_secret_value())


def load_settings() -> Settings:
    try:
        return Settings()
    except Exception:
        # Validation exceptions may contain environment values; never propagate them.
        raise RuntimeError("Invalid backend configuration; check .env format.") from None
