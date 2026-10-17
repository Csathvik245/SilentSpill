"""Tests for backend.app.data.echo and backend.app.data.tri, written from the step 3 contract.

Synthetic CSVs use the real column names plus irrelevant extras, non-VA rows, bad coordinates
(latitude 0 / positive longitude), blank ids, duplicate outfalls and latin-1 text.

match_tri geometry: the candidate sits at (38.9, -78.3); TRI points are placed d km due north at
latitude 38.9 + d / 111.0 (about 111 km per degree of latitude), so distances are ~d km in UTM.
"""
from __future__ import annotations

import math
import os
from datetime import date, datetime
from pathlib import Path

import pandas as pd
import pytest

from backend.app import schemas
from backend.app.data import echo, tri

# ---- helpers ---------------------------------------------------------------------------------


def _missing(v) -> bool:
    """None in the contract; pandas may surface it as NaN/NA inside a frame."""
    return v is None or (not isinstance(v, str) and pd.isna(v))


def _write_csv(path: Path, columns: list[str], rows: list[list], encoding: str = "utf-8") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    pd.DataFrame(rows, columns=columns, dtype=str).to_csv(path, index=False, encoding=encoding)
    return path


def _swap_same_size(path: Path, old: bytes, new: bytes, keep_mtime: bool) -> None:
    """Replace bytes without changing the file size; optionally restore the old mtime."""
    assert len(old) == len(new)
    st = os.stat(path)
    data = path.read_bytes()
    assert old in data
    path.write_bytes(data.replace(old, new))
    if keep_mtime:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns))
        assert os.stat(path).st_size == st.st_size
    else:
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 120 * 10**9))


# ---- ICIS_FACILITIES.csv ---------------------------------------------------------------------
FAC_COLS = ["NPDES_ID", "FACILITY_UIN", "FACILITY_NAME", "CITY", "STATE_CODE", "ZIP",
            "GEOCODE_LATITUDE", "GEOCODE_LONGITUDE", "FACILITY_TYPE_CODE"]
FAC_ROWS = [
    ["VA0000001", "110000000001", "Alpha Mill Inc", "Front Royal", "VA", "22630", "38.9", "-78.3", "POTW"],
    ["VA0000001", "110000000001", "Alpha Mill Inc", "Front Royal", "VA", "22630", "38.9", "-78.3", "POTW"],
    ["VA0000002", "", "Beta Works", "Richmond", "VA", "23219", "37.5", "-77.4", "NON"],
    ["VA0000003", "110000000003", "Zero Lat", "Nowhere", "VA", "", "0", "-78.0", "NON"],
    ["VA0000004", "110000000004", "Positive Lon", "Nowhere", "VA", "", "38.0", "78.0", "NON"],
    ["VA0000005", "110000000005", "No Coords", "Nowhere", "VA", "", "", "", "NON"],
    ["MD0000001", "110000000101", "Maryland Plant", "Annapolis", "MD", "21401", "39.0", "-76.9", "NON"],
    ["VA0000006", "110000000006", "Café Plant", "Staunton", "VA", "24401", "38.15", "-79.07", "NON"],
]

OUT_COLS = ["EXTERNAL_PERMIT_NMBR", "PERM_FEATURE_NMBR", "PERM_FEATURE_ID", "PERM_FEATURE_TYPE_CODE",
            "LATITUDE_MEASURE", "LONGITUDE_MEASURE", "HORIZ_ACCURACY"]
OUT_ROWS = [
    ["VA0000001", "001", "5001", "EXO", "38.9", "-78.3", "10"],
    ["VA0000001", "001", "5002", "EXO", "38.95", "-78.35", "10"],  # duplicate (permit, outfall)
    ["VA0000001", "002", "5003", "EXO", "38.91", "-78.31", "10"],
    ["MD0000001", "001", "5004", "EXO", "39.0", "-76.9", "10"],  # not VA
    ["VA0000002", "001", "5005", "EXO", "0", "-77.0", "10"],  # bad latitude
    ["VA0000002", "002", "5006", "EXO", "37.5", "77.4", "10"],  # positive longitude
    ["VA0000002", "003", "5007", "EXO", "37.51", "-77.41", "10"],
]

