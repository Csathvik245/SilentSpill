"""NOAA CDO (GHCND) daily precipitation for one station.

The CDO token is sent only in the `token` request header; it never appears in URLs, logs, errors
or saved files. Every calendar day in the requested range gets a row: days NOAA did not report
stay NaN (a gap), never 0. GHCN attribute flags (e.g. ",,W,2400") are kept verbatim.
Daily resolution only: see window_total for what that means for the storm filter.
"""
from __future__ import annotations

import math
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

import pandas as pd
import requests

BASE_URL = "https://www.ncei.noaa.gov/cdo-web/api/v2/data"
DATASET = "GHCND"
DATATYPE = "PRCP"
PAGE_LIMIT = 1000  # CDO maximum
MAX_SPAN_DAYS = 365  # CDO allows at most one year per request for daily data
MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 1.0
MAX_WAIT_S = 30.0
TIMEOUT_S = (10.0, 60.0)  # connect, read
TOKEN_ENV = "NOAA_CDO_TOKEN"

COLUMNS = ["date", "prcp_inches", "attributes", "station"]

_sleep = time.sleep  # patched in tests


class MissingTokenError(RuntimeError):
    """Raised when no CDO token is configured."""


class NoaaError(RuntimeError):
    """Raised when CDO keeps failing or returns something unusable. Never contains the token."""


# ---- network ---------------------------------------------------------------------------------
def _check_token(token: Any) -> str:
    if token is not None and hasattr(token, "get_secret_value"):
        token = token.get_secret_value()
    if not isinstance(token, str) or not token.strip():
        raise MissingTokenError(f"NOAA CDO token is missing: set {TOKEN_ENV} in .env")
    return token.strip()


def _retry_after(resp: Any) -> float | None:
    raw = (getattr(resp, "headers", None) or {}).get("Retry-After")
    try:
        return min(max(float(raw), 0.0), MAX_WAIT_S) if raw is not None else None
    except (TypeError, ValueError):
        return None  # HTTP-date form is not worth parsing here; fall back to backoff


def _get_json(session: Any, params: dict[str, Any], token: str) -> dict:
    """GET one CDO page with retries on 429, 5xx and connection errors. Empty body -> {}."""
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        wait = BACKOFF_BASE_S * 2 ** (attempt - 1)
        try:
            resp = session.get(BASE_URL, params=params, headers={"token": token}, timeout=TIMEOUT_S)
        except (requests.ConnectionError, requests.Timeout) as exc:
            last = f"{type(exc).__name__}"
        else:
            status = resp.status_code
            if status == 200:
                text = (resp.text or "").strip()
                if not text:
                    return {}
                try:
                    data = resp.json()
                except ValueError:
                    raise NoaaError("CDO returned a non-JSON body") from None
                if not isinstance(data, dict):
                    raise NoaaError("CDO returned unexpected JSON (not an object)")
                return data
            if status != 429 and status < 500:
                raise NoaaError(f"CDO request failed with HTTP {status}")
            last = f"HTTP {status}"
            ra = _retry_after(resp)
            if ra is not None:
                wait = ra
        if attempt < MAX_ATTEMPTS:
            _sleep(min(wait, MAX_WAIT_S))
    raise NoaaError(f"CDO request failed after {MAX_ATTEMPTS} attempts (last: {last})")


def _fetch_span(session: Any, station_id: str, start: date, end: date, token: str) -> list[dict]:
    """All pages for one span of at most a year, following metadata.resultset.count."""
    payloads: list[dict] = []
    offset = 1  # CDO offsets are 1-based
    while True:
        params = {
            "datasetid": DATASET, "datatypeid": DATATYPE, "stationid": station_id,
            "startdate": start.isoformat(), "enddate": end.isoformat(),
            "units": "standard", "limit": PAGE_LIMIT, "offset": offset,
        }
        payload = _get_json(session, params, token)
        payloads.append(payload)
        rs = (payload.get("metadata") or {}).get("resultset") or {}
        count = int(rs.get("count") or 0)
        limit = max(int(rs.get("limit") or PAGE_LIMIT), 1)
        if not payload.get("results") or offset + limit > count:
            return payloads
        offset += limit


def fetch_daily_prcp(
    station_id: str, start: date, end: date, token: str, session: Any = None
) -> pd.DataFrame:
    """Live CDO pull: parse_cdo output for every day start..end inclusive (gaps stay NaN)."""
    token = _check_token(token)
    if end < start:
        raise ValueError("end is before start")
    own = session is None
    session = session or requests.Session()
    try:
        payloads: list[dict] = []
        s = start
        while s <= end:
            e = min(end, s + timedelta(days=MAX_SPAN_DAYS - 1))
            payloads.extend(_fetch_span(session, station_id, s, e, token))
            s = e + timedelta(days=1)
    finally:
        if own:
            session.close()
    return parse_cdo(payloads, start, end, station=station_id)


