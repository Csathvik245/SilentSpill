"""Tests for backend.app.data.facilities, written from the step 3 contract on a synthetic network.

Synthetic network (arrows point downstream; length km / va ft/s):

    202 (3.0 / 2.0)
       |
    201 (2.0 / 2.0)     103 (1.0 / -9998, missing)
       |                  |
       |                102 (4.0 / 0.05, very slow)
        \\               /
         `---> 101 (3.0 / 2.0)
                  |
                100 (2.0 / 1.0)  <- start reach
                  |
                900 (5.0 / 2.0)  downstream, never traced

cumulative_km from 100: 100 0, 101 3, 201 5, 102 7, 103 8, 202 8.
upstream_distance_km = cumulative_km - length_km (start reach 0.0):
    100 0.0, 101 0.0, 102 3.0, 201 3.0, 202 5.0, 103 7.0.

Geometry: each reach is an east-west line at its own latitude (5.5 km apart) from -78.32 to -78.28.
Points sit at longitude -78.30, d km north of a line: latitude = line + d / 111.0.

Permits (expected rank, nearest reach, distance km):
    VA0000001 Alpha  outfalls 001 (0.7) and 002 (0.2) near 101; facility point 0.05 is ignored -> rank 1, 0.2
    VA0000002 Beta   facility only near 100 at 0.4; only violation 2020-12-31   -> rank 2, 0.4
    VA0000008 Gamma  near 100 at 0.6 (same point as VA0000009)                  -> rank 3, 0.6 (id tie-break)
    VA0000009 Gamma  near 100 at 0.6                                            -> rank 4, 0.6
    VA0000003 Epsilon near 102 at 0.3 (upstream 3.0, travel 76.1 h)            -> rank 5
    VA0000004 Zeta   near 202 at 0.1 (upstream 5.0, travel 5.47 h)             -> rank 6 (after VA0000003 despite
                                                                                  the shorter travel time)
    VA0000005 Eta    near 103 at 0.5 (upstream 7.0, travel lower bound)         -> rank 7
Excluded: VA0000006 (near downstream reach 900), VA0000007 (1.5 km from 101, outside buffer),
VA0000010 (reporting violations only), VA0000011 (numeric violation before the window),
VA0000012 (outfall + violations but not in facilities).

Window: max period_end is 2025-06-30, so lookback 5 years starts 2020-06-30. A "today"-based window
(2026-10-17 minus 5 years) would wrongly drop Beta.

Travel times (full reach lengths, ft/s * 0.3048):
    [100]                 2000/0.3048                             = 6561.68 s  = 1.82269 h
    [101,100]             3000/0.6096 + 2000/0.3048              = 11482.94 s = 3.18971 h
    [102,101,100]         4000/0.01524 + 11482.94                = 273950.13 s = 76.0973 h
    [202,201,101,100]     3000/0.6096 + 2000/0.6096 + 11482.94   = 19685.04 s = 5.46807 h
    [103,102,101,100]     103 missing -> hours None, lower bound 76.0973 h
"""
from __future__ import annotations

import sqlite3
from datetime import date

import geopandas as gpd
import pandas as pd
import pytest
from shapely.geometry import LineString

from backend.app import schemas
from backend.app.config import SOURCE_CRS, ConfigError, get_settings
from backend.app.data import facilities as fac
from backend.app.data.nhd_graph import build_graph, downstream_path, travel_time

START = 100

# id: (gnis_name, lengthkm, ftype, flowdir, hydroseq, va, qa)
REACHES = {
    900: ("Main River", 5.0, 460, 1, 1.0, 2.0, 500.0),
    100: ("Main River", 2.0, 460, 1, 2.0, 1.0, 400.0),
    101: ("Main River", 3.0, 460, 1, 3.0, 2.0, 300.0),
    102: (None, 4.0, 460, 1, 4.0, 0.05, 200.0),
    103: (None, 1.0, 460, 1, 5.0, -9998.0, 100.0),
    201: ("Trib Creek", 2.0, 460, 1, 6.0, 2.0, 50.0),
    202: ("Trib Creek", 3.0, 460, 1, 7.0, 2.0, 40.0),
}
FLOWS = [(100, 900), (900, 0), (101, 100), (102, 101), (103, 102), (201, 101), (202, 201)]

