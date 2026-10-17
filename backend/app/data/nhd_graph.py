"""NHDPlus HR river network as a networkx DiGraph, with upstream tracing and travel time.

Edges point downstream (`nhdplusflow_va.fromnhdpid -> tonhdpid`). Node ids are int NHDPlusIDs.
EROM `va` is read as ft/s and `qa` as cfs: a documented assumption, see docs/data_notes.md.
No ftype filter is applied (the demo reach is ftype 558, an artificial path).
"""
from __future__ import annotations

import heapq
import os
import pickle
import sqlite3
from collections.abc import Iterable
from pathlib import Path
from typing import TYPE_CHECKING

import networkx as nx

from ..config import FEET_TO_METERS, MIN_VALID_VELOCITY_FPS, SOURCE_CRS, get_settings

if TYPE_CHECKING:
    import geopandas as gpd
from ..schemas import TravelTime, UpstreamReach

_NODE_SQL = """
select f.nhdplusid, f.gnis_name, f.lengthkm, f.ftype, v.hydroseq, e.va, e.qa
from nhdflowline_va f
join nhdplusflowlinevaa_va v on v.nhdplusid = f.nhdplusid
left join nhdpluseromma_va e on e.nhdplusid = f.nhdplusid
"""
_EDGE_SQL = "select fromnhdpid, tonhdpid from nhdplusflow_va where fromnhdpid > 0 and tonhdpid > 0"


class ReachNotFoundError(KeyError):
    """Raised when an NHDPlusID is not a node in the graph."""


def _connect(path: Path) -> sqlite3.Connection:
    return sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)


def _opt_float(v: float | None) -> float | None:
    return None if v is None else float(v)


def build_graph(gpkg_path: str | Path) -> nx.DiGraph:
    """Build the downstream-directed reach graph from the GeoPackage tables (sqlite3 only)."""
    path = Path(gpkg_path)
    if not path.exists():
        raise FileNotFoundError(f"NHDPlus GeoPackage not found: {path}")
    G = nx.DiGraph()
    con = _connect(path)
    try:
        for nid, name, length, ftype, hydroseq, va, qa in con.execute(_NODE_SQL):
            velocity = float(va) if va is not None and va > MIN_VALID_VELOCITY_FPS else None
            G.add_node(
                int(nid),
                length_km=float(length or 0.0),
                velocity_fps=velocity,
                qa_cfs=_opt_float(qa),
                gnis_name=name or None,
                ftype=int(ftype) if ftype is not None else None,
                hydroseq=_opt_float(hydroseq),
            )
        nodes = G._node  # plain dict lookup is much faster than G.has_node on 400k edges
        G.add_edges_from(
            (f, t)
            for f, t in ((int(a), int(b)) for a, b in con.execute(_EDGE_SQL))
            if f in nodes and t in nodes
        )
    finally:
        con.close()
    return G


def load_graph(
    gpkg_path: str | Path | None = None,
    cache_dir: str | Path | None = None,
    rebuild: bool = False,
) -> nx.DiGraph:
    """Return the graph from a pickle cache keyed on the GeoPackage size and mtime, building if needed."""
    if gpkg_path is None or cache_dir is None:
        s = get_settings()
        gpkg_path = gpkg_path if gpkg_path is not None else s.nhdplus_gpkg_path
        cache_dir = cache_dir if cache_dir is not None else s.cache_dir
    gpkg = Path(gpkg_path)
    st = gpkg.stat()
    cache = Path(cache_dir)
    cache.mkdir(parents=True, exist_ok=True)
    cache_file = cache / f"nhd_graph_{gpkg.stem}_{st.st_size}_{st.st_mtime_ns}.pkl"

    if cache_file.exists() and not rebuild:
        try:
            with cache_file.open("rb") as fh:
                G = pickle.load(fh)
            if isinstance(G, nx.DiGraph):
                return G
        except Exception:  # corrupt or incompatible cache: rebuild below
            pass

    G = build_graph(gpkg)
    tmp = cache_file.with_suffix(".tmp")
    with tmp.open("wb") as fh:
        pickle.dump(G, fh, protocol=pickle.HIGHEST_PROTOCOL)
    os.replace(tmp, cache_file)
    return G


def _require(G: nx.DiGraph, nhdplusid: int) -> int:
    nid = int(nhdplusid)
    if nid not in G:
        raise ReachNotFoundError(nid)
    return nid


def velocity_mps(G: nx.DiGraph, nhdplusid: int) -> float | None:
    """EROM mean annual velocity in m/s, or None when EROM has no valid velocity."""
    fps = G.nodes[_require(G, nhdplusid)]["velocity_fps"]
    return None if fps is None else fps * FEET_TO_METERS


