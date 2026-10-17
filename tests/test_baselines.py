import json

import pandas as pd
import pytest

from backend.app.config import ConfigError, get_settings
from backend.app.data import baselines, noaa, nwis


@pytest.fixture(scope="module")
def snap():
    return baselines.load_snapshot()


def test_snapshot_loads_offline(snap, monkeypatch):
    # The committed snapshot must load with the network unavailable.
    import requests

    def no_network(*a, **k):
        raise AssertionError("network used")

    monkeypatch.setattr(requests.Session, "request", no_network)
    again = baselines.load_snapshot()
    assert len(again.sensors) == len(snap.sensors)


def test_snapshot_window_and_grid(snap):
    assert snap.manifest["site_id"] == "01634000"
    assert snap.manifest["station_id"] == "GHCND:USW00013731"
    idx = snap.sensors.index
    assert str(idx.tz) == "UTC"
    assert idx[0] == pd.Timestamp("2026-09-16T00:00Z") and idx[-1] == pd.Timestamp("2026-10-14T00:00Z")
    assert (idx[1:] - idx[:-1] == nwis.EXPECTED_INTERVAL).all()
    assert len(idx) == 2689


def test_snapshot_has_all_parameters_with_gaps_kept(snap):
    for name in ("conductivity", "turbidity", "ph", "discharge"):
        assert name in snap.sensors and f"{name}_qualifiers" in snap.sensors
        assert snap.sensors[name].notna().sum() > 1500
    # The turbidity sensor outage stays missing; nothing is filled in.
    outage = snap.sensors.loc["2026-09-25T00:00Z":"2026-09-29T00:00Z", "turbidity"]
    assert outage.isna().all()
    assert (snap.sensors.loc[snap.sensors["turbidity"].isna(), "turbidity_qualifiers"] == "").all()


def test_snapshot_rain_days_missing_stay_nan(snap):
    d = snap.daily_prcp
    assert len(d) == 29
    assert d["prcp_inches"].notna().sum() == 11
    assert not (d["prcp_inches"] == 0).any()  # gaps are never turned into zeros
    assert d["prcp_inches"].max() == pytest.approx(0.95)


def test_write_snapshot_round_trip(tmp_path, snap):
    src = tmp_path / "src"
    src.mkdir()
    nwis_csv = nwis.save_raw(snap.nwis_raw.head(200), src / "n.csv")
    noaa_csv = noaa.save_daily(snap.daily_prcp, src / "p.csv")
    manifest = dict(snap.manifest, start="2026-09-16T00:00:00+00:00", end="2026-09-16T12:00:00+00:00")
    out = baselines.write_snapshot(nwis_csv, noaa_csv, manifest, snapshot_dir=tmp_path / "snap")
    loaded = baselines.load_snapshot(out)
    assert json.loads((out / baselines.MANIFEST).read_text())["nwis_file"] == "nwis.csv"
    assert len(loaded.sensors) == 49  # 12 h of 15-min slots, both ends included
    pd.testing.assert_frame_equal(loaded.daily_prcp, noaa.load_daily(noaa_csv))


def test_missing_snapshot_explains_how_to_fetch(tmp_path):
    with pytest.raises(FileNotFoundError, match="fetch_baselines.py"):
        baselines.load_snapshot(tmp_path)


def test_cache_paths():
    from datetime import date
    from pathlib import Path

    n, p = baselines.cache_paths(Path("c"), "01634000", "GHCND:USW00013731", date(2026, 9, 16), date(2026, 10, 14))
    assert n == Path("c/nwis/01634000_20260916_20261014.csv")
    assert p == Path("c/noaa/USW00013731_20260916_20261014.csv")


def test_no_secret_in_committed_snapshot():
    try:
        token = get_settings().noaa_cdo_token
    except ConfigError:
        pytest.skip("no .env")
    if token is None:
        pytest.skip("no token configured")
    secret = token.get_secret_value()
    for f in baselines.SNAPSHOT_DIR.iterdir():
        assert secret not in f.read_text(encoding="utf-8", errors="ignore")