LINE_LAT = {900: 38.70, 100: 38.75, 101: 38.80, 102: 38.85, 103: 38.90, 201: 38.95, 202: 39.00}
PT_LON = -78.30


def _write_db(path, reaches=REACHES, flows=FLOWS):
    con = sqlite3.connect(path)
    con.executescript(
        """
        CREATE TABLE nhdflowline_va(nhdplusid REAL, gnis_name TEXT, lengthkm REAL,
                                    ftype INTEGER, flowdir INTEGER);
        CREATE TABLE nhdplusflowlinevaa_va(nhdplusid REAL, hydroseq REAL);
        CREATE TABLE nhdpluseromma_va(nhdplusid REAL, va REAL, qa REAL);
        CREATE TABLE nhdplusflow_va(fromnhdpid REAL, tonhdpid REAL);
        """
    )
    for rid, (name, length, ftype, flowdir, hs, va, qa) in reaches.items():
        con.execute("INSERT INTO nhdflowline_va VALUES (?,?,?,?,?)",
                    (float(rid), name, length, ftype, flowdir))
        if hs is not None:
            con.execute("INSERT INTO nhdplusflowlinevaa_va VALUES (?,?)", (float(rid), hs))
        if va != "noerom":
            con.execute("INSERT INTO nhdpluseromma_va VALUES (?,?,?)", (float(rid), va, qa))
    con.executemany("INSERT INTO nhdplusflow_va VALUES (?,?)",
                    [(float(u), float(v)) for u, v in flows])
    con.commit()
    con.close()


def _lat(reach: int, km: float) -> float:
    return LINE_LAT[reach] + km / 111.0


@pytest.fixture
def G(tmp_path):
    p = tmp_path / "net.gpkg"
    _write_db(p)
    return build_graph(p)


@pytest.fixture
def reach_geoms():
    # Includes downstream reach 900: only traced reaches may be used.
    return gpd.GeoDataFrame(
        {"nhdplusid": list(LINE_LAT)},
        geometry=[LineString([(-78.32, lat), (-78.28, lat)]) for lat in LINE_LAT.values()],
        crs=SOURCE_CRS,
    )


FACILITIES = pd.DataFrame(
    [
        # npdes_id, name, frs_id, latitude, longitude
        ("VA0000001", "Alpha Mill Inc", "110000000001", _lat(101, 0.05), PT_LON),
        ("VA0000002", "Beta Works", None, _lat(100, 0.4), PT_LON),
        ("VA0000003", "Epsilon Facility", None, _lat(102, 0.3), PT_LON),
        ("VA0000004", "Zeta Foods", "110000000004", _lat(202, 0.1), PT_LON),
        ("VA0000005", "Eta Quarry", None, _lat(103, 0.5), PT_LON),
        ("VA0000006", "Downstream Dairy", None, _lat(900, 0.1), PT_LON),
        ("VA0000007", "Faraway Farm", None, _lat(101, 1.5), PT_LON),
        ("VA0000008", "Gamma Works", "110000000008", _lat(100, 0.6), PT_LON),
        ("VA0000009", "Gamma Works", None, _lat(100, 0.6), PT_LON),
        ("VA0000010", "Late Paperwork Co", None, _lat(101, 0.3), PT_LON),
        ("VA0000011", "Old News Inc", None, _lat(101, 0.3), PT_LON),
    ],
    columns=["npdes_id", "name", "frs_id", "latitude", "longitude"],
)

OUTFALLS = pd.DataFrame(
    [
        ("VA0000001", "001", _lat(101, 0.7), PT_LON),
        ("VA0000001", "002", _lat(101, 0.2), PT_LON - 0.001),
        ("VA0000012", "001", _lat(101, 0.1), PT_LON),  # permit absent from FACILITIES
    ],
    columns=["npdes_id", "outfall", "latitude", "longitude"],
)

NUMERIC = "DMR, Limited - Numeric Violation"
OVERDUE = "DMR, Limited - Overdue"
ALPHA_PARAMS = ["Solids, total dissolved", "pH", "BOD, 5-day, 20 deg. C"]
ALPHA_DATES = [d.date() for d in pd.date_range("2023-01-31", "2025-06-30", freq="ME")]  # 30 months


