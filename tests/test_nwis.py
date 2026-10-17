"""Tests for backend.app.data.nwis against saved real responses in tests/fixtures/nwis.

Fixtures (fetched 2026-10-17):
- waterdata_01634000_p1/p2: OGC API, site 01634000, 4 parameters, 2026-09-16 12:00Z..2026-09-17 00:00Z,
  limit=100 so it spans two pages (184 features). conductivity/turbidity/ph miss 16:45..17:30 (4 slots).
- nwis_iv_01634000: legacy IV, same site and window.
- *_01576003_dup: site 01576003 reports East/West sensors for 00095 and 63680 (IV adds an empty
  "Center" method block), 2026-10-10 00:00Z..06:00Z.
- *_01634000_ice: discharge 2025-01-06 00:00Z..12:00Z, approved, with ESTIMATED/ICE (OGC) and e (IV).
No test touches the network: fetch_nwis is driven by a fake session.
"""
from __future__ import annotations

import copy
import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
import requests

from backend.app.data import nwis

FIX = Path(__file__).parent / "fixtures" / "nwis"
START = datetime(2026, 9, 16, 12, tzinfo=timezone.utc)
END = datetime(2026, 9, 17, tzinfo=timezone.utc)


def _load(name: str) -> dict:
    return json.loads((FIX / name).read_text())


@pytest.fixture(scope="module")
def wd_pages() -> list[dict]:
    return [_load("waterdata_01634000_p1.json"), _load("waterdata_01634000_p2.json")]


@pytest.fixture(scope="module")
def wd_raw(wd_pages) -> pd.DataFrame:
    return nwis.parse_waterdata(wd_pages)


@pytest.fixture(scope="module")
def iv_raw() -> pd.DataFrame:
    return nwis.parse_nwis_iv(_load("nwis_iv_01634000.json"))


# ---- parsing ---------------------------------------------------------------------------------
def test_parameter_mapping_constant():
    assert nwis.NWIS_PARAMETERS == {
        "00095": "conductivity", "63680": "turbidity", "00400": "ph", "00060": "discharge",
    }
    assert nwis.EXPECTED_INTERVAL == pd.Timedelta("15min")


@pytest.mark.parametrize("which", ["wd_raw", "iv_raw"])
def test_raw_format(which, request):
    raw = request.getfixturevalue(which)
    assert list(raw.columns) == nwis.RAW_COLUMNS
    assert str(raw["timestamp"].dt.tz) == "UTC"
    assert raw["value"].dtype == "float64"
    for c in ["parameter_code", "parameter", "unit", "qualifiers", "series_id", "source"]:
        assert all(isinstance(v, str) for v in raw[c])
    assert raw.groupby("parameter").size().to_dict() == {
        "conductivity": 45, "discharge": 49, "ph": 45, "turbidity": 45,
    }
    assert set(zip(raw["parameter_code"], raw["parameter"])) == {
        (c, n) for c, n in nwis.NWIS_PARAMETERS.items()
    }
    sort_cols = ["parameter", "series_id", "timestamp"]
    assert raw.equals(raw.sort_values(sort_cols, kind="mergesort").reset_index(drop=True))
    assert raw["timestamp"].min() == pd.Timestamp(START) and raw["timestamp"].max() == pd.Timestamp(END)


def test_sources_agree_on_values(wd_raw, iv_raw):
    assert set(wd_raw["source"]) == {"waterdata"} and set(iv_raw["source"]) == {"nwis_iv"}
    key = ["parameter", "timestamp"]
    m = wd_raw[key + ["value"]].merge(iv_raw[key + ["value"]], on=key, suffixes=("_wd", "_iv"))
    assert len(m) == len(wd_raw) == len(iv_raw)
    assert (m["value_wd"] == m["value_iv"]).all()
    # IV dateTimes come with a -04:00 offset; they must be converted, not relabelled
    assert pd.Timestamp("2026-09-16T12:00Z") in set(iv_raw["timestamp"])


def test_units_kept_per_source(wd_raw, iv_raw):
    wd_units = wd_raw.groupby("parameter")["unit"].first().to_dict()
    iv_units = iv_raw.groupby("parameter")["unit"].first().to_dict()
    assert wd_units == {"conductivity": "uS/cm", "discharge": "ft^3/s", "ph": "pH Units", "turbidity": "_FNU"}
    assert iv_units == {"conductivity": "uS/cm @25C", "discharge": "ft3/s", "ph": "std units", "turbidity": "FNU"}


def test_qualifiers_verbatim():
    wd = nwis.parse_waterdata([_load("waterdata_01634000_ice_p1.json")])
    iv = nwis.parse_nwis_iv(_load("nwis_iv_01634000_ice.json"))
    assert wd["qualifiers"].value_counts().to_dict() == {"A": 22, "A,ESTIMATED,ICE": 7}
    assert iv["qualifiers"].value_counts().to_dict() == {"A": 18, "A,e": 8}


