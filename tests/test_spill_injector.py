import math
from datetime import timedelta
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

from backend.app.config import ConfigError, get_settings
from backend.app.data import baselines
from backend.app.simulation.spill_injector import (
    InjectionResult,
    SpillParams,
    inject_spill,
    pulse_shape,
    spill_params_from_settings,
)

PARAMS = ("conductivity", "ph", "turbidity")
ORIGINAL = ["conductivity", "turbidity", "ph", "discharge",
            "conductivity_qualifiers", "turbidity_qualifiers", "ph_qualifiers", "discharge_qualifiers"]


def make_frame(n: int = 192, seed: int | None = None) -> pd.DataFrame:
    """Regular 15-min UTC grid. seed=None -> constant values (hand-computable); else seeded noise."""
    idx = pd.date_range("2026-03-01", periods=n, freq="15min", tz="UTC")
    if seed is None:
        cols = {"conductivity": 400.0, "turbidity": 1.0, "ph": 8.0, "discharge": 100.0}
        df = pd.DataFrame({k: np.full(n, v) for k, v in cols.items()}, index=idx)
    else:
        rng = np.random.default_rng(seed)
        df = pd.DataFrame({
            "conductivity": 400 + rng.normal(0, 1, n),
            "turbidity": 1.5 + rng.normal(0, 0.05, n),
            "ph": 8.1 + rng.normal(0, 0.005, n),
            "discharge": 100 + rng.normal(0, 2, n),
        }, index=idx)
    for c in ("conductivity", "turbidity", "ph", "discharge"):
        df[f"{c}_qualifiers"] = "A"
    return df


def params_at(df: pd.DataFrame, slot: int, **kw) -> SpillParams:
    return SpillParams(start=df.index[slot].to_pydatetime(), **kw)


# ---- pulse_shape -----------------------------------------------------------------------------
def test_pulse_shape_values():
    t = np.array([-30.0, -15.0, 0.0, 15.0, 30.0, 45.0, 45 + 360.0, 45 + 720.0])
    got = np.asarray(pulse_shape(t, 45.0, 6.0), dtype=float)
    want = [0, 0, 0, 1 / 3, 2 / 3, 1, math.exp(-1), math.exp(-2)]
    np.testing.assert_allclose(got, want, rtol=1e-12, atol=1e-12)


def test_pulse_shape_scalar_and_instant_peak():
    assert float(np.asarray(pulse_shape(15.0, 45.0, 6.0))) == pytest.approx(1 / 3)
    got = np.asarray(pulse_shape(np.array([-15.0, 0.0, 360.0]), 0.0, 6.0), dtype=float)
    np.testing.assert_allclose(got, [0.0, 1.0, math.exp(-1)])


# ---- inject_spill: hand-computed values ------------------------------------------------------
def test_noise_free_values_match_hand_computation():
    df = make_frame()
    res = inject_spill(df, params_at(df, 8, noise_fraction=0.0, ph_delta=-0.6))
    assert isinstance(res, InjectionResult)
    f = res.frame
    # slot 8: t=0 -> shape 0 -> unchanged and not simulated
    assert f["conductivity"].iloc[8] == 400.0 and not f["simulated"].iloc[8]
    # slot 9: t=15 -> shape 1/3
    assert f["conductivity"].iloc[9] == pytest.approx(400 + 250 / 3)
    assert f["ph"].iloc[9] == pytest.approx(8.0 - 0.2)
    assert f["turbidity"].iloc[9] == pytest.approx(1.0 + 5.0)
    # slot 11: t=45 -> peak
    assert f["conductivity"].iloc[11] == pytest.approx(650.0)
    assert f["ph"].iloc[11] == pytest.approx(7.4)
    assert f["turbidity"].iloc[11] == pytest.approx(16.0)
    # slot 35: t=405 = rise + 6 h -> one e-folding
    assert f["conductivity"].iloc[35] == pytest.approx(400 + 250 * math.exp(-1))
    assert f["turbidity"].iloc[35] == pytest.approx(1.0 + 15 * math.exp(-1))
    # before the start nothing changes
    pd.testing.assert_frame_equal(f[ORIGINAL].iloc[:9], df[ORIGINAL].iloc[:9])
    assert not f["simulated"].iloc[:9].any()