EFF_COLS = ["NPDES_ID", "NPDES_VIOLATION_ID", "PERM_FEATURE_NMBR", "PARAMETER_DESC", "VIOLATION_DESC",
            "MONITORING_PERIOD_END_DATE", "EXCEEDENCE_PCT", "RNC_RESOLUTION_DESC",
            "DMR_VALUE_STANDARD_UNITS", "LIMIT_VALUE_STANDARD_UNITS", "STANDARD_UNIT_DESC"]
EFF_ROWS = [
    ["VA0000001", "1", "001", "Solids, total dissolved", "DMR, Limited - Numeric Violation",
     "06/30/2024", "45", "Unresolved", "1450", "1000", "mg/L"],
    ["VA0000001", "2", "001", "Flow, in conduit or thru treatment plant", "DMR, Limited - Overdue",
     "05/31/2024", "", "", "", "", ""],
    ["VA0000002", "3", "", "pH", "DMR, Monitor Only - Overdue", "", "", "", "", "", ""],
    ["VA0000002", "4", "002", "pH", "dmr, limited - numeric violation", "12/31/2020", "3.5",
     "Resolved", "9.2", "9", "SU"],
]

TRI_COLS = ["1. YEAR", "2. TRIFD", "3. FRS ID", "4. FACILITY NAME", "5. STREET ADDRESS", "8. ST",
            "12. LATITUDE", "13. LONGITUDE", "37. CHEMICAL", "50. UNIT OF MEASURE",
            "53. 5.3 - WATER", "107. TOTAL RELEASES"]
TRI_ROWS = [
    ["2024", "22630LPHML1MAIN", "110000000001", "ALPHA MILL INC", "1 Main St", "VA",
     "38.9", "-78.3", "Zinc compounds", "Pounds", "12.5", "100"],
    ["2024", "22630LPHML1MAIN", "110000000001", "ALPHA MILL INC", "1 Main St", "VA",
     "38.9", "-78.3", "Lead", "Pounds", "", "4"],
    ["2024", "21401MRYLN1BAYS", "110000000101", "MARYLAND PLANT", "2 Bay St", "MD",
     "39.0", "-76.9", "Ammonia", "Pounds", "50", "60"],
    ["2024", "24401CFPLN2HILL", "", "CAFÉ CHEMICAL", "3 Hill Rd", "VA",
     "38.15", "-79.07", "Ammonia", "Pounds", "0", "7"],
]


@pytest.fixture
def npdes_dir(tmp_path):
    d = tmp_path / "npdes"
    _write_csv(d / "ICIS_FACILITIES.csv", FAC_COLS, FAC_ROWS, encoding="latin-1")
    _write_csv(d / "NPDES_PERM_FEATURE_COORDS.csv", OUT_COLS, OUT_ROWS)
    _write_csv(d / "ICIS_PERMITS.csv", ["EXTERNAL_PERMIT_NMBR", "X"], [["VA0000001", "y"]])  # ignored
    return d


@pytest.fixture
def effluent_dir(tmp_path):
    d = tmp_path / "effluent"
    _write_csv(d / "VA_NPDES_EFF_VIOLATIONS.csv", EFF_COLS, EFF_ROWS)
    return d


@pytest.fixture
def tri_path(tmp_path):
    return _write_csv(tmp_path / "tri" / "2024_us.csv", TRI_COLS, TRI_ROWS, encoding="latin-1")


@pytest.fixture
def cache(tmp_path):
    return tmp_path / "cache"


# ---- load_facilities -------------------------------------------------------------------------
def test_facilities_va_only_valid_coords(npdes_dir, cache):
    df = echo.load_facilities(npdes_dir, cache_dir=cache)
    assert set(df.columns) >= {"npdes_id", "name", "frs_id", "latitude", "longitude"}
    # MD dropped, lat 0 / positive lon / blank coords dropped, duplicate collapsed.
    assert sorted(df["npdes_id"]) == ["VA0000001", "VA0000002", "VA0000006"]
    assert df["npdes_id"].is_unique
    lat_min, lat_max, lon_min, lon_max = echo.VA_BBOX
    assert df["latitude"].between(lat_min, lat_max).all()
    assert df["longitude"].between(lon_min, lon_max).all()