def test_qualifier_string_rules():
    assert nwis._qualifier_str("Provisional", None) == "P"
    assert nwis._qualifier_str("Approved", ["ICE", "Eqp"]) == "A,ICE,Eqp"
    assert nwis._qualifier_str("Revised", "e") == "Revised,e"
    assert nwis._qualifier_str(None, []) == ""


def test_nodata_and_null_values_become_nan():
    iv = _load("nwis_iv_01634000.json")
    iv = copy.deepcopy(iv)
    ts = next(t for t in iv["value"]["timeSeries"] if t["variable"]["variableCode"][0]["value"] == "63680")
    point = ts["values"][0]["value"][0]
    point["value"], point["qualifiers"] = "-999999", ["P", "Eqp"]
    raw = nwis.parse_nwis_iv(iv)
    row = raw[(raw["parameter"] == "turbidity") & (raw["timestamp"] == pd.Timestamp(START))].iloc[0]
    assert pd.isna(row["value"]) and row["qualifiers"] == "P,Eqp"
    assert raw["value"].isna().sum() == 1

    page = copy.deepcopy(_load("waterdata_01634000_p1.json"))
    page["features"][0]["properties"]["value"] = None
    page["features"][1]["properties"]["value"] = "-999999"
    page["features"][1]["properties"]["qualifier"] = ["EQUIPMENT"]
    raw = nwis.parse_waterdata([page])
    assert raw["value"].isna().sum() == 2
    assert "P,EQUIPMENT" in set(raw["qualifiers"])


def test_exact_duplicate_rows_dropped(wd_pages):
    raw = nwis.parse_waterdata(wd_pages + [wd_pages[0]])
    assert len(raw) == 184


def test_empty_payloads():
    assert list(nwis.parse_waterdata([]).columns) == nwis.RAW_COLUMNS
    assert nwis.parse_nwis_iv({"value": {"timeSeries": []}}).empty


# ---- duplicate series ------------------------------------------------------------------------
@pytest.mark.parametrize("parse", [
    lambda: nwis.parse_waterdata([_load("waterdata_01576003_dup_p1.json")]),
    lambda: nwis.parse_nwis_iv(_load("nwis_iv_01576003_dup.json")),
])
def test_duplicate_series_kept_and_primary_deterministic(parse):
    raw = parse()
    per = raw.groupby("parameter")["series_id"].nunique().to_dict()
    assert per == {"conductivity": 2, "turbidity": 2}
    assert len(raw) == 100
    # equal counts -> tie broken by smallest series_id, regardless of row order
    expected = {p: min(g["series_id"]) for p, g in raw.groupby("parameter")}
    assert nwis.primary_series(raw) == expected
    shuffled = raw.sample(frac=1, random_state=1).reset_index(drop=True)
    assert nwis.primary_series(shuffled) == expected
    # knock out values in the tie-winner: the other series now has more non-null values
    cond = raw["parameter"] == "conductivity"
    first = expected["conductivity"]
    other = sorted(set(raw.loc[cond, "series_id"]) - {first})[0]
    holed = raw.copy()
    holed.loc[(holed["series_id"] == first) & cond, "value"] = float("nan")
    assert nwis.primary_series(holed)["conductivity"] == other
    wide = nwis.to_wide(holed)
    assert wide.attrs["primary_series"]["conductivity"] == other
    assert wide["conductivity"].notna().all()


def test_iv_series_id_includes_method():
    raw = nwis.parse_nwis_iv(_load("nwis_iv_01576003_dup.json"))
    assert set(raw.loc[raw["parameter"] == "conductivity", "series_id"]) == {
        "USGS:01576003:00095:00000:316278", "USGS:01576003:00095:00000:316279",
    }


# ---- wide grid and gaps ----------------------------------------------------------------------
def test_to_wide_grid_and_gaps_not_filled(wd_raw):
    wide = nwis.to_wide(wd_raw)
    assert wide.index.tz is not None and str(wide.index.tz) == "UTC"
    assert len(wide) == 49 and (wide.index.to_series().diff().dropna() == nwis.EXPECTED_INTERVAL).all()
    assert wide.index[0] == pd.Timestamp(START) and wide.index[-1] == pd.Timestamp(END)
    assert list(wide.columns) == [
        "conductivity", "conductivity_qualifiers", "turbidity", "turbidity_qualifiers",
        "ph", "ph_qualifiers", "discharge", "discharge_qualifiers",
    ]
    hole = pd.date_range("2026-09-16T16:45Z", "2026-09-16T17:30Z", freq="15min")
    for p in ["conductivity", "turbidity", "ph"]:
        assert wide.loc[hole, p].isna().all()
        assert (wide.loc[hole, f"{p}_qualifiers"] == "").all()
        assert wide[p].isna().sum() == 4
    assert wide["discharge"].notna().all()
    assert (wide["discharge_qualifiers"] == "P").all()
    # values on the grid equal the raw values exactly (no interpolation)
    s = wd_raw[wd_raw["parameter"] == "ph"].set_index("timestamp")["value"]
    assert (wide.loc[s.index, "ph"] == s).all()


