"""Settings loaded from .env via pydantic. Required values fail fast with a clear message."""
from __future__ import annotations

from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path
from typing import Any

from pydantic import SecretStr, ValidationError, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

PROJECT_ROOT = Path(__file__).resolve().parents[2]

# CRS used for every distance calculation (UTM zone 17N covers the Shenandoah at ~78.3 W).
DISTANCE_CRS = "EPSG:32617"
# CRS of the NHDPlus GeoPackage and of ECHO/TRI lat/lon values.
SOURCE_CRS = "EPSG:4269"

# EROM velocity (va) is treated as ft/s: a documented assumption, see docs/data_notes.md.
FEET_TO_METERS = 0.3048
# EROM uses -9998 for "no velocity"; anything at or below zero is treated as missing.
MIN_VALID_VELOCITY_FPS = 0.0

_REQUIRED_PATHS = ("nhdplus_gpkg_path", "echo_npdes_dir", "echo_effluent_dir", "tri_csv_path")
_PATH_FIELDS = _REQUIRED_PATHS + (
    "dem_tif_path",
    "naip_path",
    "yolo_dataset_dir",
    "simulated_footage_dir",
    "cache_dir",
)


class ConfigError(RuntimeError):
    """Raised when .env is missing required values. The message never contains values."""


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=PROJECT_ROOT / ".env",
        env_file_encoding="utf-8",
        env_ignore_empty=True,
        extra="ignore",
    )

    simulation_mode: bool = True

    # Required
    nhdplus_gpkg_path: Path
    echo_npdes_dir: Path
    echo_effluent_dir: Path
    tri_csv_path: Path
    reach_comid: int  # an NHDPlusID; the env name is kept from the brief
    nwis_site_id: str
    noaa_station_id: str
    # Gauge location (NAD83) and name as published by USGS for NWIS_SITE_ID.
    gauge_latitude: float = 38.9768
    gauge_longitude: float = -78.3361
    gauge_name: str = "North Fork Shenandoah River near Strasburg, VA"
    rain_threshold_inches: float
    rain_window_hours: int
    scenario_timestamp: datetime  # spill start for the demo; also ends the violation window
    storm_case_date: date = date(2026, 9, 22)  # real observed storm used for the storm_runoff case

    # Optional: the pipeline falls back or skips when these are absent
    groq_api_key: SecretStr | None = None
    groq_model: str | None = None
    noaa_cdo_token: SecretStr | None = None
    roboflow_api_key: SecretStr | None = None
    backboard_api_key: SecretStr | None = None  # optional incident memory; skipped when unset
    roboflow_model_id: str = "illegal-dumping-detection-dd6wk-3t0jf/1"
    roboflow_api_url: str = "https://serverless.roboflow.com"
    dem_tif_path: Path | None = None
    naip_path: Path | None = None
    yolo_dataset_dir: Path | None = None
    simulated_footage_dir: Path = Path("data/simulated_footage")
    cache_dir: Path = Path("cache")
    random_seed: int = 42
    violation_window_years: int = 5
    # API: comma-separated origins allowed by CORS (the frontend's dev server), and the SSE replay
    # speed in scenario seconds per wall-clock second (600: one 15-min sensor tick every 1.5 s).
    cors_origins: str = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000"
    stream_speed: float = 600.0

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]

    @field_validator(*_PATH_FIELDS, mode="after")
    @classmethod
    def _resolve_relative(cls, v: Path | None) -> Path | None:
        if v is None or v.is_absolute():
            return v
        return (PROJECT_ROOT / v).resolve()

    @field_validator("rain_threshold_inches", "rain_window_hours", "violation_window_years", mode="after")
    @classmethod
    def _positive(cls, v: float) -> float:
        if v <= 0:
            raise ValueError("must be greater than 0")
        return v

    @field_validator("scenario_timestamp", mode="after")
    @classmethod
    def _utc(cls, v: datetime) -> datetime:
        return v.replace(tzinfo=timezone.utc) if v.tzinfo is None else v.astimezone(timezone.utc)

    @model_validator(mode="after")
    def _required_paths_exist(self) -> "Settings":
        missing = [n.upper() for n in _REQUIRED_PATHS if not getattr(self, n).exists()]
        if missing:
            raise ValueError("path does not exist for: " + ", ".join(missing))
        return self

    @property
    def reach_nhdplusid(self) -> int:
        return self.reach_comid

    @property
    def naip_available(self) -> bool:
        return self.naip_path is not None and self.naip_path.exists()

    @property
    def dem_available(self) -> bool:
        return self.dem_tif_path is not None and self.dem_tif_path.exists()

    @property
    def yolo_available(self) -> bool:
        return self.yolo_dataset_dir is not None and self.yolo_dataset_dir.exists()

    @property
    def groq_configured(self) -> bool:
        return bool(self.groq_api_key and self.groq_model)

    def public_dict(self) -> dict[str, Any]:
        """Settings with secrets masked. Safe to print or log."""
        out: dict[str, Any] = {}
        for name, value in self.model_dump().items():
            if isinstance(value, SecretStr) or name.endswith(("_api_key", "_token")):
                out[name] = "***set***" if value else None
            else:
                out[name] = str(value) if isinstance(value, Path) else value
        return out


def load_settings(env_file: Path | str | None = ..., **overrides: Any) -> Settings:  # type: ignore[assignment]
    """Build Settings, turning pydantic errors into a ConfigError that lists names only."""
    kwargs: dict[str, Any] = dict(overrides)
    if env_file is not ...:
        kwargs["_env_file"] = env_file
    try:
        return Settings(**kwargs)
    except ValidationError as exc:
        problems = []
        for err in exc.errors():
            loc = ".".join(str(p) for p in err["loc"]).upper() or "settings"
            if err["type"] == "missing":
                problems.append(f"{loc}: missing or empty")
            else:
                problems.append(f"{loc}: {err['msg']}")  # msg never echoes the input value
        raise ConfigError(
            "Invalid or incomplete .env. Fix these and see .env.example:\n  - "
            + "\n  - ".join(problems)
        ) from None


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
