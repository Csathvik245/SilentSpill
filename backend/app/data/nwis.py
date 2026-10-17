"""USGS gauge baseline: instantaneous values for one site, fetched once and replayed from a CSV cache.

Primary source is the USGS Water Data OGC API (`continuous` collection); the legacy NWIS IV service
is the fallback. Both are parsed to one long "raw" frame, one row per (series, timestamp):

    timestamp (UTC), parameter_code, parameter, value, unit, qualifiers, series_id, source

`qualifiers` is a comma-joined string: the approval code first ("P" provisional, "A" approved),
then the source's own qualifier codes verbatim (Water Data: "ESTIMATED", "ICE"; IV: "e", "Ice",
"Eqp"). Units are kept as each source spells them. Gaps stay missing; nothing is filled.
Some sites report several series per parameter (e.g. East/West sensors); all are kept in raw and
`primary_series` picks one deterministically for the wide frame.
"""
from __future__ import annotations

import logging
import os
import time
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests

log = logging.getLogger(__name__)

NWIS_PARAMETERS: dict[str, str] = {
    "00095": "conductivity",  # specific conductance, uS/cm at 25 C
    "63680": "turbidity",  # FNU
    "00400": "ph",
    "00060": "discharge",  # cfs
}
EXPECTED_INTERVAL = pd.Timedelta("15min")

WATERDATA_URL = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/continuous/items"
NWIS_IV_URL = "https://waterservices.usgs.gov/nwis/iv/"
API_KEY_ENV = "USGS_API_KEY"

RAW_COLUMNS = ["timestamp", "parameter_code", "parameter", "value", "unit", "qualifiers", "series_id", "source"]
_STR_COLUMNS = ["parameter_code", "parameter", "unit", "qualifiers", "series_id", "source"]
NO_DATA_VALUES = (-999999.0,)
_APPROVAL_CODES = {"provisional": "P", "approved": "A"}

PAGE_LIMIT = 10000
MAX_PAGES = 100
MAX_ATTEMPTS = 4
BACKOFF_SECONDS = 1.0
MAX_WAIT_SECONDS = 60.0
TIMEOUT = (10, 60)  # connect, read
_RETRY_STATUS = {429, 500, 502, 503, 504}
_sleep = time.sleep  # patched in tests


class NwisFetchError(RuntimeError):
    """Raised when a request still fails after all retries."""


# ---- parsing ---------------------------------------------------------------------------------
def _name(code: str) -> str:
    return NWIS_PARAMETERS.get(code, code)


def _qualifier_str(approval: Any, qualifiers: Any) -> str:
    parts: list[str] = []
    if approval:
        parts.append(_APPROVAL_CODES.get(str(approval).strip().lower(), str(approval).strip()))
    if isinstance(qualifiers, str):
        qualifiers = [qualifiers]
    parts += [str(q) for q in (qualifiers or []) if q is not None and str(q) != ""]
    return ",".join(parts)


def _to_float(v: Any, no_data: Iterable[float] = NO_DATA_VALUES) -> float:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return float("nan")
    return float("nan") if f in tuple(no_data) else f


def _frame(rows: list[dict]) -> pd.DataFrame:
    """Rows -> raw frame with fixed dtypes, exact duplicates dropped, sorted."""
    df = pd.DataFrame(rows, columns=RAW_COLUMNS)
    return _normalize(df)


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    df["timestamp"] = pd.to_datetime(df["timestamp"], utc=True).dt.as_unit("ns")
    df["value"] = pd.to_numeric(df["value"], errors="coerce").astype("float64")
    for c in _STR_COLUMNS:
        df[c] = df[c].map(lambda v: "" if v is None or (not isinstance(v, str) and pd.isna(v)) else str(v))
        df[c] = df[c].astype(str)
    df = df.drop_duplicates()
    df = df.sort_values(["parameter", "series_id", "timestamp"], kind="mergesort")
    return df.reset_index(drop=True)[RAW_COLUMNS]