def test_facilities_types_and_values(npdes_dir, cache):
    df = echo.load_facilities(str(npdes_dir), cache_dir=str(cache)).set_index("npdes_id")
    assert pd.api.types.is_float_dtype(df["latitude"]) and pd.api.types.is_float_dtype(df["longitude"])
    a = df.loc["VA0000001"]
    assert a["name"] == "Alpha Mill Inc"
    assert a["frs_id"] == "110000000001" and isinstance(a["frs_id"], str)
    assert a["latitude"] == pytest.approx(38.9) and a["longitude"] == pytest.approx(-78.3)
    assert _missing(df.loc["VA0000002", "frs_id"])  # blank FACILITY_UIN -> None
    assert df.loc["VA0000006", "name"] == "Café Plant"  # latin-1 decoded


def test_va_bbox_constant():
    assert echo.VA_BBOX == (36.0, 40.0, -84.0, -75.0)


# ---- load_outfalls ---------------------------------------------------------------------------
def test_outfalls_filter_and_dedup(npdes_dir, cache):
    df = echo.load_outfalls(npdes_dir, cache_dir=cache)
    assert set(df.columns) >= {"npdes_id", "outfall", "latitude", "longitude"}
    got = sorted(zip(df["npdes_id"], df["outfall"]))
    assert got == [("VA0000001", "001"), ("VA0000001", "002"), ("VA0000002", "003")]
    first = df[(df["npdes_id"] == "VA0000001") & (df["outfall"] == "001")].iloc[0]
    assert first["latitude"] == pytest.approx(38.9)  # first duplicate kept
    assert first["longitude"] == pytest.approx(-78.3)
    assert all(isinstance(o, str) for o in df["outfall"])  # "001" keeps its leading zeros
    assert pd.api.types.is_float_dtype(df["latitude"])


# ---- load_violations -------------------------------------------------------------------------
def test_violations_columns_and_parsing(effluent_dir, cache):
    df = echo.load_violations(effluent_dir, cache_dir=cache)
    assert set(df.columns) >= {
        "npdes_id", "outfall", "parameter", "violation_desc", "period_end", "is_numeric",
        "is_reporting", "exceedance_pct", "status", "dmr_value", "limit_value", "unit",
    }
    assert len(df) == 4  # all rows kept
    df = df.sort_values("npdes_id", kind="stable").reset_index(drop=True)
    assert list(df["is_numeric"]) == [True, False, False, True]  # case-insensitive
    assert list(df["is_reporting"]) == [False, True, True, False]
    assert pd.api.types.is_bool_dtype(df["is_numeric"]) and pd.api.types.is_bool_dtype(df["is_reporting"])

    r0 = df.iloc[0]
    assert isinstance(r0["period_end"], date) and not isinstance(r0["period_end"], datetime)
    assert r0["period_end"] == date(2024, 6, 30)
    assert r0["exceedance_pct"] == pytest.approx(45.0)
    assert r0["parameter"] == "Solids, total dissolved"
    assert r0["outfall"] == "001"
    assert r0["status"] == "Unresolved"
    assert r0["dmr_value"] == "1450" and r0["limit_value"] == "1000" and r0["unit"] == "mg/L"

    r1 = df.iloc[1]
    assert _missing(r1["exceedance_pct"]) and _missing(r1["status"]) and _missing(r1["unit"])
    r2 = df.iloc[2]
    assert _missing(r2["period_end"]) and _missing(r2["outfall"])
    assert df.iloc[3]["period_end"] == date(2020, 12, 31)


