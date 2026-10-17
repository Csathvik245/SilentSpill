"""Read-only inspection of the local datasets. Prints findings; writes nothing.

Usage: python scripts/inspect_data.py [--online]
  --online  also queries the public USGS Water Data API for the gauge's parameter codes.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from collections import deque
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import (  # noqa: E402
    DISTANCE_CRS,
    FEET_TO_METERS,
    SOURCE_CRS,
    ConfigError,
    load_settings,
)

GAUGE_LAT, GAUGE_LON = 38.9768, -78.3361  # user-confirmed gauge location (NAD83)


def hr(title: str) -> None:
    print(f"\n=== {title} ===")


def inspect_nhd(s) -> None:
    hr("NHDPlus GeoPackage")
    con = sqlite3.connect(f"file:{s.nhdplus_gpkg_path}?mode=ro", uri=True)
    q = lambda sql, *a: con.execute(sql, a).fetchall()  # noqa: E731
    for name, kind, srs in q("select table_name, data_type, srs_id from gpkg_contents"):
        n = q(f'select count(*) from "{name}"')[0][0]
        print(f"  {name}: {kind}, {n} rows, srs={srs}")
    for t in ("nhdflowline_va", "nhdplusflowlinevaa_va", "nhdpluseromma_va", "nhdplusflow_va"):
        cols = [r[1] for r in q(f'pragma table_info("{t}")')]
        print(f"  columns {t}: {cols}")

    rid = s.reach_nhdplusid
    row = q(
        "select f.gnis_name, f.lengthkm, f.ftype, v.streamorde, v.totdasqkm, e.qa, e.va "
        "from nhdflowline_va f join nhdplusflowlinevaa_va v using(nhdplusid) "
        "join nhdpluseromma_va e using(nhdplusid) where f.nhdplusid = ?",
        rid,
    )
    if not row:
        print(f"  !! REACH_COMID {rid} NOT FOUND in the flowline/VAA/EROM join")
        return
    name, length, ftype, order, area, qa, va = row[0]
    print(f"  reach {rid}: {name}, {length:.2f} km, ftype={ftype}, order={order}, "
          f"area={area:.0f} km2, qa={qa:.1f} cfs, va={va:.3f} ft/s = {va * FEET_TO_METERS:.3f} m/s")

    edges = pd.read_sql(
        "select fromnhdpid f, tonhdpid t from nhdplusflow_va where fromnhdpid > 0 and tonhdpid > 0",
        con,
    )
    hydro = dict(q("select nhdplusid, hydroseq from nhdplusflowlinevaa_va"))
    hs = pd.DataFrame({"f": edges["f"].map(hydro), "t": edges["t"].map(hydro)})
    boundary = hs.isna().any(axis=1)
    ok = (hs[~boundary]["t"] < hs[~boundary]["f"]).mean()
    print(f"  edge direction check: hydroseq(to) < hydroseq(from) on {ok:.1%} of {(~boundary).sum()} "
          "in-state edges (NHDPlus hydroseq grows upstream, so from->to flows downstream)")
    print(f"  {boundary.sum()} edges point to reaches outside the Virginia clip and are dropped")

    upstream: dict[int, list[int]] = {}
    for f, t in zip(edges["f"].astype("int64"), edges["t"].astype("int64")):
        upstream.setdefault(int(t), []).append(int(f))
    lengths = dict(q("select nhdplusid, lengthkm from nhdflowline_va"))
    seen = {rid: 0.0}
    queue = deque([rid])
    while queue:
        cur = queue.popleft()
        for up in upstream.get(cur, []):
            if up not in seen:
                seen[up] = seen[cur] + lengths.get(float(up), 0.0)
                queue.append(up)
    within = sum(1 for d in seen.values() if d <= 50)
    print(f"  upstream trace from reach: {len(seen) - 1} reaches total, {within - 1} within 50 km, "
          f"max distance {max(seen.values()):.0f} km")

    missing = q("select sum(va <= 0), count(*) from nhdpluseromma_va")[0]
    print(f"  EROM velocity missing (va <= 0): {missing[0]} of {missing[1]} ({missing[0] / missing[1]:.0%})")

    import geopandas as gpd
    import pyogrio

    d = 0.05
    flow = pyogrio.read_dataframe(
        s.nhdplus_gpkg_path,
        layer="nhdflowline_va",
        bbox=(GAUGE_LON - d, GAUGE_LAT - d, GAUGE_LON + d, GAUGE_LAT + d),
        columns=["nhdplusid", "gnis_name", "ftype"],
    ).to_crs(DISTANCE_CRS)
    pt = gpd.GeoSeries(gpd.points_from_xy([GAUGE_LON], [GAUGE_LAT]), crs=SOURCE_CRS).to_crs(DISTANCE_CRS)[0]
    flow["dist_m"] = flow.geometry.distance(pt)
    near = flow.nsmallest(3, "dist_m")
    print("  nearest flowlines to gauge (spatial link):")
    for _, r in near.iterrows():
        mark = "  <-- REACH_COMID" if int(r.nhdplusid) == rid else ""
        print(f"    {int(r.nhdplusid)} {r.gnis_name} ftype={r.ftype} {r.dist_m:.0f} m{mark}")


def inspect_echo(s) -> None:
    hr("ECHO")
    fac = pd.read_csv(s.echo_npdes_dir / "ICIS_FACILITIES.csv", dtype=str, encoding="latin-1", low_memory=False)
    va = fac[fac.STATE_CODE == "VA"]
    lat = pd.to_numeric(va.GEOCODE_LATITUDE, errors="coerce")
    lon = pd.to_numeric(va.GEOCODE_LONGITUDE, errors="coerce")
    bad = ((lat < 36) | (lat > 40) | (lon < -84) | (lon > -75)).sum()
    print(f"  ICIS_FACILITIES: {len(va)} VA permits, {(lat.notna() & lon.notna()).sum()} with coords, "
          f"{bad} outside the VA box (drop)")
    print("  facility coords are FACILITY points (GEOCODE_LATITUDE/LONGITUDE), not outfalls")

    co = pd.read_csv(s.echo_npdes_dir / "NPDES_PERM_FEATURE_COORDS.csv", dtype=str, low_memory=False)
    co = co[co.EXTERNAL_PERMIT_NMBR.str.startswith("VA", na=False)]
    print(f"  NPDES_PERM_FEATURE_COORDS: {len(co)} VA outfall points for {co.EXTERNAL_PERMIT_NMBR.nunique()} permits")

    f = next(s.echo_effluent_dir.glob("*.csv"))
    eff = pd.read_csv(f, dtype=str, low_memory=False)
    print(f"  {f.name}: {len(eff)} rows, {eff.NPDES_ID.nunique()} permits")
    print(f"  date range: {pd.to_datetime(eff.MONITORING_PERIOD_END_DATE).min().date()} to "
          f"{pd.to_datetime(eff.MONITORING_PERIOD_END_DATE).max().date()}")
    top = eff.VIOLATION_DESC.value_counts().head(4)
    print("  VIOLATION_DESC top:", top.to_dict())
    numeric = eff.VIOLATION_DESC.str.contains("Numeric", case=False, na=False)
    print(f"  numeric limit violations: {numeric.sum()} ({numeric.mean():.0%}); the rest are mostly "
          "overdue-report violations and are excluded from ranking")
    pairs = eff[["NPDES_ID", "PERM_FEATURE_NMBR"]].dropna().drop_duplicates()
    m = pairs.merge(co, left_on=["NPDES_ID", "PERM_FEATURE_NMBR"],
                    right_on=["EXTERNAL_PERMIT_NMBR", "PERM_FEATURE_NMBR"])
    print(f"  (permit, outfall) pairs with violations: {len(pairs)}, with outfall coords: {len(m)}")
    for key in ("conduct", "turbid", "Solids, total dissolved", "Solids, total suspended", "pH"):
        n = eff.PARAMETER_DESC.str.contains(key, case=False, na=False, regex=False).sum()
        print(f"  PARAMETER_DESC contains '{key}': {n}")
    print("  violations are 'reported' per ECHO and are not findings of fact")


def inspect_tri(s) -> None:
    hr("TRI")
    t = pd.read_csv(s.tri_csv_path, dtype=str, encoding="latin-1", low_memory=False)
    t.columns = [c.split(". ", 1)[1] if ". " in c else c for c in t.columns]
    v = t[t["ST"] == "VA"]
    print(f"  {len(t)} rows nationally, {len(v)} VA rows, {v['TRIFD'].nunique()} facilities, "
          f"years={sorted(v['YEAR'].unique())}")
    print(f"  units: {v['UNIT OF MEASURE'].value_counts().to_dict()}; datum column filled: "
          f"{v['HORIZONTAL DATUM'].notna().sum()} rows")
    w = pd.to_numeric(v["5.3 - WATER"], errors="coerce")
    print(f"  releases to water > 0: {(w > 0).sum()} rows from {v[w > 0]['TRIFD'].nunique()} facilities")
    fac = pd.read_csv(s.echo_npdes_dir / "ICIS_FACILITIES.csv", dtype=str, encoding="latin-1", low_memory=False)
    uin = set(fac[fac.STATE_CODE == "VA"].FACILITY_UIN.dropna())
    frs = set(v["FRS ID"].dropna())
    print(f"  TRI FRS ID -> ICIS FACILITY_UIN match: {len(frs & uin)} of {len(frs)} VA facilities")
    print("  TRI is annual self-reported data; it cannot match a live plume")


def inspect_nwis() -> None:
    hr("NWIS (online)")
    import requests

    from backend.app.config import load_settings as ls

    site = ls().nwis_site_id
    url = "https://api.waterdata.usgs.gov/ogcapi/v1/collections/time-series-metadata/items"
    try:
        r = requests.get(url, params={"monitoring_location_id": f"USGS-{site}", "f": "json", "limit": 200}, timeout=30)
        r.raise_for_status()
        rows = {}
        for ft in r.json().get("features", []):
            p = ft["properties"]
            rows[p.get("parameter_code")] = p.get("parameter_name") or p.get("parameter_description")
        for code, label in sorted(rows.items(), key=lambda kv: str(kv[0])):
            print(f"  {code}: {label}")
        for need in ("00095", "63680", "00400", "00060"):
            print(f"  {'OK     ' if need in rows else 'MISSING'} {need}")
    except Exception as exc:  # network or schema surprise: report, do not guess
        print(f"  could not read gauge metadata: {type(exc).__name__}: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--online", action="store_true")
    args = ap.parse_args()
    try:
        s = load_settings()
    except ConfigError as exc:
        print(exc)
        return 2
    print("Settings (secrets masked):")
    for k, v in s.public_dict().items():
        print(f"  {k} = {v}")
    inspect_nhd(s)
    inspect_echo(s)
    inspect_tri(s)
    if args.online:
        inspect_nwis()
    return 0


if __name__ == "__main__":
    sys.exit(main())