def _v(npdes_id, parameter, desc, d, outfall="001"):
    return {
        "npdes_id": npdes_id, "outfall": outfall, "parameter": parameter, "violation_desc": desc,
        "period_end": d, "is_numeric": "numeric violation" in desc.lower(),
        "is_reporting": "overdue" in desc.lower(), "exceedance_pct": 10.0, "status": "Unresolved",
        "dmr_value": "2", "limit_value": "1", "unit": "mg/L",
    }


def _violations():
    rows = [_v("VA0000001", ALPHA_PARAMS[i % 3], NUMERIC, d) for i, d in enumerate(ALPHA_DATES)]
    rows += [
        _v("VA0000001", "Flow, in conduit or thru treatment plant", OVERDUE, date(2024, 1, 31)),
        _v("VA0000001", "Flow, in conduit or thru treatment plant", OVERDUE, date(2024, 2, 29)),
        _v("VA0000001", "Flow, in conduit or thru treatment plant", OVERDUE, date(2018, 1, 31)),  # old
        _v("VA0000001", "Turbidity", NUMERIC, date(2019, 12, 31)),  # before window
        _v("VA0000002", "Solids, total suspended", NUMERIC, date(2020, 12, 31)),
        _v("VA0000003", "pH", NUMERIC, date(2024, 3, 31)),
        _v("VA0000004", "Copper, total recoverable", NUMERIC, date(2024, 3, 31)),
        _v("VA0000005", "Specific conductance", NUMERIC, date(2022, 3, 31)),
        _v("VA0000006", "pH", NUMERIC, date(2024, 3, 31)),
        _v("VA0000007", "pH", NUMERIC, date(2024, 3, 31)),
        _v("VA0000008", "pH", NUMERIC, date(2024, 9, 30)),
        _v("VA0000009", "pH", NUMERIC, date(2024, 9, 30)),
        _v("VA0000010", "pH", OVERDUE, date(2024, 3, 31)),
        _v("VA0000010", "Flow", "DMR, Monitor Only - Overdue", date(2024, 4, 30)),
        _v("VA0000011", "pH", NUMERIC, date(2019, 6, 30)),
        _v("VA0000012", "pH", NUMERIC, date(2024, 3, 31)),
    ]
    return pd.DataFrame(rows)


VIOLATIONS = _violations()

TRI = pd.DataFrame(
    [
        # trifd, frs_id, name, latitude, longitude, chemical, year, water_release, unit
        ("T_ALPHA", "110000000001", "ALPHA MILL", 39.5, -77.0, "Zinc compounds", 2024, 12.0, "Pounds"),
        ("T_ALPHA", "110000000001", "ALPHA MILL", 39.5, -77.0, "Lead", 2024, 3.0, "Pounds"),
        ("T_EPS", "110000000099", "EPSILON FACILITY LLC", _lat(102, 0.4), PT_LON, "Ammonia", 2024, 5.0, "Pounds"),
        ("T_GAMMA", "110000000008", "GAMMA WORKS", _lat(100, 0.6), PT_LON, "Nitrate", 2024, 1.0, "Pounds"),
    ],
    columns=["trifd", "frs_id", "name", "latitude", "longitude", "chemical", "year", "water_release", "unit"],
)

EXPECTED_ORDER = ["VA0000001", "VA0000002", "VA0000008", "VA0000009", "VA0000003", "VA0000004", "VA0000005"]
EXPECTED = {  # id: (nearest reach, distance km, upstream km, travel path)
    "VA0000001": (101, 0.2, 0.0, [101, 100]),
    "VA0000002": (100, 0.4, 0.0, [100]),
    "VA0000008": (100, 0.6, 0.0, [100]),
    "VA0000009": (100, 0.6, 0.0, [100]),
    "VA0000003": (102, 0.3, 3.0, [102, 101, 100]),
    "VA0000004": (202, 0.1, 5.0, [202, 201, 101, 100]),
    "VA0000005": (103, 0.5, 7.0, [103, 102, 101, 100]),
}
BAD_WORDS = ("caused", "responsible", "cause ", "polluter")