# ---- load_tri --------------------------------------------------------------------------------
def test_tri_prefix_stripped_and_va_only(tri_path, cache):
    df = tri.load_tri(tri_path, cache_dir=cache)
    assert set(df.columns) >= {"trifd", "frs_id", "name", "latitude", "longitude", "chemical",
                               "year", "water_release", "unit"}
    assert not any(c[:1].isdigit() for c in df.columns)
    assert len(df) == 3 and set(df["trifd"]) == {"22630LPHML1MAIN", "24401CFPLN2HILL"}
    assert pd.api.types.is_integer_dtype(df["year"]) and set(df["year"]) == {2024}
    zinc = df[df["chemical"] == "Zinc compounds"].iloc[0]
    assert zinc["water_release"] == pytest.approx(12.5)
    assert zinc["frs_id"] == "110000000001" and zinc["unit"] == "Pounds"
    assert zinc["name"] == "ALPHA MILL INC"
    assert zinc["latitude"] == pytest.approx(38.9) and zinc["longitude"] == pytest.approx(-78.3)
    assert _missing(df[df["chemical"] == "Lead"].iloc[0]["water_release"])
    cafe = df[df["trifd"] == "24401CFPLN2HILL"].iloc[0]
    assert cafe["name"] == "CAFÉ CHEMICAL"
    assert _missing(cafe["frs_id"])
    assert cafe["water_release"] == pytest.approx(0.0)  # zero is a value, not missing


# ---- parquet caching -------------------------------------------------------------------------
def _cached_files(cache: Path) -> list[Path]:
    return [p for p in cache.rglob("*") if p.is_file()]


@pytest.mark.parametrize(
    "loader, src, old, new, column, key_col, key",
    [
        ("facilities", "ICIS_FACILITIES.csv", b"Alpha Mill", b"Omega Mill", "name", "npdes_id", "VA0000001"),
        ("outfalls", "NPDES_PERM_FEATURE_COORDS.csv", b"38.91", b"38.92", "latitude", "outfall", "002"),
        ("violations", "VA_NPDES_EFF_VIOLATIONS.csv", b"Unresolved", b"Unresolvex", "status", "parameter",
         "Solids, total dissolved"),
        ("tri", "2024_us.csv", b"Zinc compounds", b"Zinc compoundz", "chemical", "unit", "Pounds"),
    ],
)
def test_cache_reused_until_mtime_changes(npdes_dir, effluent_dir, tri_path, cache,
                                          loader, src, old, new, column, key_col, key):
    calls = {
        "facilities": (lambda: echo.load_facilities(npdes_dir, cache_dir=cache), npdes_dir / src),
        "outfalls": (lambda: echo.load_outfalls(npdes_dir, cache_dir=cache), npdes_dir / src),
        "violations": (lambda: echo.load_violations(effluent_dir, cache_dir=cache), effluent_dir / src),
        "tri": (lambda: tri.load_tri(tri_path, cache_dir=cache), tri_path),
    }
    load, path = calls[loader]

    def values():
        df = load()
        return sorted(str(v) for v in df.loc[df[key_col] == key, column])

    first = values()
    assert cache.is_dir() and _cached_files(cache)
    assert not any(p.suffix == ".tmp" for p in _cached_files(cache))  # tmp file replaced

    # Same size + same mtime -> cached frame, even though the content changed.
    _swap_same_size(path, old, new, keep_mtime=True)
    assert values() == first

    # mtime changes -> rebuilt from the CSV.
    _swap_same_size(path, new, new, keep_mtime=False)
    rebuilt = values()
    assert rebuilt != first
    assert any(new.decode() in v for v in rebuilt)


def test_cache_second_call_does_not_read_csv(npdes_dir, cache, monkeypatch):
    a = echo.load_facilities(npdes_dir, cache_dir=cache)

    def boom(*args, **kwargs):
        raise AssertionError("CSV re-read despite a valid cache")

    monkeypatch.setattr(pd, "read_csv", boom)
    b = echo.load_facilities(npdes_dir, cache_dir=cache)
    assert sorted(a["npdes_id"]) == sorted(b["npdes_id"])


# ---- name_similarity -------------------------------------------------------------------------
@pytest.mark.parametrize(
    "a, b, expected",
    [
        ("Alpha Mill Inc", "ALPHA MILL, INC.", 1.0),  # case, punctuation, suffix
        ("The Beta Company", "beta", 1.0),  # THE / COMPANY dropped
        ("Alpha Mill LLC", "alpha mill corp", 1.0),
        ("Gamma Chemical Plant", "GAMMA CHEMICAL FACILITY LP", 1.0),
        ("Gamma Chemical Corp", "Gamma Plastics Ltd", 1 / 3),  # {GAMMA, CHEMICAL} vs {GAMMA, PLASTICS}
        ("Delta Power Station", "Delta Power Co.", 2 / 3),
        ("Acme Paper", "Zenith Foods", 0.0),
    ],
)
def test_name_similarity(a, b, expected):
    assert tri.name_similarity(a, b) == pytest.approx(expected)
    assert tri.name_similarity(b, a) == pytest.approx(expected)