def parse_waterdata(payloads: list[dict]) -> pd.DataFrame:
    """Water Data OGC API `continuous/items` pages (GeoJSON) -> raw frame."""
    rows = []
    for page in payloads:
        for feat in page.get("features") or []:
            p = feat.get("properties") or {}
            code = str(p.get("parameter_code") or "")
            rows.append({
                "timestamp": p.get("time"),
                "parameter_code": code,
                "parameter": _name(code),
                "value": _to_float(p.get("value")),
                "unit": p.get("unit_of_measure") or "",
                "qualifiers": _qualifier_str(p.get("approval_status"), p.get("qualifier")),
                "series_id": str(p.get("time_series_id") or ""),
                "source": "waterdata",
            })
    return _frame(rows)


def parse_nwis_iv(payload: dict) -> pd.DataFrame:
    """Legacy NWIS IV JSON -> raw frame. Each `values[]` block (one per method) is its own series."""
    rows = []
    for ts in (payload.get("value") or {}).get("timeSeries") or []:
        var = ts.get("variable") or {}
        code = str((var.get("variableCode") or [{}])[0].get("value") or "")
        unit = (var.get("unit") or {}).get("unitCode") or ""
        no_data = NO_DATA_VALUES + ((float(var["noDataValue"]),) if var.get("noDataValue") is not None else ())
        for block in ts.get("values") or []:
            method = (block.get("method") or [{}])[0]
            series_id = f"{ts.get('name', '')}:{method.get('methodID', '')}"
            for v in block.get("value") or []:
                quals = v.get("qualifiers") or []
                rows.append({
                    "timestamp": v.get("dateTime"),
                    "parameter_code": code,
                    "parameter": _name(code),
                    "value": _to_float(v.get("value"), no_data),
                    "unit": unit,
                    "qualifiers": ",".join(str(q) for q in quals),
                    "series_id": series_id,
                    "source": "nwis_iv",
                })
    return _frame(rows)


# ---- network ---------------------------------------------------------------------------------
def _retry_after(resp: Any) -> float | None:
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        return min(max(float(raw), 0.0), MAX_WAIT_SECONDS) if raw is not None else None
    except (TypeError, ValueError):
        return None  # HTTP-date form: fall back to exponential backoff


def _get_json(session: Any, url: str, params: dict | None, headers: dict | None = None) -> dict:
    """GET with retries on 429/5xx and connection errors; exponential backoff, honours Retry-After."""
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait = min(BACKOFF_SECONDS * 2 ** (attempt - 1), MAX_WAIT_SECONDS)
        try:
            resp = session.get(url, params=params, headers=headers or {}, timeout=TIMEOUT)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = type(exc).__name__
        else:
            if resp.status_code == 200:
                return resp.json()
            if resp.status_code not in _RETRY_STATUS:
                raise NwisFetchError(f"HTTP {resp.status_code} from {url.split('?')[0]}")
            last = f"HTTP {resp.status_code}"
            wait = _retry_after(resp) or wait
        if attempt < MAX_ATTEMPTS:
            log.warning("%s from %s; retry %d/%d in %.1fs", last, url.split("?")[0], attempt, MAX_ATTEMPTS - 1, wait)
            _sleep(wait)
    raise NwisFetchError(f"{last} from {url.split('?')[0]} after {MAX_ATTEMPTS} attempts")


def _iso(t: datetime) -> str:
    return _utc(t).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc(t: datetime) -> pd.Timestamp:
    ts = pd.Timestamp(t)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def _fetch_waterdata_pages(session: Any, site_id: str, start: datetime, end: datetime,
                           codes: list[str]) -> list[dict]:
    headers = {}
    key = os.environ.get(API_KEY_ENV)
    if key:
        headers["X-Api-Key"] = key  # never logged
    url: str | None = WATERDATA_URL
    params: dict | None = {
        "f": "json",
        "monitoring_location_id": f"USGS-{site_id}",
        "parameter_code": ",".join(codes),
        "datetime": f"{_iso(start)}/{_iso(end)}",
        "limit": PAGE_LIMIT,
        "skipGeometry": "true",
    }
    pages: list[dict] = []
    seen: set[str] = set()
    while url:
        if len(pages) >= MAX_PAGES or url in seen:
            raise NwisFetchError("pagination did not terminate")
        seen.add(url)
        page = _get_json(session, url, params, headers)
        pages.append(page)
        params = None  # the next link carries the full query
        url = next((l.get("href") for l in page.get("links") or [] if l.get("rel") == "next"), None)
    log.info("waterdata: %d page(s), %d features", len(pages), sum(len(p.get("features") or []) for p in pages))
    return pages


