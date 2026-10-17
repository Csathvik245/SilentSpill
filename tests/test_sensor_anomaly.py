import numpy as np
import pandas as pd
import pytest

from backend.app.detection.sensor_anomaly import (
    DETECTED_PARAMETERS,
    STD_FLOOR,
    detect_anomalies,
    rolling_baseline,
    zscores,
)


def grid(n: int) -> pd.DatetimeIndex:
    return pd.date_range("2026-03-01", periods=n, freq="15min", tz="UTC")


def noisy_frame(n: int = 288, seed: int = 0) -> pd.DataFrame:
    """Noise well below STD_FLOOR so no slot reaches |z| >= 4 by chance."""
    rng = np.random.default_rng(seed)
    df = pd.DataFrame({
        "conductivity": 400 + rng.normal(0, 1, n),
        "turbidity": 1.5 + rng.normal(0, 0.05, n),
        "ph": 8.1 + rng.normal(0, 0.005, n),
        "discharge": 100 + rng.normal(0, 2, n),
    }, index=grid(n))
    for c in ("conductivity", "turbidity", "ph", "discharge"):
        df[f"{c}_qualifiers"] = "A"
    return df


FROZEN = 149  # baseline_end slot for run-grouping tests: 96 real points before it


def detect_frozen(df: pd.DataFrame, **kw):
    # Run-building mechanics on the rolling baseline: these 3-day frames are too short for the
    # 72 h same-time-of-day pH baseline, and the runs are deliberately close, so no merging here.
    kw.setdefault("hour_of_day_parameters", ())
    kw.setdefault("merge_within", None)
    return detect_anomalies(df, baseline_end=df.index[FROZEN].to_pydatetime(), **kw)


def test_constants():
    assert DETECTED_PARAMETERS == ("conductivity", "turbidity", "ph")
    assert STD_FLOOR == {"conductivity": 2.0, "turbidity": 0.2, "ph": 0.02}


# ---- rolling_baseline ------------------------------------------------------------------------
def test_rolling_baseline_is_closed_left():
    s = pd.Series(10.0, index=grid(100))
    s.iloc[60] = 100.0
    mean, std = rolling_baseline(s, window="1h", min_periods=4)
    assert mean.iloc[60] == pytest.approx(10.0)  # its own spike is not in its baseline
    assert std.iloc[60] == pytest.approx(0.0)
    assert mean.iloc[61] == pytest.approx((10 + 10 + 10 + 100) / 4)  # slots 57..60
    assert mean.iloc[65] == pytest.approx(10.0)  # spike has left the 1 h window
    assert mean.iloc[:4].isna().all()  # fewer than 4 preceding points


def test_rolling_baseline_excludes_nan_and_needs_min_periods():
    s = pd.Series([10, 20, np.nan, 30, 40, 50, 60, 70, 0.0], index=grid(9))
    mean7, _ = rolling_baseline(s, window="2h", min_periods=7)
    assert mean7.iloc[8] == pytest.approx(40.0)  # (10+20+30+40+50+60+70)/7
    mean8, std8 = rolling_baseline(s, window="2h", min_periods=8)
    assert np.isnan(mean8.iloc[8]) and np.isnan(std8.iloc[8])


def test_rolling_baseline_is_time_based():
    full = grid(48)
    idx = full[:20].append(full[28:])  # rows 20..27 absent entirely: a 2 h hole
    s = pd.Series(np.arange(len(idx), dtype=float), index=idx)
    mean, _ = rolling_baseline(s, window="1h", min_periods=1)
    assert np.isnan(mean.loc[full[28]])  # nothing in the preceding hour
    assert mean.loc[full[29]] == pytest.approx(s.loc[full[28]])


