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

PERMITS_FILE = "ICIS_PERMITS.csv"

FACILITY_COLUMNS = ["npdes_id", "name", "frs_id", "latitude", "longitude"]
PERMIT_COLUMNS = [
    "npdes_id", "permit_type_code", "permit_status_code", "facility_type_indicator",
    "general_permit_id", "general_permit_name",
]
FACILITY_TYPE_COLUMNS = ["npdes_id", "facility_type_code"]

# Code tables from the ECHO ICIS-NPDES download summary (echo.epa.gov, checked 2026-10-18).
PERMIT_TYPE_DESC = {
    "NPD": "NPDES Individual Permit",
    "NGP": "NPDES Master General Permit",
    "GPC": "General Permit Covered Facility",
    "SNN": "State Issued Master General Permit (Non-NPDES)",
    "IIU": "Individual IU Permit (Non-NPDES)",
    "SIN": "Individual State Issued Permit (Non-NPDES)",
    "APR": "Associated Permit Record",
    "UFT": "Unpermitted Facility",
}
# Only NPDES individual and general permits map to a permit_type; other codes keep the code only.
PERMIT_TYPE_KIND = {"NPD": "individual", "GPC": "general", "NGP": "general"}
FACILITY_TYPE_DESC = {
    "CNG": "County Government",
    "COR": "Corporation",
    "CTG": "Municipality",
    "DIS": "District",
    "FDF": "Federal Facility (U.S. Government)",
    "GOC": "GOCO (Gov Owned/Contractor Operated)",
    "IND": "Individual",
    "MWD": "Municipal or Water District",
    "MXO": "Mixed Ownership (e.g., Public/Private)",
    "NON": "Non-Government",
    "POF": "Privately Owned Facility",
    "SDT": "School District",
    "STF": "State Government",
    "TRB": "Tribal Government",
    "UNK": "Unknown",
}
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


# ---- permits and facility types --------------------------------------------------------------
def _build_permits(src: Path) -> pd.DataFrame:
    raw = pd.read_csv(
        src,
        dtype=str,
        encoding="latin-1",
        usecols=["EXTERNAL_PERMIT_NMBR", "VERSION_NMBR", "FACILITY_TYPE_INDICATOR", "PERMIT_TYPE_CODE",
                 "PERMIT_STATUS_CODE", "PERMIT_NAME", "MASTER_EXTERNAL_PERMIT_NMBR"],
        keep_default_na=False,
    )
    raw = raw[raw["EXTERNAL_PERMIT_NMBR"].str.strip().str.startswith("VA")].copy()
    for c in raw.columns:
        raw[c] = raw[c].str.strip()
    # One row per permit: the effective version if there is one, otherwise the highest version.
    raw["_eff"] = (raw["PERMIT_STATUS_CODE"] == "EFF").astype(int)
    raw["_ver"] = pd.to_numeric(raw["VERSION_NMBR"], errors="coerce").fillna(-1)
    raw = raw.sort_values(["EXTERNAL_PERMIT_NMBR", "_eff", "_ver"], ascending=[True, False, False])
    current = raw.drop_duplicates("EXTERNAL_PERMIT_NMBR", keep="first")
    # Master general permits carry the general permit's title (e.g. VAG400000).
    masters = current[current["PERMIT_TYPE_CODE"] == "NGP"].set_index("EXTERNAL_PERMIT_NMBR")["PERMIT_NAME"]
    master_id = clean_str(current["MASTER_EXTERNAL_PERMIT_NMBR"])
    df = pd.DataFrame({
        "npdes_id": clean_str(current["EXTERNAL_PERMIT_NMBR"]),
        "permit_type_code": clean_str(current["PERMIT_TYPE_CODE"]),
        "permit_status_code": clean_str(current["PERMIT_STATUS_CODE"]),
        "facility_type_indicator": clean_str(current["FACILITY_TYPE_INDICATOR"]),
        "general_permit_id": master_id,
        "general_permit_name": clean_str(master_id.map(lambda m: masters.get(m) if m else None)),
    })
    return df.reset_index(drop=True)[PERMIT_COLUMNS]


def _build_facility_types(src: Path) -> pd.DataFrame:
    raw = pd.read_csv(src, dtype=str, encoding="latin-1",
                      usecols=["NPDES_ID", "FACILITY_TYPE_CODE", "STATE_CODE"], keep_default_na=False)
    raw = raw[raw["STATE_CODE"].str.strip() == "VA"]
    df = pd.DataFrame({
        "npdes_id": clean_str(raw["NPDES_ID"]),
        "facility_type_code": clean_str(raw["FACILITY_TYPE_CODE"]),
    })
    df = df[df["npdes_id"].notna()].drop_duplicates("npdes_id", keep="first")
    return df.reset_index(drop=True)[FACILITY_TYPE_COLUMNS]


def load_permits(npdes_dir: str | Path | None = None, cache_dir: str | Path | None = None) -> pd.DataFrame:
    """One row per Virginia permit (current version) with its permit type and the facility type.

    Columns: PERMIT_COLUMNS from ICIS_PERMITS plus facility_type_code from ICIS_FACILITIES.
    """
    d, cache = _settings_paths(npdes_dir, cache_dir, "echo_npdes_dir")
    permits = cached_frame(d / PERMITS_FILE, cache, "echo_permits_va", _build_permits)
    types = cached_frame(d / FACILITIES_FILE, cache, "echo_facility_types_va", _build_facility_types)
    df = permits.merge(types, on="npdes_id", how="left")
    return _restore_none(df, PERMIT_COLUMNS + ["facility_type_code"])


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