def _fetch_nwis_iv(session: Any, site_id: str, start: datetime, end: datetime, codes: list[str]) -> dict:
    params = {
        "sites": site_id,
        "parameterCd": ",".join(codes),
        "startDT": _utc(start).strftime("%Y-%m-%dT%H:%MZ"),
        "endDT": _utc(end).strftime("%Y-%m-%dT%H:%MZ"),
        "format": "json",
    }
    return _get_json(session, NWIS_IV_URL, params)


def fetch_nwis(site_id: str, start: datetime, end: datetime,
               parameter_codes: Iterable[str] = NWIS_PARAMETERS, session: Any = None) -> pd.DataFrame:
    """Live pull of [start, end] (UTC, inclusive) in raw format. Water Data API first, then legacy IV."""
    codes = list(parameter_codes)
    own = session is None
    session = session or requests.Session()
    try:
        try:
            raw = parse_waterdata(_fetch_waterdata_pages(session, site_id, start, end, codes))
            if raw.empty:
                raise NwisFetchError("waterdata returned no rows")
        except (NwisFetchError, ValueError) as exc:
            log.warning("waterdata failed (%s); falling back to NWIS IV", exc)
            raw = parse_nwis_iv(_fetch_nwis_iv(session, site_id, start, end, codes))
    finally:
        if own:
            session.close()
    in_window = raw["timestamp"].between(_utc(start), _utc(end)) & raw["parameter_code"].isin(codes)
    return raw[in_window].reset_index(drop=True)