def test_baseline_end_freezes_stats():
    s = pd.Series(np.arange(120, dtype=float), index=grid(120))
    end = s.index[50]
    mean, std = rolling_baseline(s, window="1h", min_periods=4, baseline_end=end.to_pydatetime())
    assert mean.iloc[50] == pytest.approx(47.5)  # mean of 46..49
    assert mean.iloc[30] == pytest.approx(27.5)  # before baseline_end: normal rolling
    assert np.allclose(mean.iloc[51:], 47.5)
    assert np.allclose(std.iloc[51:], std.iloc[50])
    free, _ = rolling_baseline(s, window="1h", min_periods=4)
    assert free.iloc[100] == pytest.approx(97.5)


# ---- zscores ---------------------------------------------------------------------------------
def test_zscores_std_floor_and_nan():
    df = pd.DataFrame({"conductivity": 400.0, "turbidity": 1.5, "ph": 8.0}, index=grid(100))
    df.loc[df.index[60], ["conductivity", "turbidity", "ph"]] = [430.0, 2.5, 7.9]
    df.loc[df.index[70], "conductivity"] = np.nan
    zc = zscores(df, "conductivity", window="1h", min_periods=4)
    assert list(zc.columns[:4]) == ["value", "mean", "std", "z"]
    assert zc["std"].iloc[60] == pytest.approx(2.0)
    assert zc["z"].iloc[60] == pytest.approx(15.0)  # (430-400)/2
    assert np.isnan(zc["z"].iloc[70])
    assert zscores(df, "turbidity", window="1h", min_periods=4)["z"].iloc[60] == pytest.approx(5.0)
    assert zscores(df, "ph", window="1h", min_periods=4)["z"].iloc[60] == pytest.approx(-5.0)


# ---- detect_anomalies ------------------------------------------------------------------------
def test_quiet_frame_has_no_events():
    assert detect_anomalies(noisy_frame()) == []


def test_event_fields():
    df = noisy_frame()
    for slot, bump in ((150, 20.0), (151, 60.0), (152, 30.0)):
        df.loc[df.index[slot], "conductivity"] += bump
    events = detect_frozen(df)
    assert len(events) == 1
    e = events[0]
    assert e.parameter == "conductivity" and e.direction == "rise"
    assert e.timestamp == df.index[150] and e.peak_timestamp == df.index[151]
    assert e.end_timestamp == df.index[152] and e.n_points == 3
    z = zscores(df, "conductivity", baseline_end=df.index[FROZEN].to_pydatetime())
    assert e.score == pytest.approx(abs(z["z"].iloc[151]))
    assert e.value == pytest.approx(df["conductivity"].iloc[151])
    assert e.baseline_mean == pytest.approx(z["mean"].iloc[151])
    assert e.baseline_std == pytest.approx(z["std"].iloc[151])
    assert e.score == pytest.approx(30.0, abs=3)  # ~60 / 2.0 floor
    assert e.includes_simulated is False


def test_drop_direction_and_positive_score():
    df = noisy_frame()
    df.loc[df.index[160:163], "ph"] -= 0.3
    (e,) = detect_frozen(df)
    assert e.parameter == "ph" and e.direction == "drop" and e.score > 4
    assert e.n_points == 3


def test_run_bridges_one_nan_slot():
    df = noisy_frame()
    df.loc[df.index[[150, 151, 153, 154]], "conductivity"] += 100
    df.loc[df.index[152], "conductivity"] = np.nan
    (e,) = detect_frozen(df)
    assert e.timestamp == df.index[150] and e.end_timestamp == df.index[154]
    assert e.n_points == 4  # the NaN slot is not counted


def test_run_does_not_bridge_more_than_max_gap_slots():
    df = noisy_frame()
    df.loc[df.index[[150, 151, 154, 155]], "conductivity"] += 100
    df.loc[df.index[152:154], "conductivity"] = np.nan
    assert len(detect_frozen(df)) == 2
    (e,) = detect_frozen(df, max_gap_slots=2)
    assert e.n_points == 4


def test_run_splits_on_non_anomalous_real_value():
    df = noisy_frame()
    df.loc[df.index[[150, 151, 153]], "conductivity"] += 100
    events = detect_frozen(df)
    assert [(e.timestamp, e.n_points) for e in events] == [(df.index[150], 2), (df.index[153], 1)]