def test_to_wide_explicit_window(wd_raw):
    wide = nwis.to_wide(wd_raw, start=datetime(2026, 9, 16, 11, 7, tzinfo=timezone.utc),
                        end=datetime(2026, 9, 17, 1, tzinfo=timezone.utc))
    assert wide.index[0] == pd.Timestamp("2026-09-16T11:00Z")
    assert wide.index[-1] == pd.Timestamp("2026-09-17T01:00Z")
    assert len(wide) == 57
    assert wide["discharge"].isna().sum() == 8  # 4 before and 4 after the data


def test_to_wide_missing_parameter_column(wd_raw):
    wide = nwis.to_wide(wd_raw[wd_raw["parameter"] != "turbidity"])
    assert wide["turbidity"].isna().all() and (wide["turbidity_qualifiers"] == "").all()
    assert "turbidity" not in wide.attrs["primary_series"]


def test_find_gaps(wd_raw):
    wide = nwis.to_wide(wd_raw)
    gaps = nwis.find_gaps(wide, "turbidity")
    assert gaps == [{
        "start": pd.Timestamp("2026-09-16T16:45Z"), "end": pd.Timestamp("2026-09-16T17:30Z"),
        "missing_intervals": 4, "duration": pd.Timedelta("1h"),
    }]
    assert nwis.find_gaps(wide, "discharge") == []
    w2 = wide.copy()
    w2.iloc[[0, 1, -1], w2.columns.get_loc("discharge")] = float("nan")
    gaps = nwis.find_gaps(w2, "discharge")
    assert [(g["start"], g["missing_intervals"]) for g in gaps] == [
        (pd.Timestamp(START), 2), (pd.Timestamp(END), 1),
    ]


# ---- cache round-trip ------------------------------------------------------------------------
def test_save_load_roundtrip(tmp_path, wd_raw):
    raw = pd.concat([wd_raw, nwis.parse_waterdata([_load("waterdata_01634000_ice_p1.json")])])
    raw.loc[raw.index[3], "value"] = float("nan")
    raw = nwis._normalize(raw)
    raw.loc[5, "qualifiers"] = ""
    path = nwis.save_raw(raw, tmp_path / "sub" / "raw.csv")
    text = path.read_text()
    assert "2026-09-16T12:00:00Z" in text and "A,ESTIMATED,ICE" in text
    back = nwis.load_raw(path)
    pd.testing.assert_frame_equal(back, raw)
    assert back.loc[5, "qualifiers"] == "" and pd.isna(back.loc[3, "value"])
    assert back["parameter_code"].iloc[0].startswith("0")  # leading zeros survive


# ---- summary ---------------------------------------------------------------------------------
def test_summarize(wd_raw):
    wide = nwis.to_wide(wd_raw)
    s = nwis.summarize(wd_raw, wide)
    assert set(s) == {"conductivity", "turbidity", "ph", "discharge"}
    t = s["turbidity"]
    assert t["parameter_code"] == "63680"
    assert t["rows"] == 45 and t["series_found"] == 1
    assert t["chosen_series"] == "a837e6c6c9e045568d4f3c442b3541c2"
    assert t["first"] == pd.Timestamp(START) and t["last"] == pd.Timestamp(END)
    assert t["gaps"] == 1 and t["missing_intervals"] == 4
    assert t["qualifiers"] == {"P": 45}
    assert s["discharge"]["rows"] == 49 and s["discharge"]["gaps"] == 0

    dup = nwis.parse_nwis_iv(_load("nwis_iv_01576003_dup.json"))
    sd = nwis.summarize(dup, nwis.to_wide(dup))
    assert sd["conductivity"]["series_found"] == 2 and sd["conductivity"]["rows"] == 25
    assert sd["ph"]["rows"] == 0 and sd["ph"]["series_found"] == 0 and sd["ph"]["first"] is None


def test_default_window():
    start, end = nwis.default_window(datetime(2026, 10, 17, 15, 30, tzinfo=timezone.utc))
    assert end == datetime(2026, 10, 14, tzinfo=timezone.utc)
    assert start == datetime(2026, 9, 16, tzinfo=timezone.utc)