def test_duration_cutoff():
    df = make_frame()
    df.loc[df.index[20], "conductivity"] = np.nan  # gap after the cutoff: not a skipped slot
    res = inject_spill(df, params_at(df, 8, noise_fraction=0.0, duration_hours=2.0))
    f = res.frame
    assert f["conductivity"].iloc[15] != 400.0  # t=105 min, inside
    assert f["simulated"].iloc[15]
    pd.testing.assert_frame_equal(f[ORIGINAL].iloc[17:], df[ORIGINAL].iloc[17:])  # t>=135 min
    assert not f["simulated"].iloc[17:].any()
    assert not any(pd.Timestamp(s["timestamp"]) == df.index[20] for s in res.skipped)


def test_clipping_ph_and_turbidity():
    df = make_frame()
    df["ph"] = 0.3
    res = inject_spill(df, params_at(df, 8, noise_fraction=0.0, ph_delta=-0.6, turbidity_delta=-5.0))
    f = res.frame
    assert f["ph"].min() >= 0.0 and f["ph"].iloc[11] == 0.0
    assert f["turbidity"].min() >= 0.0 and f["turbidity"].iloc[11] == 0.0
    up = inject_spill(make_frame(), params_at(df, 8, noise_fraction=0.0, ph_delta=10.0))
    assert up.frame["ph"].max() <= 14.0 and up.frame["ph"].iloc[11] == 14.0


# ---- flags -----------------------------------------------------------------------------------
def test_injected_rows_are_flagged_exactly_where_values_changed():
    df = make_frame(seed=1)
    res = inject_spill(df, params_at(df, 8))
    f = res.frame
    for col in ("simulated", "conductivity_simulated", "ph_simulated", "turbidity_simulated"):
        assert f[col].dtype == bool
    changed_any = pd.Series(False, index=df.index)
    for p in PARAMS:
        changed = ~((f[p] == df[p]) | (f[p].isna() & df[p].isna()))
        assert (f[f"{p}_simulated"] == changed).all()
        assert res.injected_slots[p] == int(changed.sum()) > 0
        changed_any |= changed
    assert (f["simulated"] == changed_any).all()
    for col in ("discharge", "conductivity_qualifiers", "turbidity_qualifiers", "ph_qualifiers", "discharge_qualifiers"):
        pd.testing.assert_series_equal(f[col], df[col])
    untouched = ~f["simulated"]
    pd.testing.assert_frame_equal(f.loc[untouched, ORIGINAL], df.loc[untouched, ORIGINAL])
    assert res.params.seed == 42


def test_input_frame_not_mutated():
    df = make_frame(seed=2)
    before = df.copy()
    inject_spill(df, params_at(df, 8))
    pd.testing.assert_frame_equal(df, before)
    assert "simulated" not in df.columns


# ---- gaps ------------------------------------------------------------------------------------
def test_gaps_stay_gaps_when_injection_starts_in_a_gap():
    df = make_frame(seed=3)
    gap = df.index[7:13]  # spill starts at slot 8, inside the conductivity gap 7..12
    df.loc[gap, "conductivity"] = np.nan
    df.loc[df.index[10], ["turbidity", "ph"]] = np.nan  # row 10: every injected parameter missing
    res = inject_spill(df, params_at(df, 8, noise_fraction=0.0))
    f = res.frame
    assert f.loc[gap, "conductivity"].isna().all()
    assert not f.loc[gap, "conductivity_simulated"].any()
    assert f.loc[df.index[10], ["turbidity", "ph"]].isna().all()
    assert not f["simulated"].iloc[10]
    # rows 9, 11, 12: conductivity is a gap but turbidity was injected
    assert f["turbidity_simulated"].iloc[9] and f["simulated"].iloc[9]
    assert f["conductivity"].iloc[13] == pytest.approx(df["conductivity"].iloc[13] + 250 * math.exp(-30 / 360))

    skipped = {(pd.Timestamp(s["timestamp"]), s["parameter"]): s["reason"] for s in res.skipped}
    for slot in (9, 10, 11, 12):  # shape > 0 in the window, real value NaN
        assert skipped[(df.index[slot], "conductivity")] == "gap: no real value, not written"
    assert (df.index[10], "turbidity") in skipped and (df.index[10], "ph") in skipped
    assert all(ts in df.index[7:13] for ts, _ in skipped)
    assert (df.index[7], "conductivity") not in skipped  # before the start
    assert res.injected_slots["conductivity"] == int(f["conductivity_simulated"].sum())


