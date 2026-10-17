"""Candidate ECHO facilities near the river upstream of a reach, with reported violations and TRI context.

Candidates are permits with at least one reported numeric limit violation in the lookback window whose
outfall point (or facility point when no outfall coordinates exist) lies within a buffer of a traced
upstream reach. Ranking uses river distance only, never travel time. Every id and name comes from the
local ECHO frames. Distances are computed in DISTANCE_CRS (UTM 17N; error grows far from the zone).
"""
from __future__ import annotations

from collections.abc import Callable, Iterable
from datetime import date
from pathlib import Path
from typing import Any

import geopandas as gpd
import networkx as nx
import pandas as pd

from ..config import DISTANCE_CRS, SOURCE_CRS, ConfigError, get_settings
from ..schemas import (
    CandidateFacility,
    FeatureCollection,
    ParameterMatch,
    SensorParameter,
    ViolationRecord,
)
from .echo import load_facilities, load_outfalls, load_violations
from .nhd_graph import downstream_path, reach_geometries, trace_upstream, travel_time
from .tri import load_tri, match_tri

DEFAULT_BUFFER_M = 1000.0
DEFAULT_LOOKBACK_YEARS = 5
DEFAULT_ANOMALOUS: tuple[SensorParameter, ...] = ("conductivity", "turbidity", "ph")
MAX_VIOLATIONS_LISTED = 25

RANK_BASIS = "river distance upstream of the spill reach (km), then distance to the river (km); not travel time"
_OUTFALL_NOTE = "Reported outfall location (ECHO permitted feature) {}; not a surveyed discharge point."
_FACILITY_NOTE = (
    "Reported facility location (ECHO geocode); no outfall coordinates in local data, so this is less precise."
)


def _is_ph(p: str) -> bool:
    return p == "pH" or p.startswith("pH,")


# Sensor parameter -> (matcher, is_proxy, note). A str matcher is a case-insensitive substring.
PARAMETER_PROXIES: dict[SensorParameter, list[tuple[Callable[[str], bool] | str, bool, str]]] = {
    "conductivity": [
        ("conductance", False, "ECHO reports specific conductance directly."),
        (
            "Solids, total dissolved",
            True,
            "Total dissolved solids (TDS) is used as a proxy for specific conductance; it is an indirect indicator.",
        ),
    ],
    "turbidity": [
        ("turbidity", False, "ECHO reports turbidity directly."),
        (
            "Solids, total suspended",
            True,
            "Total suspended solids (TSS) is used as a proxy for turbidity; it is an indirect indicator.",
        ),
    ],
    "ph": [(_is_ph, False, "ECHO reports pH directly.")],
    "discharge": [],
}

_VIOLATION_FIELDS = {
    "parameter": "PARAMETER_DESC",
    "violation_desc": "VIOLATION_DESC",
    "period_end": "MONITORING_PERIOD_END_DATE",
    "exceedance_pct": "EXCEEDENCE_PCT",
    "status": "RNC_RESOLUTION_DESC",
    "outfall": "PERM_FEATURE_NMBR",
}


def _rule_matches(matcher: Callable[[str], bool] | str, parameter: str) -> bool:
    if isinstance(matcher, str):
        return matcher.lower() in parameter.lower()
    return bool(matcher(parameter))


def _opt_str(v: Any) -> str | None:
    if v is None or (not isinstance(v, str) and pd.isna(v)):
        return None
    s = str(v).strip()
    return s or None


def _opt_float(v: Any) -> float | None:
    if v is None:
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if pd.isna(f) else f


def match_parameters(parameters: Iterable[str], anomalous: Iterable[SensorParameter]) -> list[ParameterMatch]:
    """Map ECHO parameter names to the anomalous sensor parameters, labelling proxy matches."""
    params = sorted({p for p in parameters if isinstance(p, str) and p})
    found: dict[tuple[str, str], ParameterMatch] = {}
    for sensor in dict.fromkeys(anomalous):
        for matcher, is_proxy, note in PARAMETER_PROXIES.get(sensor, []):
            for p in params:
                if (sensor, p) not in found and _rule_matches(matcher, p):
                    found[(sensor, p)] = ParameterMatch(
                        sensor_parameter=sensor, echo_parameter=p, is_proxy=is_proxy, note=note
                    )
    return [found[k] for k in sorted(found)]


_POINT_COLUMNS = ["npdes_id", "name", "frs_id", "latitude", "longitude", "location_type", "outfall"]


