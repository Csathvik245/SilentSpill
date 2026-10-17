"""Tests for backend.app.data.nhd_graph, written from the module contract on a synthetic network.

Synthetic network (arrows point downstream; length km / va ft/s):

    400 <-> 401 (cycle)          300 (1.0 / 1.0)
             |                   /            \\
             v              301 (2.0/1.0)   302 (0.5/1.0)     braid rejoining at 103
    202 (1.5 / no EROM)          \\            /
             |                    103 (1.0 / 1.5)
             v                         |
    201 (2.5 / va NULL)           102 (4.0 / -9998)
             \\                         |
              `------------>  101 (3.0 / 2.0, ftype 558)
                                       |
                    999 (no VAA) ->   100 (2.0 / 1.0)  <- start reach
                                       |
                                      900 (5.0 / 2.0)  outlet (downstream of start)

400, 401: 0.5 km each, 1.0 ft/s; 400 -> 401, 401 -> 400, 401 -> 202.
Flow rows with 0 (0 -> 103, 900 -> 0) and 999 -> 100 must not become edges.

Hand-computed cumulative_km tracing upstream from 100 (cum(u) = cum(d) + len(u), min over paths):
    100: 0.0   101: 3.0   201: 5.5   102: 7.0   202: 7.0   401: 7.5
    103: 8.0   400: 8.0   302: 8.5   300: 9.5 (via 302, not 10.5 via 301)   301: 10.0
"""
import os
import sqlite3

import networkx as nx
import pytest

from backend.app import schemas
from backend.app.config import FEET_TO_METERS, ConfigError, get_settings
from backend.app.data.nhd_graph import (
    ReachNotFoundError,
    build_graph,
    downstream_path,
    load_graph,
    trace_upstream,
    travel_time,
    velocity_mps,
)

START = 100

# id: (gnis_name, lengthkm, ftype, flowdir, hydroseq or None for no VAA row, va or "noerom", qa)
REACHES = {
    900: ("Main River", 5.0, 460, 1, 1.0, 2.0, 500.0),
    100: ("Main River", 2.0, 460, 1, 2.0, 1.0, 400.0),
    101: ("Main River", 3.0, 558, 1, 3.0, 2.0, 300.0),
    102: (None, 4.0, 460, 1, 4.0, -9998.0, 200.0),
    103: (None, 1.0, 460, 1, 5.0, 1.5, 150.0),
    301: (None, 2.0, 460, 1, 6.0, 1.0, 50.0),
    302: (None, 0.5, 460, 1, 7.0, 1.0, 50.0),
    300: (None, 1.0, 460, 1, 8.0, 1.0, 100.0),
    201: ("Trib Creek", 2.5, 460, 1, 9.0, None, 80.0),
    202: ("Trib Creek", 1.5, 460, 1, 10.0, "noerom", None),
    401: (None, 0.5, 460, 1, 11.0, 1.0, 10.0),
    400: (None, 0.5, 460, 1, 12.0, 1.0, 10.0),
    999: ("Ghost Run", 1.0, 460, 1, None, 1.0, 5.0),  # no VAA row -> excluded
}
FLOWS = [
    (900, 0), (100, 900), (101, 100), (102, 101), (103, 102), (0, 103),
    (301, 103), (302, 103), (300, 301), (300, 302),
    (201, 101), (202, 201), (401, 202), (400, 401), (401, 400),
    (999, 100),
]
NODES = {k for k, v in REACHES.items() if v[4] is not None}
EDGES = {(u, v) for u, v in FLOWS if u in NODES and v in NODES}

EXPECTED_CUM = {
    100: 0.0, 101: 3.0, 201: 5.5, 102: 7.0, 202: 7.0, 401: 7.5,
    103: 8.0, 400: 8.0, 302: 8.5, 300: 9.5, 301: 10.0,
}
EXPECTED_ORDER = [100, 101, 201, 102, 202, 401, 103, 400, 302, 300, 301]


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


@pytest.fixture
def gpkg(tmp_path):
    p = tmp_path / "net.gpkg"
    _write_db(p)
    return p


@pytest.fixture
def G(gpkg):
    return build_graph(gpkg)


def _ids(reaches):
    return [r.nhdplusid for r in reaches]


