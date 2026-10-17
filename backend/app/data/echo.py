"""ECHO ICIS-NPDES loaders: Virginia facilities, outfall points and effluent violations.

Each loader filters the raw CSV once and caches the result as parquet in the cache dir, keyed on
the source file size and mtime. Missing values come back as None (object columns), not NaN.
Violations are reported or alleged in ECHO; `is_reporting` rows are overdue reports, not pollution.
"""
from __future__ import annotations

import os
from datetime import date, datetime
from pathlib import Path
from typing import Callable

import pandas as pd

from ..config import get_settings

VA_BBOX = (36.0, 40.0, -84.0, -75.0)  # lat_min, lat_max, lon_min, lon_max; points outside are dropped

FACILITIES_FILE = "ICIS_FACILITIES.csv"
OUTFALLS_FILE = "NPDES_PERM_FEATURE_COORDS.csv"

FACILITY_COLUMNS = ["npdes_id", "name", "frs_id", "latitude", "longitude"]
OUTFALL_COLUMNS = ["npdes_id", "outfall", "latitude", "longitude"]
VIOLATION_COLUMNS = [
    "npdes_id", "outfall", "parameter", "violation_desc", "period_end", "is_numeric",
    "is_reporting", "exceedance_pct", "status", "dmr_value", "limit_value", "unit",
]
_VIOLATION_SOURCE = {
    "NPDES_ID": "npdes_id",
    "PERM_FEATURE_NMBR": "outfall",
    "PARAMETER_DESC": "parameter",
    "VIOLATION_DESC": "violation_desc",
    "MONITORING_PERIOD_END_DATE": "period_end",
    "EXCEEDENCE_PCT": "exceedance_pct",
    "RNC_RESOLUTION_DESC": "status",
    "DMR_VALUE_STANDARD_UNITS": "dmr_value",
    "LIMIT_VALUE_STANDARD_UNITS": "limit_value",
    "STANDARD_UNIT_DESC": "unit",
}


# ---- shared helpers (also used by tri.py) ----------------------------------------------------
def cached_frame(
    source: Path, cache_dir: str | Path, prefix: str, build: Callable[[Path], pd.DataFrame]
) -> pd.DataFrame:
    """Return build(source), cached as parquet keyed on the source's size and mtime_ns."""
    st = source.stat()
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    cache_file = cache / f"{prefix}_{source.stem}_{st.st_size}_{st.st_mtime_ns}.parquet"
    if cache_file.exists():
        try:
            return pd.read_parquet(cache_file)
        except Exception:  # corrupt or incompatible cache: rebuild below
            pass
    df = build(source)
    tmp = cache_file.with_suffix(".tmp")
    df.to_parquet(tmp, index=False)
    os.replace(tmp, cache_file)
    return df


def clean_str(s: pd.Series) -> pd.Series:
    """Strip text; blank or missing -> None (object dtype)."""
    out = s.astype("object").map(lambda v: v.strip() if isinstance(v, str) else None)
    return out.map(lambda v: v if v else None).astype("object")


def opt_float(s: pd.Series) -> pd.Series:
    """Parse numbers; unparseable or missing -> None (object dtype)."""
    num = pd.to_numeric(s, errors="coerce")
    return pd.Series([None if pd.isna(v) else float(v) for v in num], index=s.index, dtype="object")


def in_va_bbox(lat: pd.Series, lon: pd.Series) -> pd.Series:
    lat_min, lat_max, lon_min, lon_max = VA_BBOX
    return lat.between(lat_min, lat_max) & lon.between(lon_min, lon_max)


def _settings_paths(path, cache_dir, attr: str) -> tuple[Path, Path]:
    if path is None or cache_dir is None:
        s = get_settings()
        path = path if path is not None else getattr(s, attr)
        cache_dir = cache_dir if cache_dir is not None else s.cache_dir
    return Path(path), Path(cache_dir)


def _coords(df: pd.DataFrame, lat_col: str, lon_col: str) -> pd.DataFrame:
    df = df.assign(
        latitude=pd.to_numeric(df[lat_col], errors="coerce").astype("float64"),
        longitude=pd.to_numeric(df[lon_col], errors="coerce").astype("float64"),
    )
    return df[in_va_bbox(df["latitude"], df["longitude"])]


def _restore_none(df: pd.DataFrame, columns: list[str]) -> pd.DataFrame:
    # Parquet round-trips may bring strings back as a string dtype with NaN.
    for c in columns:
        df[c] = df[c].astype("object").where(df[c].notna(), None)
    return df


# ---- facilities ------------------------------------------------------------------------------
def _build_facilities(src: Path) -> pd.DataFrame:
    raw = pd.read_csv(
        src,
        dtype=str,
        encoding="latin-1",
        usecols=["NPDES_ID", "FACILITY_UIN", "FACILITY_NAME", "STATE_CODE",
                 "GEOCODE_LATITUDE", "GEOCODE_LONGITUDE"],
        keep_default_na=False,
    )
    raw = raw[raw["STATE_CODE"].str.strip() == "VA"]
    raw = _coords(raw, "GEOCODE_LATITUDE", "GEOCODE_LONGITUDE")
    df = pd.DataFrame({
        "npdes_id": clean_str(raw["NPDES_ID"]),
        "name": clean_str(raw["FACILITY_NAME"]),
        "frs_id": clean_str(raw["FACILITY_UIN"]),
        "latitude": raw["latitude"],
        "longitude": raw["longitude"],
    })
    df = df[df["npdes_id"].notna()].drop_duplicates("npdes_id", keep="first")
    return df.reset_index(drop=True)[FACILITY_COLUMNS]