def facility_points(facilities: pd.DataFrame, outfalls: pd.DataFrame) -> pd.DataFrame:
    """Outfall points per permit where any exist, otherwise the facility point."""
    fac = facilities[["npdes_id", "name", "frs_id", "latitude", "longitude"]].drop_duplicates("npdes_id")
    out = outfalls[["npdes_id", "outfall", "latitude", "longitude"]].merge(
        fac[["npdes_id", "name", "frs_id"]], on="npdes_id", how="inner"
    )
    out["location_type"] = "outfall"
    fac_only = fac[~fac["npdes_id"].isin(out["npdes_id"])].copy()
    fac_only["location_type"] = "facility"
    fac_only["outfall"] = None
    pts = pd.concat([out[_POINT_COLUMNS], fac_only[_POINT_COLUMNS]], ignore_index=True)
    pts["latitude"] = pts["latitude"].astype(float)
    pts["longitude"] = pts["longitude"].astype(float)
    pts["frs_id"] = [_opt_str(v) for v in pts["frs_id"]]
    pts["outfall"] = [_opt_str(v) for v in pts["outfall"]]
    return pts.astype({"frs_id": object, "outfall": object})


def _years_before(d: pd.Timestamp, years: int) -> pd.Timestamp:
    try:
        return d.replace(year=d.year - years)
    except ValueError:  # 29 February
        return d.replace(year=d.year - years, day=28)


def _window(
    violations: pd.DataFrame, lookback_years: int, reference_date: date | None
) -> tuple[pd.DataFrame, pd.DataFrame, pd.Timestamp | None, pd.Timestamp | None]:
    """Split rows into (in window, historical) for the lookback_years ending at reference_date.

    reference_date defaults to the latest period_end in the frame; either way the window is fixed by
    the scenario or the data, never by today's date. Rows after reference_date are left out.
    """
    v = violations.copy()
    v["end_ts"] = pd.to_datetime(v["period_end"], errors="coerce")
    end = pd.Timestamp(reference_date) if reference_date is not None else v["end_ts"].max()
    if pd.isna(end):
        return v.iloc[0:0], v.iloc[0:0], None, None
    start = _years_before(end, lookback_years)
    in_window = v[(v["end_ts"] >= start) & (v["end_ts"] <= end)]
    historical = v[v["end_ts"] < start]
    return in_window, historical, start, end


def _default_window_years() -> int:
    try:
        return get_settings().violation_window_years
    except ConfigError:
        return DEFAULT_LOOKBACK_YEARS


def _violation_record(row: Any) -> ViolationRecord:
    end = None if pd.isna(row.end_ts) else row.end_ts.date()
    values = {
        "parameter": _opt_str(row.parameter) or "",
        "violation_desc": _opt_str(row.violation_desc) or "",
        "period_end": end,
        "exceedance_pct": _opt_float(row.exceedance_pct),
        "status": _opt_str(row.status),
        "outfall": _opt_str(row.outfall),
    }
    fields = [
        col for key, col in _VIOLATION_FIELDS.items()
        if key in ("parameter", "violation_desc", "period_end") or values[key] is not None
    ]
    return ViolationRecord(
        parameter=values["parameter"],
        monitoring_period_end=end,
        violation_desc=values["violation_desc"],
        exceedance_pct=values["exceedance_pct"],
        status=values["status"],
        outfall=values["outfall"],
        source_fields=fields,
    )


