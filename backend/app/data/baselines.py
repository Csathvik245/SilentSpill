"""Offline baseline inputs for the pipeline: gauge series and daily rain, read only from local files.

Network fetching happens only in scripts/fetch_baselines.py. The demo reads the committed snapshot in
backend/fixtures/baseline/ so it replays identically on any machine with no network.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path
from typing import Any

import pandas as pd

from ..config import PROJECT_ROOT
from . import noaa, nwis

SNAPSHOT_DIR = PROJECT_ROOT / "backend" / "fixtures" / "baseline"
MANIFEST = "manifest.json"


@dataclass(frozen=True)
class Baseline:
    manifest: dict[str, Any]
    nwis_raw: pd.DataFrame  # every series, long format, qualifiers kept
    sensors: pd.DataFrame  # primary series on a regular 15-min UTC grid; gaps stay NaN
    daily_prcp: pd.DataFrame  # one row per day; days with no reading stay NaN

    @property
    def start(self) -> datetime:
        return pd.Timestamp(self.manifest["start"]).to_pydatetime()

    @property
    def end(self) -> datetime:
        return pd.Timestamp(self.manifest["end"]).to_pydatetime()


def cache_paths(cache_dir: Path, site_id: str, station_id: str, start: date, end: date) -> tuple[Path, Path]:
    """Cache file names for one pull (the GHCND: prefix is dropped from the file name only)."""
    span = f"{start:%Y%m%d}_{end:%Y%m%d}"
    station = station_id.split(":", 1)[-1]
    return cache_dir / "nwis" / f"{site_id}_{span}.csv", cache_dir / "noaa" / f"{station}_{span}.csv"


def write_snapshot(
    nwis_csv: Path,
    noaa_csv: Path,
    manifest: dict[str, Any],
    snapshot_dir: Path = SNAPSHOT_DIR,
) -> Path:
    """Copy one pull into the snapshot folder with a manifest describing it."""
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    nwis.save_raw(nwis.load_raw(nwis_csv), snapshot_dir / "nwis.csv")
    noaa.save_daily(noaa.load_daily(noaa_csv), snapshot_dir / "noaa_prcp.csv")
    manifest = {**manifest, "nwis_file": "nwis.csv", "noaa_file": "noaa_prcp.csv"}
    (snapshot_dir / MANIFEST).write_text(json.dumps(manifest, indent=2, default=str), encoding="utf-8")
    return snapshot_dir


def load_snapshot(snapshot_dir: Path = SNAPSHOT_DIR) -> Baseline:
    """Read the committed baseline snapshot. Never touches the network."""
    path = snapshot_dir / MANIFEST
    if not path.exists():
        raise FileNotFoundError(
            f"No baseline snapshot at {snapshot_dir}. Run: python scripts/fetch_baselines.py --snapshot"
        )
    manifest = json.loads(path.read_text(encoding="utf-8"))
    raw = nwis.load_raw(snapshot_dir / manifest["nwis_file"])
    sensors = nwis.to_wide(raw, start=manifest["start"], end=manifest["end"])
    daily = noaa.load_daily(snapshot_dir / manifest["noaa_file"])
    return Baseline(manifest=manifest, nwis_raw=raw, sensors=sensors, daily_prcp=daily)