# ---- build_graph -----------------------------------------------------------------------------
def test_graph_nodes_are_vaa_flowlines_with_int_ids(G):
    assert isinstance(G, nx.DiGraph)
    assert set(G.nodes) == NODES
    assert 999 not in G
    assert all(type(n) is int for n in G.nodes)


def test_edges_point_downstream_and_drop_zero_and_non_nodes(G):
    assert set(G.edges) == EDGES
    assert G.has_edge(101, 100) and not G.has_edge(100, 101)
    assert G.has_edge(100, 900)
    assert 0 not in G
    assert all(type(u) is int and type(v) is int for u, v in G.edges)


def test_node_attributes(G):
    a = G.nodes[101]
    assert a["length_km"] == pytest.approx(3.0)
    assert a["velocity_fps"] == pytest.approx(2.0)
    assert a["qa_cfs"] == pytest.approx(300.0)
    assert a["gnis_name"] == "Main River"
    assert a["ftype"] == 558  # artificial path reaches are kept
    assert a["hydroseq"] == pytest.approx(3.0)
    assert G.nodes[103]["gnis_name"] is None


def test_missing_velocity_attrs_are_none(G):
    assert G.nodes[102]["velocity_fps"] is None  # va = -9998
    assert G.nodes[201]["velocity_fps"] is None  # va NULL
    assert G.nodes[202]["velocity_fps"] is None  # no EROM row
    assert G.nodes[202]["length_km"] == pytest.approx(1.5)


def test_velocity_mps(G):
    assert velocity_mps(G, 101) == pytest.approx(2.0 * FEET_TO_METERS)
    assert velocity_mps(G, 103) == pytest.approx(1.5 * 0.3048)
    assert velocity_mps(G, 102) is None
    assert velocity_mps(G, 202) is None


# ---- trace_upstream --------------------------------------------------------------------------
def test_trace_full_order_and_distances(G):
    out = trace_upstream(G, START, 100)
    assert all(isinstance(r, schemas.UpstreamReach) for r in out)
    assert _ids(out) == EXPECTED_ORDER  # cumulative asc, ties (7.0, 8.0) broken by id
    for r in out:
        assert r.cumulative_km == pytest.approx(EXPECTED_CUM[r.nhdplusid])
        assert r.length_km == pytest.approx(REACHES[r.nhdplusid][1])
    assert out[0].nhdplusid == START and out[0].cumulative_km == 0.0


def test_trace_braid_uses_shortest_distance(G):
    by_id = {r.nhdplusid: r for r in trace_upstream(G, START, 100)}
    assert by_id[300].cumulative_km == pytest.approx(9.5)  # via 302, not 10.5 via 301


def test_trace_item_fields(G):
    by_id = {r.nhdplusid: r for r in trace_upstream(G, START, 100)}
    assert by_id[101].velocity_mps == pytest.approx(0.6096)
    assert by_id[101].gnis_name == "Main River"
    assert by_id[201].gnis_name == "Trib Creek"
    assert by_id[102].velocity_mps is None
    assert by_id[201].velocity_mps is None
    assert by_id[202].velocity_mps is None


def test_trace_never_includes_downstream_or_non_nodes(G):
    ids = _ids(trace_upstream(G, START, 1000))
    assert 900 not in ids and 999 not in ids
    assert len(ids) == len(set(ids))


@pytest.mark.parametrize(
    "max_km, expected",
    [
        (3.0, [100, 101]),  # 201/102 start at 3.0, not < 3.0
        (3.5, [100, 101, 201, 102]),
        (7.0, [100, 101, 201, 102, 202]),  # 401 and 103 start at 7.0
        (8.0, [100, 101, 201, 102, 202, 401, 103, 400]),  # 302/301 start at 8.0
        (8.6, EXPECTED_ORDER),  # 300 starts at 8.5
    ],
)
def test_trace_max_km_cutoff_rule(G, max_km, expected):
    out = trace_upstream(G, START, max_km)
    assert _ids(out) == expected
    for r in out[1:]:
        assert r.cumulative_km - r.length_km < max_km


@pytest.mark.parametrize("max_km", [0, 0.0, -5])
def test_trace_nonpositive_max_km_returns_only_start(G, max_km):
    out = trace_upstream(G, START, max_km)
    assert _ids(out) == [START] and out[0].cumulative_km == 0.0


def test_trace_terminates_on_cycle(G):
    out = trace_upstream(G, 400, 1000)  # 400 and 401 flow into each other
    assert _ids(out) == [400, 401]
    assert out[1].cumulative_km == pytest.approx(0.5)