# ---- fetch with a fake session ---------------------------------------------------------------
class FakeResponse:
    def __init__(self, status: int, payload: dict | None = None, headers: dict | None = None):
        self.status_code, self._payload, self.headers = status, payload, headers or {}

    def json(self) -> dict:
        if self._payload is None:
            raise ValueError("no json")
        return self._payload


class FakeSession:
    """Replays responses per base URL and records every call."""

    def __init__(self, waterdata: list, iv: list | None = None):
        self.queues = {"waterdata": list(waterdata), "iv": list(iv or [])}
        self.calls: list[tuple[str, dict | None, dict]] = []

    def get(self, url, params=None, headers=None, timeout=None):
        assert timeout is not None
        self.calls.append((url, params, headers or {}))
        q = self.queues["iv" if url.startswith(nwis.NWIS_IV_URL) else "waterdata"]
        item = q.pop(0)
        if isinstance(item, Exception):
            raise item
        return item


@pytest.fixture
def sleeps(monkeypatch) -> list[float]:
    waits: list[float] = []
    monkeypatch.setattr(nwis, "_sleep", waits.append)
    monkeypatch.delenv(nwis.API_KEY_ENV, raising=False)
    return waits


def test_fetch_follows_pagination(wd_pages, sleeps):
    session = FakeSession([FakeResponse(200, p) for p in wd_pages])
    raw = nwis.fetch_nwis("01634000", START, END, session=session)
    assert len(session.calls) == 2 and sleeps == []
    first_url, first_params, _ = session.calls[0]
    assert first_url == nwis.WATERDATA_URL
    assert first_params["monitoring_location_id"] == "USGS-01634000"
    assert first_params["datetime"] == "2026-09-16T12:00:00Z/2026-09-17T00:00:00Z"
    assert first_params["parameter_code"] == "00095,63680,00400,00060"
    next_href = next(l["href"] for l in wd_pages[0]["links"] if l["rel"] == "next")
    assert session.calls[1][0] == next_href and session.calls[1][1] is None
    assert len(raw) == 184 and set(raw["source"]) == {"waterdata"}


def test_fetch_retries_then_succeeds(wd_pages, sleeps):
    session = FakeSession([
        FakeResponse(429, headers={"Retry-After": "7"}),
        FakeResponse(503),
        requests.ConnectionError("reset"),
        FakeResponse(200, wd_pages[0]),
        FakeResponse(200, wd_pages[1]),
    ])
    raw = nwis.fetch_nwis("01634000", START, END, session=session)
    assert sleeps == [7.0, 2.0, 4.0]  # Retry-After honoured, then exponential backoff
    assert len(raw) == 184 and set(raw["source"]) == {"waterdata"}


def test_fetch_falls_back_to_iv(sleeps):
    iv = _load("nwis_iv_01634000.json")
    session = FakeSession([FakeResponse(503)] * nwis.MAX_ATTEMPTS, [FakeResponse(200, iv)])
    raw = nwis.fetch_nwis("01634000", START, END, parameter_codes=["00095", "00060"], session=session)
    assert len(sleeps) == nwis.MAX_ATTEMPTS - 1
    url, params, _ = session.calls[-1]
    assert url == nwis.NWIS_IV_URL
    assert params == {"sites": "01634000", "parameterCd": "00095,00060", "startDT": "2026-09-16T12:00Z",
                      "endDT": "2026-09-17T00:00Z", "format": "json"}
    assert set(raw["source"]) == {"nwis_iv"}
    assert set(raw["parameter"]) == {"conductivity", "discharge"}


def test_fetch_falls_back_on_client_error_without_retry(sleeps):
    iv = _load("nwis_iv_01634000.json")
    session = FakeSession([FakeResponse(400)], [FakeResponse(200, iv)])
    raw = nwis.fetch_nwis("01634000", START, END, session=session)
    assert sleeps == [] and set(raw["source"]) == {"nwis_iv"}


def test_fetch_raises_when_both_fail(sleeps):
    session = FakeSession([FakeResponse(500)] * nwis.MAX_ATTEMPTS, [FakeResponse(502)] * nwis.MAX_ATTEMPTS)
    with pytest.raises(nwis.NwisFetchError):
        nwis.fetch_nwis("01634000", START, END, session=session)


def test_api_key_header_only_from_env(wd_pages, sleeps, monkeypatch):
    monkeypatch.setenv(nwis.API_KEY_ENV, "dummy-key")
    session = FakeSession([FakeResponse(200, p) for p in wd_pages])
    nwis.fetch_nwis("01634000", START, END, session=session)
    assert session.calls[0][2] == {"X-Api-Key": "dummy-key"}