# ---- cache -----------------------------------------------------------------------------------
def save_raw(df: pd.DataFrame, path: str | Path) -> Path:
    """Write the raw frame as CSV with ISO-8601 UTC timestamps. Missing values are empty cells."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = _normalize(df[RAW_COLUMNS])
    out["timestamp"] = out["timestamp"].dt.strftime("%Y-%m-%dT%H:%M:%SZ")
    tmp = path.with_suffix(path.suffix + ".tmp")
    out.to_csv(tmp, index=False)
    os.replace(tmp, path)
    return path


def load_raw(path: str | Path) -> pd.DataFrame:
    """Read a CSV written by save_raw back into the raw frame (tz-aware UTC timestamps)."""
    df = pd.read_csv(path, dtype={c: str for c in _STR_COLUMNS}, keep_default_na=False,
                     na_values={"value": [""]})
    return _normalize(df[RAW_COLUMNS])


# ---- analysis --------------------------------------------------------------------------------
def primary_series(raw: pd.DataFrame) -> dict[str, str]:
    """parameter -> series_id with the most non-null values; ties go to the smallest series_id."""
    if raw.empty:
        return {}
    counts = (raw.assign(_n=raw["value"].notna())
              .groupby(["parameter", "series_id"], sort=True)["_n"].sum().reset_index())
    counts = counts.sort_values(["parameter", "_n", "series_id"], ascending=[True, False, True], kind="mergesort")
    return {str(p): str(g["series_id"].iloc[0]) for p, g in counts.groupby("parameter", sort=True)}


def to_wide(raw: pd.DataFrame, start: datetime | None = None, end: datetime | None = None) -> pd.DataFrame:
    """Primary series on a regular 15-min UTC grid; missing stays NaN / "" (never filled).

    The chosen series are stored in `wide.attrs["primary_series"]`.
    """
    if start is not None:
        raw = raw[raw["timestamp"] >= _utc(start).floor(EXPECTED_INTERVAL)]
    if end is not None:
        raw = raw[raw["timestamp"] <= _utc(end)]
    lo = _utc(start) if start is not None else raw["timestamp"].min()
    hi = _utc(end) if end is not None else raw["timestamp"].max()
    if pd.isna(lo) or pd.isna(hi):
        grid = pd.DatetimeIndex([], tz="UTC", name="timestamp").as_unit("ns")
    else:
        grid = pd.date_range(lo.floor(EXPECTED_INTERVAL), hi, freq=EXPECTED_INTERVAL, name="timestamp").as_unit("ns")
    chosen = primary_series(raw)
    names = list(NWIS_PARAMETERS.values()) + sorted(set(raw["parameter"]) - set(NWIS_PARAMETERS.values()))
    wide = pd.DataFrame(index=grid)
    for name in names:
        s = raw[(raw["parameter"] == name) & (raw["series_id"] == chosen.get(name, None))]
        s = s.drop_duplicates("timestamp", keep="last").set_index("timestamp")
        wide[name] = s["value"].reindex(grid).astype("float64")
        wide[f"{name}_qualifiers"] = s["qualifiers"].reindex(grid).fillna("").astype(str)
    wide = wide[[c for n in names for c in (n, f"{n}_qualifiers")]]
    wide.attrs["primary_series"] = chosen
    return wide


def find_gaps(wide: pd.DataFrame, parameter: str) -> list[dict]:
    """Runs of missing grid values for one parameter."""
    missing = wide[parameter].isna().to_numpy()
    gaps: list[dict] = []
    i, n = 0, len(missing)
    while i < n:
        if missing[i]:
            j = i
            while j + 1 < n and missing[j + 1]:
                j += 1
            count = j - i + 1
            gaps.append({"start": wide.index[i], "end": wide.index[j], "missing_intervals": count,
                         "duration": count * EXPECTED_INTERVAL})
            i = j + 1
        else:
            i += 1
    return gaps


def summarize(raw: pd.DataFrame, wide: pd.DataFrame) -> dict:
    """Per-parameter counts for the check-in report (see module docstring for the column meanings)."""
    chosen = wide.attrs.get("primary_series") or primary_series(raw)
    out: dict[str, dict] = {}
    names = [c for c in wide.columns if not c.endswith("_qualifiers")]
    for name in names:
        sub = raw[raw["parameter"] == name]
        vals = wide[name].dropna()
        gaps = find_gaps(wide, name)
        quals = wide[f"{name}_qualifiers"]
        out[name] = {
            "parameter_code": next((c for c, n in NWIS_PARAMETERS.items() if n == name), name),
            "rows": int(vals.size),
            "series_found": int(sub["series_id"].nunique()),
            "series_ids": sorted(sub["series_id"].unique().tolist()),
            "chosen_series": chosen.get(name),
            "source": ",".join(sorted(sub["source"].unique().tolist())),
            "unit": ",".join(sorted(sub.loc[sub["series_id"] == chosen.get(name), "unit"].unique().tolist())),
            "first": vals.index.min() if vals.size else None,
            "last": vals.index.max() if vals.size else None,
            "expected_intervals": int(len(wide)),
            "gaps": len(gaps),
            "missing_intervals": int(sum(g["missing_intervals"] for g in gaps)),
            "largest_gaps": sorted(gaps, key=lambda g: (-g["missing_intervals"], g["start"]))[:5],
            "qualifiers": {str(k): int(v) for k, v in quals[quals != ""].value_counts().sort_index().items()},
        }
    return out


def default_window(today: datetime | None = None, lag_days: int = 3, days: int = 28) -> tuple[datetime, datetime]:
    """(start, end) in UTC: end is today 00:00 UTC minus lag_days, start is end minus days."""
    now = today or datetime.now(timezone.utc)
    end = _utc(now).floor("D") - pd.Timedelta(days=lag_days)
    return (end - pd.Timedelta(days=days)).to_pydatetime(), end.to_pydatetime()