DOMESTIC_GP = "VPDES General Permit for Domestic Sewage Discharges of Less Than or Equal to 1,000 Gallons Per Day"
PERMITS = pd.DataFrame(
    [
        # npdes_id, type code, status, POTW indicator, master permit, master title, facility type code
        ("VA0000001", "NPD", "EFF", "NON-POTW", None, None, "COR"),
        ("VA0000002", "GPC", "EFF", "NON-POTW", "VAG400000", DOMESTIC_GP, "POF"),
        ("VA0000003", "GPC", "EFF", "NON-POTW", "VAG840000", "General Permit for Nonmetallic Mineral Mining", "COR"),
        ("VA0000004", "APR", "EFF", None, None, None, None),
        # VA0000005, 08 and 09 have no permit record
    ],
    columns=["npdes_id", "permit_type_code", "permit_status_code", "facility_type_indicator",
             "general_permit_id", "general_permit_name", "facility_type_code"],
)


def _find(G, reach_geoms, **kw):
    kw.setdefault("facilities", FACILITIES)
    kw.setdefault("outfalls", OUTFALLS)
    kw.setdefault("violations", VIOLATIONS)
    kw.setdefault("tri", TRI)
    kw.setdefault("permits", PERMITS)
    return fac.find_candidate_facilities(G, START, 50, reach_geoms=reach_geoms, **kw)


@pytest.fixture
def cands(G, reach_geoms):
    return _find(G, reach_geoms)


# ---- constants -------------------------------------------------------------------------------
def test_defaults():
    assert fac.DEFAULT_BUFFER_M == 1000.0
    assert fac.DEFAULT_LOOKBACK_YEARS == 5


# ---- match_parameters ------------------------------------------------------------------------
ALL = ["conductivity", "turbidity", "ph"]


def _pm(out):
    return [(m.sensor_parameter, m.echo_parameter, m.is_proxy) for m in out]


def test_match_parameters_tds_proxy():
    out = fac.match_parameters(["Solids, total dissolved"], ["conductivity"])
    assert _pm(out) == [("conductivity", "Solids, total dissolved", True)]
    assert isinstance(out[0], schemas.ParameterMatch)
    assert "TDS" in out[0].note and "proxy" in out[0].note.lower()


def test_match_parameters_conductance_direct_case_insensitive():
    assert _pm(fac.match_parameters(["Specific conductance"], ALL)) == [
        ("conductivity", "Specific conductance", False)]
    assert _pm(fac.match_parameters(["SPECIFIC CONDUCTANCE, FIELD"], ALL)) == [
        ("conductivity", "SPECIFIC CONDUCTANCE, FIELD", False)]
    assert _pm(fac.match_parameters(["SOLIDS, TOTAL DISSOLVED - 180 DEG. C"], ALL)) == [
        ("conductivity", "SOLIDS, TOTAL DISSOLVED - 180 DEG. C", True)]


def test_match_parameters_turbidity():
    out = fac.match_parameters(["Solids, total suspended"], ["turbidity"])
    assert _pm(out) == [("turbidity", "Solids, total suspended", True)]
    assert "TSS" in out[0].note and "proxy" in out[0].note.lower()
    assert _pm(fac.match_parameters(["Turbidity"], ["turbidity"])) == [("turbidity", "Turbidity", False)]


def test_match_parameters_ph_exact_or_prefix_only():
    assert _pm(fac.match_parameters(["pH"], ["ph"])) == [("ph", "pH", False)]
    assert _pm(fac.match_parameters(["pH, maximum"], ["ph"])) == [("ph", "pH, maximum", False)]
    assert fac.match_parameters(["Length of longest pH excursion"], ["ph"]) == []
    assert fac.match_parameters(["Phosphorus, total [as P]", "Phenols"], ALL) == []
    assert fac.match_parameters(["pH range excursions, > 60 minutes"], ["ph"]) == []


def test_match_parameters_only_requested_and_never_discharge():
    params = ["Solids, total dissolved", "Turbidity", "pH", "Flow, in conduit or thru treatment plant",
              "Discharge"]
    assert _pm(fac.match_parameters(params, ["turbidity"])) == [("turbidity", "Turbidity", False)]
    assert fac.match_parameters(params, ["discharge"]) == []
    assert fac.match_parameters(params, []) == []
    assert fac.match_parameters([], ALL) == []


def test_match_parameters_dedup_and_sort():
    params = ["pH", "Turbidity", "pH", "Specific conductance", "Solids, total suspended",
              "Solids, total dissolved", "Turbidity", "BOD, 5-day, 20 deg. C"]
    out = fac.match_parameters(iter(params), iter(["ph", "turbidity", "conductivity"]))
    assert _pm(out) == [
        ("conductivity", "Solids, total dissolved", True),
        ("conductivity", "Specific conductance", False),
        ("ph", "pH", False),
        ("turbidity", "Solids, total suspended", True),
        ("turbidity", "Turbidity", False),
    ]


