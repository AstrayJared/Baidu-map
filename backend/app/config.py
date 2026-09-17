from pathlib import Path
from urllib.parse import urlsplit
from typing import Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

BACKEND_DIR = Path(__file__).resolve().parents[1]


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=BACKEND_DIR / ".env", env_file_encoding="utf-8", extra="ignore"
    )
    baidu_map_ak: SecretStr = SecretStr("")
    analysis_provider: Literal["baidu", "synthetic"] = "baidu"
    analysis_qps: float | None = Field(default=None, gt=0, allow_inf_nan=False)
    osm_pbf_path: Path = BACKEND_DIR.parent / "data/osm/shanghai.osm.pbf"
    osm_graph_cache_path: Path = BACKEND_DIR.parent / "data/osm/shanghai.osm-cache"
    osm_data_version: str = "unconfigured"
    osm_metric_crs: str = "EPSG:32651"
    walk_speed_mps: float = Field(default=1.3, gt=0, allow_inf_nan=False)
    snap_max_distance_m: float = Field(default=200, ge=0, allow_inf_nan=False)
    isochrone_buffer_m: float = Field(default=25, gt=0, allow_inf_nan=False)
    osm_coverage_boundary_path: Path | None = None
    osm_coverage_margin_m: float = Field(default=100, ge=0, allow_inf_nan=False)
    hybrid_ledger_dir: Path = BACKEND_DIR / ".hybrid-ledgers"
    hybrid_risk_path: Path = BACKEND_DIR.parent / "data/osm/shanghai.risks.geojson"
    hybrid_obstacle_path: Path = BACKEND_DIR.parent / "data/osm/shanghai.obstacles.geojson"

    @field_validator("osm_pbf_path", "osm_graph_cache_path", "osm_coverage_boundary_path", "hybrid_ledger_dir", "hybrid_risk_path", "hybrid_obstacle_path", mode="before")
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

    @field_validator("analysis_qps", mode="before")
    @classmethod
    def empty_qps(cls, value):
        return None if isinstance(value, str) and not value.strip() else value

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