def find_candidate_facilities(
    G: nx.DiGraph,
    start_id: int,
    max_km: float,
    buffer_m: float = DEFAULT_BUFFER_M,
    anomalous: Iterable[SensorParameter] | None = None,
    lookback_years: int | None = None,
    reference_date: date | None = None,
    gpkg_path: str | Path | None = None,
    facilities: pd.DataFrame | None = None,
    outfalls: pd.DataFrame | None = None,
    violations: pd.DataFrame | None = None,
    tri: pd.DataFrame | None = None,
    reach_geoms: gpd.GeoDataFrame | None = None,
) -> list[CandidateFacility]:
    """Permits with reported numeric violations near the river upstream of start_id, ranked by distance.

    The violation window is lookback_years (default VIOLATION_WINDOW_YEARS) ending at reference_date
    (pass the scenario date; defaults to the latest violation date in the data).
    """
    anomalous = list(DEFAULT_ANOMALOUS if anomalous is None else anomalous)
    if lookback_years is None:
        lookback_years = _default_window_years()
    start = int(start_id)

    # 1. Upstream trace and its geometry.
    trace = trace_upstream(G, start, max_km)
    by_id = {r.nhdplusid: r for r in trace}
    if reach_geoms is None:
        if gpkg_path is None:
            gpkg_path = get_settings().nhdplus_gpkg_path
        reach_geoms = reach_geometries(gpkg_path, by_id)
    reaches = reach_geoms[["nhdplusid", "geometry"]].copy()
    if reaches.crs is None:
        reaches = reaches.set_crs(SOURCE_CRS)
    reaches["nhdplusid"] = reaches["nhdplusid"].astype("int64")
    reaches = reaches[reaches["nhdplusid"].isin(by_id)].to_crs(DISTANCE_CRS)

    # 3-4. Violations window; only permits with a numeric violation in it can be candidates.
    if violations is None:
        violations = load_violations()
    win, hist, win_start, win_end = _window(violations, lookback_years, reference_date)
    numeric = win[win["is_numeric"].astype(bool)]
    reporting = win[win["is_reporting"].astype(bool)]
    historical = hist[hist["is_numeric"].astype(bool)]
    window_label = (
        f"reported numeric limit violations, {win_start.year}–{win_end.year}" if win_start is not None else ""
    )
    permits = set(numeric["npdes_id"])
    if reaches.empty or not permits:
        return []

    # 2. Points near the traced reaches.
    if facilities is None:
        facilities = load_facilities()
    if outfalls is None:
        outfalls = load_outfalls()
    pts = facility_points(facilities, outfalls)
    pts = pts[pts["npdes_id"].isin(permits)].reset_index(drop=True)
    if pts.empty:
        return []
    gpts = gpd.GeoDataFrame(
        pts, geometry=gpd.points_from_xy(pts["longitude"], pts["latitude"]), crs=SOURCE_CRS
    ).to_crs(DISTANCE_CRS)
    near = gpd.sjoin_nearest(
        gpts, reaches, how="inner", max_distance=buffer_m, distance_col="dist_m"
    )
    near = near[near["dist_m"] <= buffer_m]
    if near.empty:
        return []
    near = near.rename(columns={"nhdplusid": "reach_id"})
    near["reach_id"] = near["reach_id"].astype("int64")

    # 6. River distance from the top of the start reach to the downstream end of the nearest reach.
    def upstream_km(nid: int) -> float:
        if nid == start:
            return 0.0
        r = by_id[nid]
        return max(0.0, r.cumulative_km - r.length_km)

    near["upstream_km"] = near["reach_id"].map(upstream_km)
    # 5. Per permit, its nearest kept point (ties broken deterministically).
    near["outfall_key"] = near["outfall"].fillna("")
    near = near.sort_values(["npdes_id", "dist_m", "upstream_km", "reach_id", "outfall_key"])
    best = near.drop_duplicates("npdes_id", keep="first")

    if tri is None:
        tri = load_tri()
    echo_frs_ids = {s for s in (_opt_str(v) for v in facilities["frs_id"]) if s}
    fac_points = facilities.drop_duplicates("npdes_id").set_index("npdes_id")[["latitude", "longitude"]]

    candidates = []
    for row in best.itertuples(index=False):
        pid = row.npdes_id
        nid = int(row.reach_id)
        own = numeric[numeric["npdes_id"] == pid].copy()
        own["param_key"] = own["parameter"].fillna("").astype(str)
        own["outfall_key"] = own["outfall"].fillna("").astype(str)
        own = own.sort_values(["end_ts", "param_key", "outfall_key"], ascending=[False, True, True])
        records = [_violation_record(r) for r in own.head(MAX_VIOLATIONS_LISTED).itertuples(index=False)]

        # TRI matching uses the facility geocode (TRI points are facility locations), else the candidate point.
        if pid in fac_points.index:
            tri_lat, tri_lon = float(fac_points.at[pid, "latitude"]), float(fac_points.at[pid, "longitude"])
        else:
            tri_lat, tri_lon = float(row.latitude), float(row.longitude)
        frs_id, outfall = _opt_str(row.frs_id), _opt_str(row.outfall)
        releases, how, confidence = match_tri(frs_id, row.name, tri_lat, tri_lon, tri, echo_frs_ids)

        is_outfall = row.location_type == "outfall"
        candidates.append(
            CandidateFacility(
                facility_id=pid,
                name=row.name,
                frs_id=frs_id,
                latitude=float(row.latitude),
                longitude=float(row.longitude),
                location_type=row.location_type,
                location_note=_OUTFALL_NOTE.format(outfall) if is_outfall else _FACILITY_NOTE,
                distance_km_to_reach=float(row.dist_m) / 1000.0,
                nearest_reach_nhdplusid=nid,
                upstream_distance_km=float(row.upstream_km),
                travel_time=travel_time(G, downstream_path(G, nid, start)),
                numeric_violation_count=len(own),
                reporting_violation_count=int((reporting["npdes_id"] == pid).sum()),
                historical_numeric_violation_count=int((historical["npdes_id"] == pid).sum()),
                violation_window_start=win_start.date(),
                violation_window_end=win_end.date(),
                violation_window_label=window_label,
                violations=records,
                matched_parameters=match_parameters(own["parameter"].dropna(), anomalous),
                tri_releases=releases,
                tri_match=how,
                tri_match_confidence=confidence,
            )
        )

    # 11. Distance only; travel time never enters the ordering.
    candidates.sort(key=lambda c: (c.upstream_distance_km, c.distance_km_to_reach, c.facility_id))
    for i, c in enumerate(candidates, start=1):
        c.rank = i
        c.rank_basis = RANK_BASIS
    return candidates


def candidates_geojson(candidates: list[CandidateFacility]) -> FeatureCollection:
    """One Point feature per candidate at its reported location."""
    features = []
    for c in candidates:
        props = c.model_dump(mode="json")
        props.pop("latitude", None)
        props.pop("longitude", None)
        features.append(
            {
                "type": "Feature",
                "geometry": {"type": "Point", "coordinates": [c.longitude, c.latitude]},
                "properties": props,
            }
        )
    return FeatureCollection(features=features)
