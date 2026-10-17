"""Tests for backend.app.data.noaa. No live network: real CDO bodies are in tests/fixtures/noaa.

The fixtures are response bodies only, saved from GHCND:USW00013731, 2026-09-16..2026-10-14,
units=standard: one full page (limit 1000), the same query paged at limit 5 (offsets 1, 6, 11),
and a 2026-10-15..2026-10-16 query for which CDO returned the empty body `{}`.
"""
from __future__ import annotations

import json
import math
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd
import pytest
import requests

from backend.app.data import noaa

FIXTURES = Path(__file__).parent / "fixtures" / "noaa"
STATION = "GHCND:USW00013731"
START, END = date(2026, 9, 16), date(2026, 10, 14)
FAKE_TOKEN = "fake-token-abc123"


def _load(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


# ---- fake HTTP -------------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int = 200, body: str = "{}", headers: dict | None = None):
        self.status_code = status
        self.text = body
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


class FakeSession:
    """Replays responses in order and records every call. Responses may be exceptions."""

    def __init__(self, responses: list):
        self.responses = list(responses)
        self.calls: list[dict] = []

    def get(self, url, params=None, headers=None, timeout=None, **kw):
        self.calls.append({"url": url, "params": dict(params or {}), "headers": dict(headers or {}),
                           "timeout": timeout, "kw": kw})
        item = self.responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def sleeps(monkeypatch):
    waits: list[float] = []
    monkeypatch.setattr(noaa, "_sleep", waits.append)
    return waits


def _body(name: str) -> FakeResponse:
    return FakeResponse(200, (FIXTURES / name).read_text(encoding="utf-8"))


# ---- parse_cdo on real samples ---------------------------------------------------------------
def test_parse_real_sample():
    df = noaa.parse_cdo([_load("prcp_20260916_20261014.json")], START, END)
    assert list(df.columns) == ["date", "prcp_inches", "attributes", "station"]
    assert len(df) == (END - START).days + 1 == 29
    assert df["date"].tolist() == [START + timedelta(days=i) for i in range(29)]
    assert all(type(d) is date for d in df["date"])
    assert df["prcp_inches"].dtype == "float64"
    assert (df["station"] == STATION).all()
    by_day = df.set_index("date")
    assert by_day.loc[date(2026, 9, 22), "prcp_inches"] == pytest.approx(0.95)
    assert by_day.loc[date(2026, 9, 17), "attributes"] == "H,,2,"
    # 11 real readings; every other day is a gap, not zero
    assert df["prcp_inches"].notna().sum() == 11
    assert math.isnan(by_day.loc[date(2026, 9, 16), "prcp_inches"])
    assert by_day.loc[date(2026, 9, 16), "attributes"] == ""
    assert (df["prcp_inches"].dropna() > 0).all()


def test_parse_real_pages_match_single_page():
    pages = [_load(f"prcp_page_limit5_offset{o}.json") for o in (1, 6, 11)]
    paged = noaa.parse_cdo(pages, START, END)
    whole = noaa.parse_cdo([_load("prcp_20260916_20261014.json")], START, END)
    pd.testing.assert_frame_equal(paged, whole)


def test_parse_empty_body_gives_all_gaps():
    body = _load("prcp_empty_20261015_20261016.json")
    assert body == {}
    df = noaa.parse_cdo([body], date(2026, 10, 15), date(2026, 10, 16), station=STATION)
    assert df["date"].tolist() == [date(2026, 10, 15), date(2026, 10, 16)]
    assert df["prcp_inches"].isna().all()
    assert (df["prcp_inches"] != 0).all()
    assert df["attributes"].tolist() == ["", ""]
    assert (df["station"] == STATION).all()


def test_parse_flags_verbatim_and_edge_records():
    payload = {"results": [
        {"date": "2026-01-02T00:00:00", "datatype": "PRCP", "station": STATION,
         "attributes": ",,W,2400", "value": 0.0},
        {"date": "2026-01-03T00:00:00", "datatype": "PRCP", "station": STATION,
         "attributes": "T,,7,0700", "value": 0.0},
        {"date": "2026-01-03T00:00:00", "datatype": "TMAX", "station": STATION, "value": 40},
        {"date": "2025-12-31T00:00:00", "datatype": "PRCP", "station": STATION, "value": 9.0},
    ]}
    df = noaa.parse_cdo([payload], date(2026, 1, 1), date(2026, 1, 3)).set_index("date")
    assert df.loc[date(2026, 1, 2), "attributes"] == ",,W,2400"
    assert df.loc[date(2026, 1, 3), "attributes"] == "T,,7,0700"
    # a reported 0 stays 0; an unreported day stays NaN
    assert df.loc[date(2026, 1, 2), "prcp_inches"] == 0.0
    assert math.isnan(df.loc[date(2026, 1, 1), "prcp_inches"])
    assert len(df) == 3


# ---- save / load -----------------------------------------------------------------------------
def test_save_load_roundtrip_keeps_nan(tmp_path):
    df = noaa.parse_cdo([_load("prcp_20260916_20261014.json")], START, END)
    path = noaa.save_daily(df, tmp_path / "noaa" / "x.csv")
    assert path.exists()
    back = noaa.load_daily(path)
    pd.testing.assert_frame_equal(back, df)
    assert back["prcp_inches"].isna().sum() == 18
    assert not (back["prcp_inches"] == 0).any()
    assert all(type(d) is date for d in back["date"])
    assert back.set_index("date").loc[date(2026, 9, 17), "attributes"] == "H,,2,"


def test_save_load_flag_strings_with_commas(tmp_path):
    df = pd.DataFrame({
        "date": [date(2026, 1, 1), date(2026, 1, 2)],
        "prcp_inches": [0.0, float("nan")],
        "attributes": [",,W,2400", ""],
        "station": [STATION, STATION],
    }).astype({"attributes": "object", "station": "object"})
    back = noaa.load_daily(noaa.save_daily(df, tmp_path / "f.csv"))
    pd.testing.assert_frame_equal(back, df)
    assert back["prcp_inches"].iloc[0] == 0.0 and math.isnan(back["prcp_inches"].iloc[1])


# ---- fetch: headers, pagination, empty body, retries -----------------------------------------
def test_token_only_in_header(sleeps):
    session = FakeSession([_body("prcp_20260916_20261014.json")])
    df = noaa.fetch_daily_prcp(STATION, START, END, FAKE_TOKEN, session=session)
    assert len(df) == 29 and df["prcp_inches"].notna().sum() == 11
    (call,) = session.calls
    assert call["headers"] == {"token": FAKE_TOKEN}
    assert FAKE_TOKEN not in call["url"]
    assert FAKE_TOKEN not in json.dumps(call["params"])
    assert "token" not in call["params"]
    assert call["timeout"] is not None
    p = call["params"]
    assert p["datasetid"] == "GHCND" and p["datatypeid"] == "PRCP" and p["stationid"] == STATION
    assert p["startdate"] == "2026-09-16" and p["enddate"] == "2026-10-14"
    assert p["units"] == "standard" and p["limit"] == 1000 and p["offset"] == 1
    assert sleeps == []


def test_pagination_follows_offset(sleeps, monkeypatch):
    monkeypatch.setattr(noaa, "PAGE_LIMIT", 5)
    session = FakeSession([_body(f"prcp_page_limit5_offset{o}.json") for o in (1, 6, 11)])
    df = noaa.fetch_daily_prcp(STATION, START, END, FAKE_TOKEN, session=session)
    assert [c["params"]["offset"] for c in session.calls] == [1, 6, 11]
    assert all(c["params"]["limit"] == 5 for c in session.calls)
    assert df["prcp_inches"].notna().sum() == 11
    assert session.responses == []


def test_empty_body_variants(sleeps):
    for body in ("{}", ""):
        session = FakeSession([FakeResponse(200, body)])
        df = noaa.fetch_daily_prcp(STATION, date(2026, 10, 15), date(2026, 10, 16), FAKE_TOKEN,
                                   session=session)
        assert len(session.calls) == 1
        assert df["prcp_inches"].isna().all() and len(df) == 2
        assert (df["station"] == STATION).all()


def test_retry_429_then_503_then_success(sleeps):
    session = FakeSession([
        FakeResponse(429, "", {"Retry-After": "7"}),
        FakeResponse(503, "Service Unavailable"),
        _body("prcp_20260916_20261014.json"),
    ])
    df = noaa.fetch_daily_prcp(STATION, START, END, FAKE_TOKEN, session=session)
    assert len(session.calls) == 3
    assert sleeps == [7.0, 2.0]  # Retry-After honoured, then exponential backoff (attempt 2)
    assert df["prcp_inches"].notna().sum() == 11


def test_retry_connection_error_then_success(sleeps):
    session = FakeSession([requests.ConnectionError("boom"), _body("prcp_20260916_20261014.json")])
    df = noaa.fetch_daily_prcp(STATION, START, END, FAKE_TOKEN, session=session)
    assert len(session.calls) == 2 and sleeps == [1.0]
    assert len(df) == 29


def test_retry_gives_up_without_leaking_token(sleeps):
    session = FakeSession([FakeResponse(500, "err")] * noaa.MAX_ATTEMPTS)
    with pytest.raises(noaa.NoaaError) as exc:
        noaa.fetch_daily_prcp(STATION, START, END, FAKE_TOKEN, session=session)
    assert len(session.calls) == noaa.MAX_ATTEMPTS
    assert len(sleeps) == noaa.MAX_ATTEMPTS - 1
    assert FAKE_TOKEN not in str(exc.value)


def test_client_error_not_retried(sleeps):
    session = FakeSession([FakeResponse(400, "bad")])
    with pytest.raises(noaa.NoaaError) as exc:
        noaa.fetch_daily_prcp(STATION, START, END, FAKE_TOKEN, session=session)
    assert len(session.calls) == 1 and sleeps == []
    assert "400" in str(exc.value) and FAKE_TOKEN not in str(exc.value)


@pytest.mark.parametrize("token", [None, "", "   "])
def test_missing_token_names_env_var(token):
    session = FakeSession([])
    with pytest.raises(noaa.MissingTokenError) as exc:
        noaa.fetch_daily_prcp(STATION, START, END, token, session=session)
    assert "NOAA_CDO_TOKEN" in str(exc.value)
    assert session.calls == []


def test_long_range_split_into_year_spans(sleeps):
    start, end = date(2024, 1, 1), date(2025, 6, 30)
    session = FakeSession([FakeResponse(200, "{}"), FakeResponse(200, "{}")])
    df = noaa.fetch_daily_prcp(STATION, start, end, FAKE_TOKEN, session=session)
    spans = [(c["params"]["startdate"], c["params"]["enddate"]) for c in session.calls]
    assert spans == [("2024-01-01", "2024-12-30"), ("2024-12-31", "2025-06-30")]
    assert len(df) == (end - start).days + 1


# ---- window_total / summarize ----------------------------------------------------------------
def _daily(values: dict[date, float]) -> pd.DataFrame:
    days = sorted(values)
    return pd.DataFrame({"date": days, "prcp_inches": [values[d] for d in days],
                         "attributes": [""] * len(days), "station": [STATION] * len(days)})


def test_window_total_day_selection_and_sum():
    nan = float("nan")
    daily = _daily({date(2026, 9, 20): 0.1, date(2026, 9, 21): 0.28, date(2026, 9, 22): 0.95,
                    date(2026, 9, 23): nan})
    total, days, missing = noaa.window_total(daily, datetime(2026, 9, 22, 15, 30), 24)
    assert days == [date(2026, 9, 21), date(2026, 9, 22)]
    assert total == pytest.approx(1.23) and missing == 0
    total, days, missing = noaa.window_total(daily, date(2026, 9, 22), 48)
    assert days == [date(2026, 9, 20), date(2026, 9, 21), date(2026, 9, 22)]
    assert total == pytest.approx(1.33)
    _, days, _ = noaa.window_total(daily, date(2026, 9, 22), 25)
    assert len(days) == 3
    _, days, _ = noaa.window_total(daily, date(2026, 9, 22), 6)
    assert days == [date(2026, 9, 21), date(2026, 9, 22)]


def test_window_total_missing_and_all_missing():
    nan = float("nan")
    daily = _daily({date(2026, 9, 22): 0.95, date(2026, 9, 23): nan, date(2026, 9, 24): nan})
    total, days, missing = noaa.window_total(daily, date(2026, 9, 23), 24)
    assert total == pytest.approx(0.95) and missing == 1
    total, days, missing = noaa.window_total(daily, date(2026, 9, 24), 24)
    assert total is None and missing == 2
    # days outside the frame count as missing, never as zero
    total, days, missing = noaa.window_total(daily, date(2026, 10, 30), 24)
    assert total is None and missing == 2
    with pytest.raises(ValueError):
        noaa.window_total(daily, date(2026, 9, 24), 0)


def test_window_total_reported_zero_is_not_missing():
    daily = _daily({date(2026, 9, 1): 0.0, date(2026, 9, 2): 0.0})
    assert noaa.window_total(daily, date(2026, 9, 2), 24) == (0.0, [date(2026, 9, 1), date(2026, 9, 2)], 0)


def test_summarize_real_sample():
    df = noaa.parse_cdo([_load("prcp_20260916_20261014.json")], START, END)
    s = noaa.summarize(df)
    assert s["start"] == START and s["end"] == END and s["days"] == 29
    assert s["days_with_readings"] == 11
    assert len(s["missing_days"]) == 18 and date(2026, 10, 14) in s["missing_days"]
    assert s["total_inches"] == pytest.approx(2.25)
    assert s["max_day"] == {"date": date(2026, 9, 22), "prcp_inches": pytest.approx(0.95)}


def test_summarize_all_missing():
    df = noaa.parse_cdo([{}], START, START + timedelta(days=2), station=STATION)
    s = noaa.summarize(df)
    assert s["days_with_readings"] == 0 and s["total_inches"] is None and s["max_day"] is None
    assert len(s["missing_days"]) == 3
