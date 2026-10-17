"""Rolling z-score anomaly detection on the 15-min gauge series.

Each slot is compared with the mean and standard deviation of the real values in the window
before it (the slot itself is never in its own baseline). Gaps are never filled: a NaN slot has
no z, and a slot whose window holds too few real values has no baseline.
"""
from __future__ import annotations

from datetime import datetime, timezone

import numpy as np
import pandas as pd

from ..schemas import SensorEvent

DETECTED_PARAMETERS = ("conductivity", "turbidity", "ph")
STD_FLOOR = {"conductivity": 2.0, "turbidity": 0.2, "ph": 0.02}  # avoid z blow-ups on flat baselines


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


def zscores(
    sensors: pd.DataFrame,
    parameter: str,
    window: str = "24h",
    min_periods: int = 48,
    baseline_end: datetime | None = None,
) -> pd.DataFrame:
    """Columns value, mean, std (floored with STD_FLOOR), z. NaN value or baseline -> NaN z."""
    value = sensors[parameter].astype("float64")
    mean, std = rolling_baseline(value, window, min_periods, baseline_end)
    std = std.clip(lower=STD_FLOOR.get(parameter, 0.0))  # NaN stays NaN
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
) -> list[SensorEvent]:
    """One SensorEvent per run of consecutive same-sign slots with |z| >= z_threshold.

    A run may bridge at most max_gap_slots consecutive missing slots, never a real
    non-anomalous value (or a real value with no baseline). start/end (inclusive) restrict
    which slots can be anomalous; the baseline still uses earlier data.
    """
    events: list[SensorEvent] = []
    for parameter in parameters:
        if parameter not in sensors.columns:
            continue
        zs = zscores(sensors, parameter, window, min_periods, baseline_end)
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
    events.sort(key=lambda e: (e.timestamp.astimezone(timezone.utc), e.parameter))
    return events
