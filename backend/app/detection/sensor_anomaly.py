"""Rolling z-score anomaly detection on the 15-min gauge series.

Each slot is compared with the mean and standard deviation of the real values in the window
before it (the slot itself is never in its own baseline). Gaps are never filled: a NaN slot has
no z, and a slot whose window holds too few real values has no baseline.
"""
from __future__ import annotations

import warnings
from datetime import datetime, timezone
from typing import Literal

import numpy as np
import pandas as pd

from ..schemas import SensorEvent

DETECTED_PARAMETERS = ("conductivity", "turbidity", "ph")
STD_FLOOR = {"conductivity": 2.0, "turbidity": 0.2, "ph": 0.02}  # avoid z blow-ups on flat baselines
# pH follows a daily cycle, so it is compared with the same time of day over the previous 72 h.
HOUR_OF_DAY_PARAMETERS = ("ph",)
HOUR_OF_DAY_DAYS = 3
HOUR_OF_DAY_MIN_POINTS = 6
# The gauge reports pH in 0.1 steps; same-time references are often identical, so the std
# floor is set at that resolution or a single 0.1 step would score as an anomaly.
HOUR_OF_DAY_STD_FLOOR = {"ph": 0.1}
MERGE_WITHIN = "1h"  # events of one parameter and direction closer than this are joined


def _utc(ts: datetime | pd.Timestamp) -> pd.Timestamp:
    """Timestamp in UTC; naive input is taken as UTC."""
    ts = pd.Timestamp(ts)
    return ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")


def rolling_baseline(
    series: pd.Series,
    window: str = "24h",
    min_periods: int = 48,
    baseline_end: datetime | None = None,
) -> tuple[pd.Series, pd.Series]:
    """Rolling mean and std over the preceding `window` only (closed="left"), NaNs excluded.

    A slot gets a baseline only if >= min_periods real points fall in its window. With
    baseline_end, every slot after it uses the baseline computed at baseline_end.
    """
    series = series.astype("float64")
    roll = series.rolling(window, closed="left", min_periods=min_periods)
    mean, std = roll.mean(), roll.std()
    if baseline_end is not None:
        be = _utc(baseline_end)
        idx = series.index
        held = series[(idx >= be - pd.Timedelta(window)) & (idx < be)].dropna()
        if len(held) >= min_periods:
            m, s = float(held.mean()), float(held.std())
        else:
            m = s = np.nan
        after = idx > be
        mean = mean.copy()
        std = std.copy()
        mean[after] = m
        std[after] = s
    return mean, std


def hour_of_day_baseline(
    series: pd.Series,
    days: int = HOUR_OF_DAY_DAYS,
    half_width_slots: int = 2,
    min_points: int = HOUR_OF_DAY_MIN_POINTS,
    baseline_end: datetime | None = None,
) -> tuple[pd.Series, pd.Series]:
    """Mean and std of the same time of day on the previous `days` days (± half_width_slots slots).

    Removes a regular daily cycle (pH rises and falls with photosynthesis) from the baseline.
    Reference slots are always at least 24 h earlier minus half_width_slots, never the slot itself.
    With baseline_end, reference values at or after baseline_end are not used.
    """
    series = series.astype("float64")
    idx = series.index
    step = pd.Timedelta("15min")
    refs = []
    for k in range(1, days + 1):
        for off in range(-half_width_slots, half_width_slots + 1):
            lag = pd.Timedelta(days=k) + off * step
            ref = series.reindex(idx - lag)
            ref.index = idx
            if baseline_end is not None:
                ref[(idx - lag) >= _utc(baseline_end)] = np.nan
            refs.append(ref.to_numpy())
    stack = np.vstack(refs)
    count = np.sum(~np.isnan(stack), axis=0)
    with np.errstate(invalid="ignore"), warnings.catch_warnings():
        warnings.simplefilter("ignore", category=RuntimeWarning)
        mean = np.nanmean(stack, axis=0)
        std = np.nanstd(stack, axis=0, ddof=1)
    ok = count >= min_points
    return (
        pd.Series(np.where(ok, mean, np.nan), index=idx),
        pd.Series(np.where(ok, std, np.nan), index=idx),
    )


def zscores(
    sensors: pd.DataFrame,
    parameter: str,
    window: str = "24h",
    min_periods: int = 48,
    baseline_end: datetime | None = None,
    baseline: Literal["rolling", "hour_of_day"] = "rolling",
) -> pd.DataFrame:
    """Columns value, mean, std (floored with STD_FLOOR), z. NaN value or baseline -> NaN z."""
    value = sensors[parameter].astype("float64")
    floor = STD_FLOOR.get(parameter, 0.0)
    if baseline == "hour_of_day":
        mean, std = hour_of_day_baseline(value, baseline_end=baseline_end)
        floor = max(floor, HOUR_OF_DAY_STD_FLOOR.get(parameter, 0.0))
    else:
        mean, std = rolling_baseline(value, window, min_periods, baseline_end)
    std = std.clip(lower=floor)  # NaN stays NaN
    z = (value - mean) / std
    return pd.DataFrame({"value": value, "mean": mean, "std": std, "z": z}, index=sensors.index)