# ---- facility_points -------------------------------------------------------------------------
def test_facility_points():
    f = pd.DataFrame(
        [("VA1", "One", "F1", 38.0, -78.0), ("VA2", "Two", None, 38.5, -78.5)],
        columns=["npdes_id", "name", "frs_id", "latitude", "longitude"],
    )
    o = pd.DataFrame(
        [("VA2", "001", 38.51, -78.51), ("VA2", "002", 38.52, -78.52), ("VA3", "001", 38.6, -78.6)],
        columns=["npdes_id", "outfall", "latitude", "longitude"],
    )
    pts = fac.facility_points(f, o)
    assert set(pts.columns) >= {"npdes_id", "name", "frs_id", "latitude", "longitude",
                                "location_type", "outfall"}
    assert "VA3" not in set(pts["npdes_id"])  # not in facilities: skipped
    assert len(pts) == 3

    one = pts[pts["npdes_id"] == "VA1"]
    assert len(one) == 1
    r = one.iloc[0]
    assert r["location_type"] == "facility" and r["name"] == "One" and r["frs_id"] == "F1"
    assert r["latitude"] == pytest.approx(38.0) and r["longitude"] == pytest.approx(-78.0)
    assert r["outfall"] is None or pd.isna(r["outfall"])

    two = pts[pts["npdes_id"] == "VA2"].sort_values("outfall")
    assert list(two["location_type"]) == ["outfall", "outfall"]  # facility point replaced
    assert list(two["outfall"]) == ["001", "002"]
    assert list(two["name"]) == ["Two", "Two"]
    assert list(two["latitude"]) == pytest.approx([38.51, 38.52])
    assert list(two["longitude"]) == pytest.approx([-78.51, -78.52])


# ---- find_candidate_facilities ---------------------------------------------------------------
def test_candidates_and_ranking(cands):
    assert all(isinstance(c, schemas.CandidateFacility) for c in cands)
    assert [c.facility_id for c in cands] == EXPECTED_ORDER
    assert [c.rank for c in cands] == list(range(1, len(cands) + 1))
    for c in cands:
        assert c.rank_basis == (
            "river distance upstream of the spill reach (km), then distance to the river (km); "
            "not travel time"
        )


def test_excluded_permits(cands):
    ids = {c.facility_id for c in cands}
    assert "VA0000006" not in ids  # near a downstream reach only
    assert "VA0000007" not in ids  # outside the 1 km buffer
    assert "VA0000010" not in ids  # reporting violations only
    assert "VA0000011" not in ids  # numeric violation older than the window
    assert "VA0000012" not in ids  # not in the facilities frame


def test_distances_reaches_and_upstream(cands):
    for c in cands:
        reach, dist, up, _ = EXPECTED[c.facility_id]
        assert c.nearest_reach_nhdplusid == reach, c.facility_id
        assert c.distance_km_to_reach == pytest.approx(dist, abs=0.005), c.facility_id
        assert c.upstream_distance_km == pytest.approx(up), c.facility_id
        assert c.distance_km_to_reach <= 1.0


def test_ranking_ignores_travel_time(cands, G):
    by = {c.facility_id: c for c in cands}
    for fid, (_, _, _, path) in EXPECTED.items():
        assert by[fid].travel_time == travel_time(G, path), fid
        assert downstream_path(G, path[0], START) == path
    # Zeta (VA0000004) reaches the spill faster than Epsilon (VA0000003) but is further upstream.
    assert by["VA0000004"].travel_time.hours == pytest.approx(19685.03937007874 / 3600)
    assert by["VA0000003"].travel_time.hours == pytest.approx(273950.1312335958 / 3600)
    assert by["VA0000004"].travel_time.hours < by["VA0000003"].travel_time.hours
    assert by["VA0000003"].rank < by["VA0000004"].rank
    assert by["VA0000002"].travel_time.hours == pytest.approx(2000 / 0.3048 / 3600)
    assert by["VA0000001"].travel_time.hours == pytest.approx(11482.939632545932 / 3600)