# ---- match_tri -------------------------------------------------------------------------------
CAND_LAT, CAND_LON = 38.9, -78.3


def _tri_frame(rows):
    """rows: (trifd, frs_id, name, km_north, chemical, water_release)."""
    return pd.DataFrame(
        [
            {"trifd": t, "frs_id": f, "name": n, "latitude": CAND_LAT + km / 111.0,
             "longitude": CAND_LON, "chemical": c, "year": 2024, "water_release": w, "unit": "Pounds"}
            for t, f, n, km, c, w in rows
        ]
    )


def _match(frame, frs=None, name="Beta Works Inc", echo_ids=frozenset(), **kw):
    return tri.match_tri(frs, name, CAND_LAT, CAND_LON, frame, set(echo_ids), **kw)


def test_match_tri_frs_id_high_and_ordering():
    frame = _tri_frame([
        ("T1", "F1", "ALPHA MILL", 5.0, "Zinc compounds", 5.0),
        ("T1", "F1", "ALPHA MILL", 5.0, "Lead", None),
        ("T1", "F1", "ALPHA MILL", 5.0, "Copper", 20.0),
        ("T1", "F1", "ALPHA MILL", 5.0, "Arsenic", 5.0),
        ("T2", None, "BETA WORKS", 0.1, "Benzene", 99.0),  # near and similar, but FRS wins
    ])
    releases, how, conf = _match(frame, frs="F1", echo_ids={"F1"})
    assert (how, conf) == ("frs_id", "high")
    assert all(isinstance(r, schemas.TriRelease) for r in releases)
    # water_release desc, None last, then chemical
    assert [r.chemical for r in releases] == ["Copper", "Arsenic", "Zinc compounds", "Lead"]
    assert releases[0].water_release == pytest.approx(20.0) and releases[-1].water_release is None
    assert releases[0].year == 2024 and releases[0].unit == "Pounds"
    for r in releases:
        assert any("5.3 - WATER" in f for f in r.source_fields)
        assert any("CHEMICAL" in f for f in r.source_fields)


def test_match_tri_proximity_name_low():
    frame = _tri_frame([("T2", None, "BETA WORKS", 0.3, "Benzene", 1.0)])
    releases, how, conf = _match(frame, frs=None)
    assert (how, conf) == ("proximity_name", "low")
    assert [r.chemical for r in releases] == ["Benzene"]
    # A candidate FRS id with no TRI rows also falls back.
    assert _match(frame, frs="F404")[1:] == ("proximity_name", "low")


def test_match_tri_fallback_refuses_tri_site_tied_to_other_echo_facility():
    frame = _tri_frame([("T2", "F2", "BETA WORKS", 0.1, "Benzene", 1.0)])
    assert _match(frame, echo_ids={"F2"}) == ([], None, None)
    assert _match(frame, echo_ids={"F9"})[1:] == ("proximity_name", "low")


def test_match_tri_refuses_too_far_or_different_name():
    far = _tri_frame([("T2", None, "BETA WORKS", 1.5, "Benzene", 1.0)])
    assert _match(far) == ([], None, None)
    assert _match(far, max_distance_m=2000.0)[1:] == ("proximity_name", "low")
    other = _tri_frame([("T3", None, "GAMMA CHEMICAL", 0.1, "Toluene", 1.0)])
    assert _match(other) == ([], None, None)


def test_match_tri_nearest_wins():
    frame = _tri_frame([
        ("TFAR", None, "BETA WORKS", 0.8, "Far chemical", 1.0),
        ("TNEAR", None, "BETA WORKS INC", 0.2, "Near chemical", 1.0),
    ])
    releases, how, _ = _match(frame)
    assert how == "proximity_name"
    assert [r.chemical for r in releases] == ["Near chemical"]


def test_match_tri_no_match():
    frame = _tri_frame([("T3", None, "GAMMA CHEMICAL", 3.0, "Toluene", 1.0)])
    assert _match(frame, frs="F404", name="Zeta Foods") == ([], None, None)