# ---- parsing and cache -----------------------------------------------------------------------
def _all_days(start: date, end: date) -> list[date]:
    return [start + timedelta(days=i) for i in range((end - start).days + 1)]


def parse_cdo(payloads: list[dict], start: date, end: date, *, station: str = "") -> pd.DataFrame:
    """CDO bodies -> one row per calendar day start..end, sorted. No record -> NaN and "".

    Only PRCP records are used; records outside the range are dropped; a repeated date keeps the
    first record. `station` labels the gap rows (defaults to the station seen in the records).
    """
    rows: dict[date, tuple[float, str, str]] = {}
    for payload in payloads:
        for rec in (payload or {}).get("results") or []:
            if rec.get("datatype", DATATYPE) != DATATYPE or not rec.get("date"):
                continue
            day = date.fromisoformat(str(rec["date"])[:10])
            if not start <= day <= end or day in rows:
                continue
            value = rec.get("value")
            prcp = float(value) if value is not None else math.nan
            attrs = rec.get("attributes")
            rows[day] = (prcp, attrs if isinstance(attrs, str) else "", str(rec.get("station") or ""))
    if not station:
        seen = {r[2] for r in rows.values() if r[2]}
        station = seen.pop() if len(seen) == 1 else ""
    out = []
    for day in _all_days(start, end):
        prcp, attrs, st = rows.get(day, (math.nan, "", station))
        out.append((day, prcp, attrs, st or station))
    return _normalize(pd.DataFrame(out, columns=COLUMNS))


def _normalize(df: pd.DataFrame) -> pd.DataFrame:
    """Fixed dtypes: date objects, float64 prcp, object text columns."""
    df = df[COLUMNS].copy()
    df["date"] = pd.Series(list(df["date"]), index=df.index, dtype="object")
    df["prcp_inches"] = df["prcp_inches"].astype("float64")
    for col in ("attributes", "station"):
        df[col] = df[col].astype("object")
    return df


def save_daily(df: pd.DataFrame, path: str | Path) -> Path:
    """Write the daily frame as CSV. NaN is written as an empty cell, never 0."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    out = df[COLUMNS].copy()
    out["date"] = [d.isoformat() for d in out["date"]]
    tmp = path.with_suffix(path.suffix + ".tmp")
    out.to_csv(tmp, index=False, na_rep="")
    tmp.replace(path)
    return path


def load_daily(path: str | Path) -> pd.DataFrame:
    """Read a save_daily CSV back with the same dtypes; empty prcp cells come back NaN."""
    df = pd.read_csv(
        path,
        dtype={"date": str, "prcp_inches": "float64", "attributes": str, "station": str},
        keep_default_na=False,
        na_values={"prcp_inches": [""]},
    )
    df["date"] = [date.fromisoformat(d) for d in df["date"]]
    return _normalize(df)


# ---- analysis --------------------------------------------------------------------------------
def window_total(
    daily: pd.DataFrame, end: datetime | date, window_hours: int
) -> tuple[float | None, list[date], int]:
    """Rain total for the storm filter, at DAILY resolution.

    Days used: the calendar day of `end` plus ceil(window_hours / 24) earlier days, so 24 h means
    the day of `end` and the day before. Limitation: GHCND gives daily totals only, so this
    over-covers the window (up to 24 h extra) and rain timing within a day is unknown.
    Returns (sum of available days or None if all are missing, days used, number missing).
    Days absent from `daily` count as missing.
    """
    if window_hours <= 0:
        raise ValueError("window_hours must be greater than 0")
    end_day = end.date() if isinstance(end, datetime) else end
    n = math.ceil(window_hours / 24) + 1
    days = [end_day - timedelta(days=k) for k in range(n - 1, -1, -1)]
    lookup = dict(zip(daily["date"], daily["prcp_inches"]))
    values = [lookup.get(d) for d in days]
    present = [float(v) for v in values if v is not None and not pd.isna(v)]
    missing = len(days) - len(present)
    return (float(sum(present)) if present else None), days, missing


def summarize(daily: pd.DataFrame) -> dict[str, Any]:
    """Date range, day counts, missing dates, total and wettest day (None when no readings)."""
    have = daily[daily["prcp_inches"].notna()]
    max_day = None
    if len(have):
        row = have.loc[have["prcp_inches"].idxmax()]
        max_day = {"date": row["date"], "prcp_inches": float(row["prcp_inches"])}
    return {
        "start": min(daily["date"]) if len(daily) else None,
        "end": max(daily["date"]) if len(daily) else None,
        "days": int(len(daily)),
        "days_with_readings": int(len(have)),
        "missing_days": [d for d, v in zip(daily["date"], daily["prcp_inches"]) if pd.isna(v)],
        "total_inches": float(have["prcp_inches"].sum()) if len(have) else None,
        "max_day": max_day,
    }