def _event(parameter: str, zs: pd.DataFrame, slots: list[pd.Timestamp], sim: pd.Series | None) -> SensorEvent:
    run = zs.loc[slots]
    peak = run["z"].abs().idxmax()
    first, last = slots[0], slots[-1]
    includes_sim = bool(sim is not None and sim.loc[first:last].fillna(False).astype(bool).any())
    return SensorEvent(
        timestamp=first.to_pydatetime(),
        parameter=parameter,
        score=float(abs(run.at[peak, "z"])),
        value=float(run.at[peak, "value"]),
        baseline_mean=float(run.at[peak, "mean"]),
        baseline_std=float(run.at[peak, "std"]),
        direction="rise" if run.at[peak, "z"] > 0 else "drop",
        peak_timestamp=peak.to_pydatetime(),
        end_timestamp=last.to_pydatetime(),
        n_points=len(slots),
        includes_simulated=includes_sim,
    )


def detect_anomalies(
    sensors: pd.DataFrame,
    parameters: tuple[str, ...] | list[str] = DETECTED_PARAMETERS,
    window: str = "24h",
    min_periods: int = 48,
    z_threshold: float = 4.0,
    baseline_end: datetime | None = None,
    start: datetime | None = None,
    end: datetime | None = None,
    max_gap_slots: int = 1,
    hour_of_day_parameters: tuple[str, ...] = HOUR_OF_DAY_PARAMETERS,
    merge_within: str | None = MERGE_WITHIN,
) -> list[SensorEvent]:
    """One SensorEvent per run of consecutive same-sign slots with |z| >= z_threshold.

    A run may bridge at most max_gap_slots consecutive missing slots, never a real
    non-anomalous value (or a real value with no baseline). start/end (inclusive) restrict
    which slots can be anomalous; the baseline still uses earlier data. Parameters in
    hour_of_day_parameters use the same-time-of-day baseline. Runs of one parameter and
    direction less than merge_within apart are then joined (None keeps every run separate).
    """
    events: list[SensorEvent] = []
    for parameter in parameters:
        if parameter not in sensors.columns:
            continue
        method = "hour_of_day" if parameter in hour_of_day_parameters else "rolling"
        zs = zscores(sensors, parameter, window, min_periods, baseline_end, baseline=method)
        sim_col = f"{parameter}_simulated"
        sim = sensors[sim_col] if sim_col in sensors.columns else None
        view = zs
        if start is not None:
            view = view[view.index >= _utc(start)]
        if end is not None:
            view = view[view.index <= _utc(end)]

        slots: list[pd.Timestamp] = []
        sign = 0
        gap = 0
        for ts, value, z in zip(view.index, view["value"].to_numpy(), view["z"].to_numpy()):
            if np.isnan(value):
                if slots:
                    gap += 1
                    if gap > max_gap_slots:
                        events.append(_event(parameter, zs, slots, sim))
                        slots, gap = [], 0
                continue
            s = int(np.sign(z)) if not np.isnan(z) and abs(z) >= z_threshold else 0
            if slots and s != sign:
                events.append(_event(parameter, zs, slots, sim))
                slots = []
            if s:
                slots.append(ts)
                sign = s
            gap = 0
        if slots:
            events.append(_event(parameter, zs, slots, sim))
    if merge_within is not None:
        events = merge_events(events, merge_within)
    events.sort(key=lambda e: (e.timestamp.astimezone(timezone.utc), e.parameter))
    return events


def merge_events(events: list[SensorEvent], within: str = MERGE_WITHIN) -> list[SensorEvent]:
    """Join events of the same parameter and direction whose gap (end to next onset) is < within.

    The merged event keeps the first onset, the last end, the strongest peak and the summed points.
    """
    limit = pd.Timedelta(within)
    out: list[SensorEvent] = []
    for e in sorted(events, key=lambda e: (e.parameter, e.direction, e.timestamp)):
        prev = out[-1] if out else None
        if (
            prev is not None
            and prev.parameter == e.parameter
            and prev.direction == e.direction
            and pd.Timestamp(e.timestamp) - pd.Timestamp(prev.end_timestamp) < limit
        ):
            peak = prev if prev.score >= e.score else e
            out[-1] = peak.model_copy(
                update={
                    "timestamp": prev.timestamp,
                    "end_timestamp": max(prev.end_timestamp, e.end_timestamp),
                    "n_points": prev.n_points + e.n_points,
                    "includes_simulated": prev.includes_simulated or e.includes_simulated,
                }
            )
        else:
            out.append(e)
    return out