def test_noise_draw_does_not_depend_on_gaps():
    df = make_frame(seed=4)
    holed = df.copy()
    holed.loc[df.index[12], "turbidity"] = np.nan
    a = inject_spill(df, params_at(df, 8)).frame
    b = inject_spill(holed, params_at(df, 8)).frame
    pd.testing.assert_series_equal(a["conductivity"], b["conductivity"])
    pd.testing.assert_series_equal(a["ph"], b["ph"])
    mask = df.index != df.index[12]
    pd.testing.assert_series_equal(a.loc[mask, "turbidity"], b.loc[mask, "turbidity"])
    assert np.isnan(b["turbidity"].iloc[12])


# ---- determinism -----------------------------------------------------------------------------
def test_same_seed_same_output_different_seed_differs_only_in_injected_values():
    df = make_frame(seed=5)
    a = inject_spill(df, params_at(df, 8, seed=7))
    b = inject_spill(df, params_at(df, 8, seed=7))
    pd.testing.assert_frame_equal(a.frame, b.frame)
    assert a.skipped == b.skipped and a.injected_slots == b.injected_slots

    c = inject_spill(df, params_at(df, 8, seed=8))
    flags = ["simulated", "conductivity_simulated", "ph_simulated", "turbidity_simulated"]
    pd.testing.assert_frame_equal(a.frame[flags], c.frame[flags])
    sim = a.frame["simulated"]
    pd.testing.assert_frame_equal(a.frame.loc[~sim, ORIGINAL], c.frame.loc[~sim, ORIGINAL])
    for p in PARAMS:
        m = a.frame[f"{p}_simulated"]
        assert (a.frame.loc[m, p] != c.frame.loc[m, p]).any()


def test_noise_is_a_small_multiplicative_jitter():
    df = make_frame()
    res = inject_spill(df, params_at(df, 8, noise_fraction=0.03))
    # at the peak, delta * (1 + noise) with noise ~ N(0, 0.03): stay within 6 sigma
    ratio = (res.frame["conductivity"].iloc[11] - 400.0) / 250.0
    assert abs(ratio - 1) < 0.18
    assert not np.allclose(res.frame["conductivity"].iloc[9:20], inject_spill(df, params_at(df, 8, noise_fraction=0.0)).frame["conductivity"].iloc[9:20])


# ---- settings and the real snapshot ----------------------------------------------------------
def test_spill_params_from_settings_object():
    from datetime import datetime, timezone

    ts = datetime(2026, 10, 10, 14, tzinfo=timezone.utc)
    s = SimpleNamespace(scenario_timestamp=ts, random_seed=7)
    p = spill_params_from_settings(s)
    assert p.start == ts and p.seed == 7 and p.conductivity_delta == 250.0
    assert spill_params_from_settings(s, conductivity_delta=100.0).conductivity_delta == 100.0


@pytest.fixture(scope="module")
def settings():
    try:
        return get_settings()
    except ConfigError:
        pytest.skip("no .env")


def test_real_snapshot_injection_at_scenario_timestamp(settings):
    snap = baselines.load_snapshot()
    before = snap.sensors.copy()
    params = spill_params_from_settings()
    assert params.start == settings.scenario_timestamp and params.seed == settings.random_seed
    res = inject_spill(snap.sensors, params)
    pd.testing.assert_frame_equal(snap.sensors, before)
    assert res.skipped == []  # all sensors are continuous through the 24 h after the scenario
    start = pd.Timestamp(params.start)
    sim_idx = res.frame.index[res.frame["simulated"]]
    assert sim_idx.min() > start and sim_idx.max() <= start + timedelta(hours=24)
    for p in PARAMS:
        assert res.injected_slots[p] >= 95
    pd.testing.assert_series_equal(res.frame["discharge"], before["discharge"])