def test_run_splits_on_sign_change():
    df = noisy_frame()
    df.loc[df.index[150:152], "conductivity"] += 100
    df.loc[df.index[152:154], "conductivity"] -= 100
    events = detect_frozen(df)
    assert [e.direction for e in events] == ["rise", "drop"]
    assert events[0].end_timestamp == df.index[151] and events[1].timestamp == df.index[152]


def test_trailing_nan_is_not_the_end():
    df = noisy_frame()
    df.loc[df.index[150], "conductivity"] += 100
    df.loc[df.index[151], "conductivity"] = np.nan
    (e,) = detect_frozen(df)
    assert e.end_timestamp == df.index[150] and e.n_points == 1


def test_nan_slots_never_create_events():
    df = noisy_frame()
    df.loc[df.index[150:160], ["conductivity", "turbidity", "ph"]] = np.nan
    assert detect_frozen(df) == []
    assert detect_anomalies(df) == []


def test_too_few_baseline_points_means_no_event():
    df = noisy_frame()
    df.loc[df.index[10], "conductivity"] += 100  # only 10 preceding points < min_periods 48
    df.loc[df.index[200], "turbidity"] += 5
    events = detect_anomalies(df)
    assert [(e.parameter, e.timestamp) for e in events] == [("turbidity", df.index[200])]
    # a slot whose window is mostly a gap also gets no baseline
    df2 = noisy_frame()
    df2.loc[df2.index[100:190], "conductivity"] = np.nan  # 24 h window before 200 has 10 points
    df2.loc[df2.index[200], "conductivity"] += 100
    assert detect_anomalies(df2) == []


def test_start_end_restrict_and_sort_order():
    df = noisy_frame()
    for p, d in (("conductivity", 100), ("turbidity", 5), ("ph", 0.5)):
        df.loc[df.index[150], p] += d
    df.loc[df.index[200], "conductivity"] += 100
    events = detect_frozen(df)
    assert [(e.timestamp, e.parameter) for e in events] == [
        (df.index[150], "conductivity"), (df.index[150], "ph"),
        (df.index[150], "turbidity"), (df.index[200], "conductivity"),
    ]
    late = detect_frozen(df, start=df.index[180].to_pydatetime())
    assert [e.timestamp for e in late] == [df.index[200]]
    early = detect_frozen(df, end=df.index[180].to_pydatetime())
    assert {e.timestamp for e in early} == {df.index[150]}
    only = detect_frozen(df, parameters=("turbidity",))
    assert [e.parameter for e in only] == ["turbidity"]


def test_includes_simulated_from_flag_column():
    df = noisy_frame()
    df.loc[df.index[150:153], "conductivity"] += 100
    df.loc[df.index[150:153], "turbidity"] += 5
    df.loc[df.index[160:162], "ph"] -= 0.5
    df["conductivity_simulated"] = False
    df.loc[df.index[151], "conductivity_simulated"] = True
    df["ph_simulated"] = False
    by = {e.parameter: e for e in detect_frozen(df)}
    assert by["conductivity"].includes_simulated is True
    assert by["turbidity"].includes_simulated is False  # no flag column
    assert by["ph"].includes_simulated is False  # flag column all False


# ---- same-time-of-day pH baseline and event merging ------------------------------------------
from datetime import datetime, timedelta, timezone  # noqa: E402

from backend.app.detection.sensor_anomaly import hour_of_day_baseline, merge_events  # noqa: E402
from backend.app.schemas import SensorEvent  # noqa: E402


def diurnal_frame(days: int = 5) -> pd.DataFrame:
    """pH swinging 8.2-8.7 every day (like the demo gauge) on a 15-min grid; flat other columns."""
    n = days * 96
    idx = grid(n)
    hours = (idx.hour + idx.minute / 60).to_numpy()
    df = pd.DataFrame({
        "conductivity": 400.0, "turbidity": 1.0,
        "ph": np.round(8.45 + 0.25 * np.sin(2 * np.pi * (hours - 14) / 24), 1),
        "discharge": 100.0,
    }, index=idx)
    for c in ("conductivity", "turbidity", "ph", "discharge"):
        df[f"{c}_qualifiers"] = "A"
    return df