def test_trace_from_headwater_and_from_outlet(G):
    assert _ids(trace_upstream(G, 300, 50)) == [300]
    out = trace_upstream(G, 900, 1000)
    assert out[0].nhdplusid == 900
    assert {r.nhdplusid: r.cumulative_km for r in out}[100] == pytest.approx(2.0)
    assert {r.nhdplusid: r.cumulative_km for r in out}[300] == pytest.approx(11.5)


def test_trace_unknown_id(G):
    with pytest.raises(ReachNotFoundError):
        trace_upstream(G, 999, 10)
    with pytest.raises(KeyError):  # ReachNotFoundError is a KeyError
        trace_upstream(G, 123456, 10)


# ---- downstream_path -------------------------------------------------------------------------
def test_downstream_path_picks_shorter_braid(G):
    assert downstream_path(G, 300, 900) == [300, 302, 103, 102, 101, 100, 900]


def test_downstream_path_through_cycle_and_tributary(G):
    assert downstream_path(G, 400, 100) == [400, 401, 202, 201, 101, 100]


def test_downstream_path_same_id(G):
    assert downstream_path(G, 103, 103) == [103]


def test_downstream_path_no_path_and_unknown(G):
    with pytest.raises(ValueError):
        downstream_path(G, 100, 101)  # 101 is upstream
    with pytest.raises(ValueError):
        downstream_path(G, 201, 103)  # different branches
    with pytest.raises(ReachNotFoundError):
        downstream_path(G, 999, 100)
    with pytest.raises(ReachNotFoundError):
        downstream_path(G, 100, 424242)


# ---- travel_time -----------------------------------------------------------------------------
def test_travel_time_hours(G):
    # 2000 m / 0.3048 m/s + 5000 m / 0.6096 m/s = 14763.78 s = 4.10105 h
    tt = travel_time(G, [100, 900])
    assert isinstance(tt, schemas.TravelTime)
    assert tt.hours == pytest.approx(4.101049868766404, rel=1e-9)
    assert tt.reaches_missing_velocity == []
    # 1000/0.3048 + 500/0.3048 + 1000/0.4572 = 7108.49 s = 1.97458 h
    assert travel_time(G, [300, 302, 103]).hours == pytest.approx(1.9745795664430832, rel=1e-9)
    single = 3000 / (2.0 * FEET_TO_METERS) / 3600
    assert travel_time(G, [101]).hours == pytest.approx(single)


def test_travel_time_missing_velocity_never_guesses(G):
    tt = travel_time(G, [400, 401, 202, 201, 101, 100])
    assert tt.hours is None
    assert isinstance(tt.reason, str) and tt.reason.strip()
    assert tt.reaches_missing_velocity == [202, 201]
    tt2 = travel_time(G, [103, 102, 101])
    assert tt2.hours is None and tt2.reason and tt2.reaches_missing_velocity == [102]


def test_travel_time_complete_path_is_not_a_lower_bound(G):
    tt = travel_time(G, [100, 900])
    assert tt.is_lower_bound is False
    assert tt.travel_time_hours_min == pytest.approx(tt.hours)
    assert tt.km_with_velocity == pytest.approx(7.0) and tt.km_total == pytest.approx(7.0)
    assert tt.missing_reach_count == 0 and tt.reaches_missing_velocity == []
    assert tt.display == "4.1 h"


def test_travel_time_partial_is_lower_bound(G):
    # 202 (1.5 km) and 201 (2.5 km) lack velocity; the other 6.0 km are summed:
    # 500/0.3048 + 500/0.3048 + 3000/0.6096 + 2000/0.3048 = 14763.78 s
    tt = travel_time(G, [400, 401, 202, 201, 101, 100])
    assert tt.hours is None
    assert tt.is_lower_bound is True
    assert tt.travel_time_hours_min == pytest.approx(14763.779527559055 / 3600)
    assert tt.km_with_velocity == pytest.approx(6.0) and tt.km_total == pytest.approx(10.0)
    assert tt.missing_reach_count == 2 and tt.reaches_missing_velocity == [202, 201]
    assert "lower bound" in tt.reason
    assert tt.display == "≥ 4.1 h (velocity available for 6.0 of 10.0 km)"