def load_facilities(npdes_dir: str | Path | None = None, cache_dir: str | Path | None = None) -> pd.DataFrame:
    """Virginia NPDES facilities with valid coordinates: npdes_id, name, frs_id, latitude, longitude."""
    d, cache = _settings_paths(npdes_dir, cache_dir, "echo_npdes_dir")
    df = cached_frame(d / FACILITIES_FILE, cache, "echo_facilities_va", _build_facilities)
    return _restore_none(df, ["npdes_id", "name", "frs_id"])


# ---- outfalls --------------------------------------------------------------------------------
def _build_outfalls(src: Path) -> pd.DataFrame:
    raw = pd.read_csv(
        src,
        dtype=str,
        encoding="latin-1",
        usecols=["EXTERNAL_PERMIT_NMBR", "PERM_FEATURE_NMBR", "LATITUDE_MEASURE", "LONGITUDE_MEASURE"],
        keep_default_na=False,
    )
    raw = raw[raw["EXTERNAL_PERMIT_NMBR"].str.strip().str.startswith("VA")]
    raw = _coords(raw, "LATITUDE_MEASURE", "LONGITUDE_MEASURE")
    df = pd.DataFrame({
        "npdes_id": clean_str(raw["EXTERNAL_PERMIT_NMBR"]),
        "outfall": clean_str(raw["PERM_FEATURE_NMBR"]),
        "latitude": raw["latitude"],
        "longitude": raw["longitude"],
    })
    df = df.drop_duplicates(["npdes_id", "outfall"], keep="first")
    return df.reset_index(drop=True)[OUTFALL_COLUMNS]


def load_outfalls(npdes_dir: str | Path | None = None, cache_dir: str | Path | None = None) -> pd.DataFrame:
    """Virginia outfall (permitted feature) points: npdes_id, outfall, latitude, longitude."""
    d, cache = _settings_paths(npdes_dir, cache_dir, "echo_npdes_dir")
    df = cached_frame(d / OUTFALLS_FILE, cache, "echo_outfalls_va", _build_outfalls)
    return _restore_none(df, ["npdes_id", "outfall"])


# ---- violations ------------------------------------------------------------------------------
def _as_date(v: object) -> date | None:
    # Parquet may return date32 as datetime.date or as a timestamp, depending on versions.
    if v is None or (not isinstance(v, date) and pd.isna(v)):
        return None
    if isinstance(v, pd.Timestamp):
        return None if pd.isna(v) else v.date()
    if isinstance(v, datetime):
        return v.date()
    return v if isinstance(v, date) else None


def _violations_csv(effluent_dir: Path) -> Path:
    files = sorted(effluent_dir.glob("*.csv"))
    if len(files) != 1:
        raise FileNotFoundError(f"expected one CSV in {effluent_dir}, found {len(files)}")
    return files[0]


def _build_violations(src: Path) -> pd.DataFrame:
    raw = pd.read_csv(src, dtype=str, encoding="latin-1", usecols=list(_VIOLATION_SOURCE),
                      keep_default_na=False)
    df = pd.DataFrame({new: clean_str(raw[old]) for old, new in _VIOLATION_SOURCE.items()})
    dates = pd.to_datetime(df["period_end"], format="%m/%d/%Y", errors="coerce")
    df["period_end"] = pd.Series(
        [None if pd.isna(t) else t.date() for t in dates], index=df.index, dtype="object"
    )
    desc = df["violation_desc"].fillna("").astype(str)
    df["is_numeric"] = desc.str.contains("numeric violation", case=False, regex=False).astype(bool)
    df["is_reporting"] = desc.str.contains("overdue", case=False, regex=False).astype(bool)
    df["exceedance_pct"] = opt_float(df["exceedance_pct"])
    df["violation_desc"] = df["violation_desc"].map(lambda v: v if v is not None else "").astype("object")
    return df.reset_index(drop=True)[VIOLATION_COLUMNS]


def load_violations(effluent_dir: str | Path | None = None, cache_dir: str | Path | None = None) -> pd.DataFrame:
    """All rows of the effluent violations CSV with normalized columns (see module docstring)."""
    d, cache = _settings_paths(effluent_dir, cache_dir, "echo_effluent_dir")
    df = cached_frame(_violations_csv(d), cache, "echo_violations", _build_violations)
    df = _restore_none(
        df, ["npdes_id", "outfall", "parameter", "status", "dmr_value", "limit_value", "unit"]
    )
    df["violation_desc"] = df["violation_desc"].astype("object")
    df["exceedance_pct"] = opt_float(df["exceedance_pct"])
    df["period_end"] = pd.Series([_as_date(v) for v in df["period_end"]], index=df.index, dtype="object")
    df["is_numeric"] = df["is_numeric"].astype(bool)
    df["is_reporting"] = df["is_reporting"].astype(bool)
    return df