def test_travel_time_lower_bound_when_velocity_missing(cands):
    eta = {c.facility_id: c for c in cands}["VA0000005"]
    tt = eta.travel_time
    assert tt.hours is None
    assert tt.is_lower_bound is True
    assert tt.travel_time_hours_min == pytest.approx(273950.1312335958 / 3600)
    assert tt.reaches_missing_velocity == [103]
    assert eta.rank == 7  # ranked by distance regardless


def test_nearest_point_is_used(cands):
    by = {c.facility_id: c for c in cands}
    a = by["VA0000001"]
    assert a.location_type == "outfall"
    assert a.latitude == pytest.approx(_lat(101, 0.2)) and a.longitude == pytest.approx(PT_LON - 0.001)
    assert a.location_note == (
        "Reported outfall location (ECHO permitted feature) 002; not a surveyed discharge point."
    )
    b = by["VA0000002"]
    assert b.location_type == "facility"
    assert b.latitude == pytest.approx(_lat(100, 0.4)) and b.longitude == pytest.approx(PT_LON)
    assert b.location_note == (
        "Reported facility location (ECHO geocode); no outfall coordinates in local data, "
        "so this is less precise."
    )


def test_violations_window_cap_and_counts(cands):
    by = {c.facility_id: c for c in cands}
    a = by["VA0000001"]
    assert a.numeric_violation_count == 30
    assert a.reporting_violation_count == 2  # the 2018 overdue row is outside the window
    assert len(a.violations) == 25
    ends = [v.monitoring_period_end for v in a.violations]
    assert ends == sorted(ends, reverse=True)
    assert ends[0] == date(2025, 6, 30) and ends[-1] == ALPHA_DATES[5]
    assert all(v.violation_desc == NUMERIC for v in a.violations)
    for v in a.violations:
        assert isinstance(v, schemas.ViolationRecord)
        assert {"PARAMETER_DESC", "VIOLATION_DESC", "MONITORING_PERIOD_END_DATE"} <= set(v.source_fields)
        assert v.outfall == "001"
    assert by["VA0000002"].numeric_violation_count == 1  # 2020-12-31: window is data-relative
    assert by["VA0000002"].reporting_violation_count == 0
    assert [v.parameter for v in by["VA0000002"].violations] == ["Solids, total suspended"]


def test_matched_parameters_from_window(cands):
    by = {c.facility_id: c for c in cands}
    # Alpha's turbidity violation is before the window, so only TDS and pH match.
    assert _pm(by["VA0000001"].matched_parameters) == [
        ("conductivity", "Solids, total dissolved", True), ("ph", "pH", False)]
    assert _pm(by["VA0000002"].matched_parameters) == [("turbidity", "Solids, total suspended", True)]
    assert by["VA0000004"].matched_parameters == []


def test_anomalous_restricts_matches(G, reach_geoms):
    by = {c.facility_id: c for c in _find(G, reach_geoms, anomalous=["turbidity"])}
    assert by["VA0000001"].matched_parameters == []
    assert _pm(by["VA0000002"].matched_parameters) == [("turbidity", "Solids, total suspended", True)]
    assert by["VA0000003"].matched_parameters == []


def test_tri_matching(cands):
    by = {c.facility_id: c for c in cands}
    a = by["VA0000001"]
    assert (a.tri_match, a.tri_match_confidence) == ("frs_id", "high")
    assert [r.chemical for r in a.tri_releases] == ["Zinc compounds", "Lead"]
    e = by["VA0000003"]
    assert (e.tri_match, e.tri_match_confidence) == ("proximity_name", "low")
    assert [r.chemical for r in e.tri_releases] == ["Ammonia"]
    g8, g9 = by["VA0000008"], by["VA0000009"]
    assert g8.tri_match == "frs_id"
    # GAMMA WORKS's FRS id belongs to VA0000008, so it is never re-assigned to VA0000009.
    assert (g9.tri_match, g9.tri_match_confidence, g9.tri_releases) == (None, None, [])
    assert by["VA0000004"].tri_releases == [] and by["VA0000004"].tri_match is None


def test_identity_fields_come_from_input(cands):
    src = FACILITIES.set_index("npdes_id")
    for c in cands:
        assert c.facility_id in src.index
        assert c.name == src.loc[c.facility_id, "name"]
        expected_frs = src.loc[c.facility_id, "frs_id"]
        assert c.frs_id == (None if pd.isna(expected_frs) else expected_frs)