def test_travel_time_all_missing_returns_none(G):
    tt = travel_time(G, [202, 201])
    assert tt.hours is None and tt.travel_time_hours_min is None
    assert tt.is_lower_bound is False
    assert tt.km_with_velocity == 0.0 and tt.km_total == pytest.approx(4.0)
    assert tt.missing_reach_count == 2
    assert tt.reason and "not available" in tt.reason
    assert "≥" not in tt.display and "unavailable" in tt.display


def test_lower_bound_display_rounds_down_and_never_says_estimate():
    tt = schemas.TravelTime(
        hours=None, travel_time_hours_min=2.46, is_lower_bound=True,
        km_with_velocity=46.1, km_total=47.3, missing_reach_count=1,
    )
    assert tt.display == "≥ 2.4 h (velocity available for 46.1 of 47.3 km)"
    assert "estimate" not in tt.display.lower()
    assert tt.model_dump()["display"] == tt.display  # serialized for the API


def test_travel_time_empty_and_unknown(G):
    assert travel_time(G, []).hours == 0.0
    with pytest.raises(ReachNotFoundError):
        travel_time(G, [100, 999])


# ---- load_graph caching ----------------------------------------------------------------------
def _same(a, b):
    return set(a.edges) == set(b.edges) and dict(a.nodes(data=True)) == dict(b.nodes(data=True))


def _set_length(path, rid, km):
    con = sqlite3.connect(path)
    con.execute("UPDATE nhdflowline_va SET lengthkm=? WHERE nhdplusid=?", (km, float(rid)))
    con.commit()
    con.close()


def test_load_graph_creates_cache_and_reuses_it(gpkg, tmp_path):
    cache = tmp_path / "cache" / "nested"
    G1 = load_graph(gpkg, cache_dir=cache)
    assert cache.is_dir() and any(p.is_file() for p in cache.iterdir())
    assert _same(G1, build_graph(gpkg))

    # Change content but keep size + mtime: a cached load must not see the change.
    st = os.stat(gpkg)
    _set_length(gpkg, 100, 9.0)
    os.utime(gpkg, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert os.stat(gpkg).st_size == st.st_size
    G2 = load_graph(gpkg, cache_dir=cache)
    assert _same(G1, G2)
    assert G2.nodes[100]["length_km"] == pytest.approx(2.0)

    # rebuild=True ignores the cache.
    G3 = load_graph(gpkg, cache_dir=cache, rebuild=True)
    assert G3.nodes[100]["length_km"] == pytest.approx(9.0)


def test_load_graph_rebuilds_when_mtime_changes(gpkg, tmp_path):
    cache = tmp_path / "cache"
    st = os.stat(gpkg)
    assert load_graph(gpkg, cache_dir=cache).nodes[100]["length_km"] == pytest.approx(2.0)
    _set_length(gpkg, 100, 9.0)
    os.utime(gpkg, ns=(st.st_atime_ns, st.st_mtime_ns))
    assert load_graph(gpkg, cache_dir=cache).nodes[100]["length_km"] == pytest.approx(2.0)
    os.utime(gpkg, (st.st_atime + 120, st.st_mtime + 120))
    G = load_graph(gpkg, cache_dir=cache)
    assert G.nodes[100]["length_km"] == pytest.approx(9.0)
    assert set(G.nodes) == NODES and set(G.edges) == EDGES


# ---- real data -------------------------------------------------------------------------------
REAL_REACH = 10000500083697


def test_real_reach_against_geopackage():
    try:
        settings = get_settings()
    except ConfigError:
        pytest.skip("settings unavailable (.env incomplete)")
    if not settings.nhdplus_gpkg_path.exists():
        pytest.skip("NHDPlus GeoPackage not present")

    G = load_graph()
    assert REAL_REACH in G
    assert G.nodes[REAL_REACH]["ftype"] == 558

    max_km = 50
    out = trace_upstream(G, REAL_REACH, max_km)
    assert out[0].nhdplusid == REAL_REACH and out[0].cumulative_km == 0.0
    assert len(out) > 100
    for r in out[1:]:
        assert r.cumulative_km - r.length_km < max_km
    assert [r.cumulative_km for r in out] == sorted(r.cumulative_km for r in out)

    # 3.16015443 km at 0.96159656 ft/s -> 2.9950 h
    expected = 3160.15443 / (0.96159656 * FEET_TO_METERS) / 3600
    assert travel_time(G, [REAL_REACH]).hours == pytest.approx(expected, rel=1e-3)
