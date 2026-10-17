"""Fetch the gauge baseline (USGS) and daily rain (NOAA CDO) into the local cache.

This is the only place the pipeline touches the network. Everything else reads cache or the snapshot.

Usage:
  python scripts/fetch_baselines.py [--start 2026-09-16] [--end 2026-10-14] [--snapshot] [--from-cache]
    --snapshot    also copy the pull into backend/fixtures/baseline/ (committable, used by the demo)
    --from-cache  skip the network and build the snapshot from cache files already on disk
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from backend.app.config import ConfigError, load_settings  # noqa: E402
from backend.app.data import noaa, nwis  # noqa: E402
from backend.app.data.baselines import SNAPSHOT_DIR, cache_paths, write_snapshot  # noqa: E402


def _default_window() -> tuple[date, date]:
    end = datetime.now(timezone.utc).date() - timedelta(days=3)  # NOAA GHCND lags a few days
    return end - timedelta(days=28), end


def _print_nwis(raw) -> None:
    wide = nwis.to_wide(raw)
    for name, info in nwis.summarize(raw, wide).items():
        print(f"  {name}: {info}")


def _print_noaa(daily) -> None:
    for key, value in noaa.summarize(daily).items():
        print(f"  {key}: {value}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start", type=date.fromisoformat)
    ap.add_argument("--end", type=date.fromisoformat)
    ap.add_argument("--snapshot", action="store_true")
    ap.add_argument("--from-cache", action="store_true")
    args = ap.parse_args()

    try:
        s = load_settings()
    except ConfigError as exc:
        print(exc)
        return 2
    start, end = _default_window()
    start, end = args.start or start, args.end or end
    start_dt = datetime.combine(start, time(0), timezone.utc)
    end_dt = datetime.combine(end, time(0), timezone.utc)
    nwis_csv, noaa_csv = cache_paths(s.cache_dir, s.nwis_site_id, s.noaa_station_id, start, end)
    print(f"Window: {start_dt.isoformat()} to {end_dt.isoformat()} (UTC)")

    if not args.from_cache:
        print(f"Fetching USGS {s.nwis_site_id} ...")
        raw = nwis.fetch_nwis(s.nwis_site_id, start_dt, end_dt)
        nwis_csv.parent.mkdir(parents=True, exist_ok=True)
        nwis.save_raw(raw, nwis_csv)

        if s.noaa_cdo_token is None:
            print("NOAA_CDO_TOKEN is not set in .env; skipping rain.")
            return 2
        print(f"Fetching NOAA {s.noaa_station_id} ...")
        daily = noaa.fetch_daily_prcp(
            s.noaa_station_id, start, end, token=s.noaa_cdo_token.get_secret_value()
        )
        noaa_csv.parent.mkdir(parents=True, exist_ok=True)
        noaa.save_daily(daily, noaa_csv)

    for p in (nwis_csv, noaa_csv):
        if not p.exists():
            print(f"Missing cache file {p}; run without --from-cache.")
            return 1
    raw, daily = nwis.load_raw(nwis_csv), noaa.load_daily(noaa_csv)
    print(f"USGS cache: {nwis_csv}")
    _print_nwis(raw)
    print(f"NOAA cache: {noaa_csv}")
    _print_noaa(daily)

    if args.snapshot:
        sources = sorted(set(raw["source"])) if "source" in raw else []
        out = write_snapshot(
            nwis_csv,
            noaa_csv,
            {
                "site_id": s.nwis_site_id,
                "station_id": s.noaa_station_id,
                "start": start_dt.isoformat(),
                "end": end_dt.isoformat(),
                "nwis_parameters": nwis.NWIS_PARAMETERS,
                "nwis_sources": sources,
                "prcp_resolution": "daily",
                "created_from": [nwis_csv.name, noaa_csv.name],
            },
        )
        print(f"Snapshot written to {out.relative_to(SNAPSHOT_DIR.parents[2])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