def test_wording_is_not_accusatory(cands):
    for c in cands:
        for text in (c.location_note, c.rank_basis):
            assert not any(w in text.lower() for w in BAD_WORDS)


def test_buffer_cutoff(G, reach_geoms):
    ids = [c.facility_id for c in _find(G, reach_geoms, buffer_m=2000.0)]
    assert "VA0000007" in ids  # 1.5 km from 101
    assert ids.index("VA0000007") == 4  # upstream 0.0, after the 0.6 km Gammas
    assert "VA0000006" not in ids
    small = [c.facility_id for c in _find(G, reach_geoms, buffer_m=250.0)]
    assert small == ["VA0000001", "VA0000004"]  # 0.2 km and 0.1 km


def test_lookback_years(G, reach_geoms):
    ids = [c.facility_id for c in _find(G, reach_geoms, lookback_years=1)]
    assert ids == ["VA0000001", "VA0000008", "VA0000009"]  # window starts 2024-06-30


def test_window_label_and_historical_count(cands):
    by = {c.facility_id: c for c in cands}
    a = by["VA0000001"]
    # Default reference is the latest period_end in the data (2025-06-30), 5 years back.
    assert a.violation_window_start == date(2020, 6, 30)
    assert a.violation_window_end == date(2025, 6, 30)
    assert a.violation_window_label == "reported numeric limit violations, 2020–2025"
    assert a.historical_numeric_violation_count == 1  # the 2019-12-31 turbidity row
    assert all(v.monitoring_period_end >= a.violation_window_start for v in a.violations)


def test_reference_date_fixes_the_window(G, reach_geoms):
    ref = date(2024, 12, 31)
    by = {c.facility_id: c for c in _find(G, reach_geoms, reference_date=ref)}
    a = by["VA0000001"]
    assert a.violation_window_end == ref and a.violation_window_start == date(2019, 12, 31)
    assert a.violation_window_label == "reported numeric limit violations, 2019–2024"
    assert all(v.monitoring_period_end <= ref for v in a.violations)  # later rows left out
    assert a.historical_numeric_violation_count == 0  # 2019-12-31 is now inside the window
    assert "VA0000011" not in by  # 2019-06-30 stays historical: never makes a permit a candidate


def test_historical_count_does_not_change_ranking(G, reach_geoms):
    ranked = [c.facility_id for c in _find(G, reach_geoms)]
    moved = [c.facility_id for c in _find(G, reach_geoms, reference_date=date(2025, 6, 30))]
    assert ranked == moved == EXPECTED_ORDER


def test_max_km_limits_trace(G, reach_geoms):
    # Trace of 3 km reaches 100 and 101 only (102/201 start at 3.0).
    ids = [c.facility_id for c in fac.find_candidate_facilities(
        G, START, 3.0, facilities=FACILITIES, outfalls=OUTFALLS, violations=VIOLATIONS, tri=TRI,
        reach_geoms=reach_geoms, permits=PERMITS)]
    assert ids == ["VA0000001", "VA0000002", "VA0000008", "VA0000009"]


def test_no_candidates(G, reach_geoms):
    empty = VIOLATIONS[VIOLATIONS["is_numeric"] == False]  # noqa: E712
    assert _find(G, reach_geoms, violations=empty) == []


# ---- candidates_geojson ----------------------------------------------------------------------
def test_candidates_geojson(cands):
    fc = fac.candidates_geojson(cands)
    assert isinstance(fc, schemas.FeatureCollection)
    assert fc.type == "FeatureCollection"
    assert len(fc.features) == len(cands)
    for feat, c in zip(fc.features, cands):
        assert feat["type"] == "Feature"
        assert feat["geometry"]["type"] == "Point"
        assert list(feat["geometry"]["coordinates"]) == pytest.approx([c.longitude, c.latitude])
        props = feat["properties"]
        assert "latitude" not in props and "longitude" not in props
        assert props["facility_id"] == c.facility_id and props["rank"] == c.rank
        expected = c.model_dump(mode="json")
        expected.pop("latitude")
        expected.pop("longitude")
        assert props == expected
    assert fac.candidates_geojson([]).features == []