def test_hour_of_day_baseline_uses_same_time_on_earlier_days():
    df = diurnal_frame()
    mean, _ = hour_of_day_baseline(df["ph"])
    day4 = df.index[3 * 96:]
    # A perfectly repeating cycle: the baseline equals the value at that time of day.
    np.testing.assert_allclose(mean[day4].to_numpy(), df.loc[day4, "ph"].to_numpy(), atol=0.1)
    assert mean.iloc[:90].isna().all()  # under one day of history: too few same-time points


def test_daily_cycle_alone_raises_no_ph_events():
    df = diurnal_frame()
    assert detect_anomalies(df, parameters=("ph",), start=df.index[3 * 96].to_pydatetime()) == []


def test_ph_drop_at_daily_high_is_detected_against_same_time_baseline():
    df = diurnal_frame()
    peak = df["ph"].iloc[3 * 96:4 * 96].idxmax()
    i = df.index.get_loc(peak)
    df.iloc[i:i + 4, df.columns.get_loc("ph")] -= 0.8
    events = detect_anomalies(df, parameters=("ph",), baseline_end=peak.to_pydatetime(),
                              start=peak.to_pydatetime())
    assert [e.direction for e in events] == ["drop"]
    assert events[0].score >= 4


def test_hour_of_day_baseline_respects_baseline_end():
    df = diurnal_frame()
    be = df.index[3 * 96]
    df.loc[df.index > be, "ph"] = 5.0  # anything after be must not enter the baseline
    mean, _ = hour_of_day_baseline(df["ph"], baseline_end=be.to_pydatetime())
    assert (mean[df.index > be].dropna() > 8).all()


def _ev(param, direction, start_min, end_min, score=5.0, n=2, sim=False):
    t0 = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
    return SensorEvent(
        timestamp=t0 + timedelta(minutes=start_min), parameter=param, score=score, value=1.0,
        baseline_mean=0.0, baseline_std=1.0, direction=direction,
        peak_timestamp=t0 + timedelta(minutes=start_min), end_timestamp=t0 + timedelta(minutes=end_min),
        n_points=n, includes_simulated=sim,
    )


def test_merge_joins_same_parameter_and_direction_under_one_hour():
    a, b = _ev("ph", "drop", 0, 30, score=6, n=3), _ev("ph", "drop", 75, 90, score=9, n=2, sim=True)
    (m,) = merge_events([a, b])  # gap 45 min
    assert m.timestamp == a.timestamp and m.end_timestamp == b.end_timestamp
    assert m.score == 9 and m.peak_timestamp == b.peak_timestamp
    assert m.n_points == 5 and m.includes_simulated


def test_merge_keeps_events_an_hour_or_more_apart_or_different():
    assert len(merge_events([_ev("ph", "drop", 0, 30), _ev("ph", "drop", 90, 100)])) == 2  # 60 min
    assert len(merge_events([_ev("ph", "drop", 0, 30), _ev("ph", "rise", 45, 60)])) == 2
    assert len(merge_events([_ev("ph", "drop", 0, 30), _ev("turbidity", "drop", 45, 60)])) == 2


def test_detect_merges_by_default():
    df = noisy_frame()
    df.loc[df.index[[150, 151, 154, 155]], "conductivity"] += 100  # two runs 45 min apart (onset to onset)
    kw = dict(hour_of_day_parameters=(), baseline_end=df.index[FROZEN].to_pydatetime())
    assert len(detect_anomalies(df, merge_within=None, **kw)) == 2
    (e,) = detect_anomalies(df, **kw)
    assert e.n_points == 4 and e.end_timestamp == df.index[155]