def trace_upstream(G: nx.DiGraph, nhdplusid: int, max_km: float) -> list[UpstreamReach]:
    """Reaches upstream of the start reach whose downstream end is within max_km, nearest first.

    cumulative_km is the shortest distance from the top of the start reach to the top of each reach.
    """
    start = _require(G, nhdplusid)
    dist: dict[int, float] = {start: 0.0}
    done: set[int] = set()
    heap: list[tuple[float, int]] = [(0.0, start)]
    while heap:
        d, cur = heapq.heappop(heap)
        if cur in done:
            continue
        done.add(cur)
        # Upstream neighbours' downstream ends sit at the top of cur, i.e. at distance d.
        if d >= max_km:
            continue
        for up in G.predecessors(cur):
            nd = d + G.nodes[up]["length_km"]
            if up not in done and nd < dist.get(up, float("inf")):
                dist[up] = nd
                heapq.heappush(heap, (nd, up))

    out = []
    for nid in sorted(done, key=lambda n: (dist[n], n)):
        attrs = G.nodes[nid]
        out.append(
            UpstreamReach(
                nhdplusid=nid,
                gnis_name=attrs["gnis_name"],
                length_km=attrs["length_km"],
                cumulative_km=dist[nid],
                velocity_mps=velocity_mps(G, nid),
            )
        )
    return out


def downstream_path(G: nx.DiGraph, from_id: int, to_id: int) -> list[int]:
    """Shortest downstream path (by reach length) from from_id to to_id, both ends included."""
    src, dst = _require(G, from_id), _require(G, to_id)
    if src == dst:
        return [src]
    try:
        path = nx.dijkstra_path(G, src, dst, weight=lambda u, v, _: G.nodes[v]["length_km"])
    except nx.NetworkXNoPath:
        raise ValueError(f"no downstream path from {src} to {dst}") from None
    return [int(n) for n in path]


def travel_time(G: nx.DiGraph, path: list[int]) -> TravelTime:
    """Travel time in hours over every reach in path (each reach's full length is counted).

    If some reaches lack velocity, hours is None and travel_time_hours_min is the time over the
    reaches that have one: a lower bound. If none have velocity, both are None. Never guesses.
    """
    ids = [_require(G, n) for n in path]
    km_total = sum(G.nodes[n]["length_km"] for n in ids)
    missing = [n for n in ids if G.nodes[n]["velocity_fps"] is None]
    have = [n for n in ids if G.nodes[n]["velocity_fps"] is not None]
    km_with = sum(G.nodes[n]["length_km"] for n in have)
    seconds = sum(G.nodes[n]["length_km"] * 1000.0 / velocity_mps(G, n) for n in have)
    common = dict(
        km_with_velocity=km_with,
        km_total=km_total,
        missing_reach_count=len(missing),
        reaches_missing_velocity=missing,
    )
    if not missing:
        return TravelTime(hours=seconds / 3600.0, travel_time_hours_min=seconds / 3600.0, **common)

    n, total = len(missing), len(ids)
    gap = (
        f"{n} of {total} reach{'es' if total != 1 else ''} on the path "
        f"({km_total - km_with:.1f} of {km_total:.1f} km) "
        f"{'have' if n != 1 else 'has'} no EROM velocity"
    )
    if not have:
        return TravelTime(hours=None, reason=f"{gap}; travel time not available.", **common)
    return TravelTime(
        hours=None,
        travel_time_hours_min=seconds / 3600.0,
        is_lower_bound=True,
        reason=f"{gap}; time over those reaches is not counted, so this is a lower bound.",
        **common,
    )


_GEOM_CHUNK = 500


def reach_geometries(gpkg_path: str | Path, nhdplusids: Iterable[int]) -> gpd.GeoDataFrame:
    """Flowline geometries for the given reaches in SOURCE_CRS. Unknown ids are simply absent."""
    import geopandas as gpd  # imported lazily so the graph code does not pay for it
    import pandas as pd
    import pyogrio

    ids = sorted({int(n) for n in nhdplusids})
    nid_col: list[int] = []
    geoms: list = []
    crs = None
    for i in range(0, len(ids), _GEOM_CHUNK):
        chunk = ids[i : i + _GEOM_CHUNK]
        df = pyogrio.read_dataframe(
            str(gpkg_path),
            layer="nhdflowline_va",
            columns=["nhdplusid"],
            where=f"nhdplusid IN ({','.join(str(n) for n in chunk)})",
        )
        crs = crs or df.crs
        nid_col.extend(int(v) for v in df["nhdplusid"])
        geoms.extend(df.geometry)
    out = gpd.GeoDataFrame(
        {"nhdplusid": pd.Series(nid_col, dtype="int64")},
        geometry=gpd.GeoSeries(geoms, crs=crs or SOURCE_CRS),
    )
    out = out.drop_duplicates("nhdplusid").reset_index(drop=True)
    return out if out.crs == SOURCE_CRS else out.to_crs(SOURCE_CRS)