# ---- real data -------------------------------------------------------------------------------
def test_real_reach_invariants():
    try:
        settings = get_settings()
    except ConfigError:
        pytest.skip("settings unavailable (.env incomplete)")
    paths = [settings.nhdplus_gpkg_path, settings.echo_npdes_dir / "ICIS_FACILITIES.csv",
             settings.echo_npdes_dir / "NPDES_PERM_FEATURE_COORDS.csv", settings.echo_effluent_dir,
             settings.tri_csv_path]
    if not all(p.exists() for p in paths):
        pytest.skip("real data files not present")

    from backend.app.data.echo import load_facilities
    from backend.app.data.nhd_graph import load_graph

    G = load_graph()
    out = fac.find_candidate_facilities(G, settings.reach_nhdplusid, 25)
    ids = set(load_facilities()["npdes_id"])
    for c in out:
        assert c.facility_id in ids
        assert c.numeric_violation_count >= 1
        assert c.location_type in ("outfall", "facility")
        assert c.distance_km_to_reach <= 1.0
        assert len(c.violations) <= 25
    assert [c.rank for c in out] == list(range(1, len(out) + 1))
    keys = [(c.upstream_distance_km, c.distance_km_to_reach) for c in out]
    assert keys == sorted(keys)


# ---- permit and facility type -----------------------------------------------------------------
def test_permit_types_from_icis(cands):
    by = {c.facility_id: c for c in cands}
    a, b, e, z, eta = by["VA0000001"], by["VA0000002"], by["VA0000003"], by["VA0000004"], by["VA0000005"]
    assert (a.permit_type, a.permit_type_code, a.facility_type, a.facility_type_indicator) == (
        "individual", "NPD", "Corporation", "NON-POTW")
    assert not a.small_domestic_general_permit and a.permit_note == "Holds an individual NPDES permit."
    assert (b.permit_type, b.general_permit_id, b.facility_type) == ("general", "VAG400000", "Privately Owned Facility")
    assert b.small_domestic_general_permit and "small domestic general permit" in b.permit_note
    assert b.general_permit_name == DOMESTIC_GP
    # A general permit that is not for domestic sewage is general, but not small domestic.
    assert e.permit_type == "general" and not e.small_domestic_general_permit
    assert e.permit_note.startswith("Covered by a general permit (VAG840000")
    # Codes other than individual/general keep the code and ECHO's description, with no permit_type.
    assert (z.permit_type, z.permit_type_code, z.permit_type_desc) == (None, "APR", "Associated Permit Record")
    assert eta.permit_type is None and eta.permit_type_code is None and "No permit type record" in eta.permit_note


def test_permit_type_does_not_change_ranking(G, reach_geoms):
    # Swap every permit type: the order and ranks must not move.
    swapped = PERMITS.assign(
        permit_type_code=PERMITS["permit_type_code"].map({"NPD": "GPC", "GPC": "NPD", "APR": "NPD"}),
        general_permit_name=DOMESTIC_GP,
    )
    for permits in (PERMITS, swapped, PERMITS.iloc[0:0]):
        out = _find(G, reach_geoms, permits=permits)
        assert [c.facility_id for c in out] == EXPECTED_ORDER
        assert [c.rank for c in out] == list(range(1, len(EXPECTED_ORDER) + 1))


def test_permit_note_is_non_accusatory(cands):
    for c in cands:
        assert not any(w in (c.permit_note or "").lower() for w in BAD_WORDS)


def test_real_permit_types_on_demo_reach():
    try:
        settings = get_settings()
    except ConfigError:
        pytest.skip("no .env")
    from backend.app.data.echo import load_permits
    from backend.app.data.nhd_graph import load_graph

    permits = load_permits()
    assert permits["npdes_id"].is_unique
    out = fac.find_candidate_facilities(load_graph(), settings.reach_nhdplusid, 50,
                                        reference_date=settings.scenario_timestamp.date())
    by = {c.facility_id: c for c in out}
    assert by["VA0052621"].permit_type == "individual" and not by["VA0052621"].small_domestic_general_permit
    for fid in ("VAG408560", "VAG408449", "VAG408158"):
        c = by[fid]
        assert c.permit_type == "general" and c.general_permit_id == "VAG400000"
        assert c.small_domestic_general_permit and "Domestic Sewage" in c.general_permit_name
    for c in out:
        assert c.facility_type is not None and c.facility_type_code is not None
