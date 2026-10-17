"""Synthetic spill pulse added on top of real gauge data for the demo scenario.

Only real values are shifted: a gap (NaN) in the real record stays a gap and is reported in
`skipped`. Discharge and qualifier columns are never touched. Every changed cell is marked in a
`<parameter>_simulated` column so downstream code can tell real from injected values.
"""
from __future__ import annotations

from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from typing import Any

import numpy as np
import pandas as pd

from ..config import Settings, get_settings

# Fixed order: also the order noise is drawn per slot, so the draw never depends on gaps.
INJECTED_PARAMETERS = ("conductivity", "ph", "turbidity")
SKIP_REASON = "gap: no real value, not written"


@dataclass(frozen=True)
class SpillParams:
    start: datetime  # tz-aware UTC; spill onset
    conductivity_delta: float = 250.0  # uS/cm added at peak
    # pH units added at peak; result clipped to [0, 14]. Large enough to stand out from the diurnal
    # swing (~8.2-8.7 at the demo gauge), which a -0.6 shift at the daily high does not.
    ph_delta: float = -1.5
    turbidity_delta: float = 15.0  # FNU added at peak; result clipped at >= 0
    rise_minutes: float = 45.0  # linear rise from 0 to peak
    decay_hours: float = 6.0  # e-folding decay after the peak
    duration_hours: float = 24.0  # nothing injected after start + duration
    noise_fraction: float = 0.03  # delta * shape * (1 + noise), noise ~ N(0, noise_fraction)
    seed: int = 42

    def delta(self, parameter: str) -> float:
        return float(getattr(self, f"{parameter}_delta"))


@dataclass
class InjectionResult:
    frame: pd.DataFrame
    params: SpillParams
    injected_slots: dict[str, int] = field(default_factory=dict)
    skipped: list[dict[str, Any]] = field(default_factory=list)


def pulse_shape(minutes_since_start: np.ndarray | float, rise_minutes: float, decay_hours: float) -> np.ndarray:
    """Pulse in [0, 1]: 0 before start, linear rise to 1, then exponential decay."""
    t = np.asarray(minutes_since_start, dtype=float)
    decay_min = decay_hours * 60.0
    with np.errstate(divide="ignore", invalid="ignore", over="ignore"):
        if rise_minutes <= 0:
            rising = np.zeros_like(t)
            after = t >= 0
            tail = np.exp(-t / decay_min) if decay_min > 0 else np.where(t == 0, 1.0, 0.0)
        else:
            rising = t / rise_minutes
            after = t >= rise_minutes
            tail = np.exp(-(t - rise_minutes) / decay_min) if decay_min > 0 else np.where(t == rise_minutes, 1.0, 0.0)
        out = np.where(t < 0, 0.0, np.where(after, tail, rising))
    return np.asarray(out, dtype=float)


def _utc(ts: datetime | pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    return ts.tz_localize(timezone.utc) if ts.tzinfo is None else ts.tz_convert(timezone.utc)


def inject_spill(sensors: pd.DataFrame, params: SpillParams) -> InjectionResult:
    """Add the spill pulse to a copy of the wide sensor frame. Never writes into a gap."""
    frame = sensors.copy()
    index = pd.DatetimeIndex(frame.index)
    if index.tz is None:
        index = index.tz_localize(timezone.utc)
    start = _utc(params.start)
    minutes = np.asarray((index - start) / pd.Timedelta(minutes=1), dtype=float)
    shape = pulse_shape(minutes, params.rise_minutes, params.decay_hours)
    in_window = (minutes >= 0) & (minutes <= params.duration_hours * 60.0) & (shape > 0)
    positions = np.flatnonzero(in_window)

    rng = np.random.default_rng(params.seed)
    noise = rng.normal(0.0, params.noise_fraction, size=(len(positions), len(INJECTED_PARAMETERS)))

    injected: dict[str, int] = {}
    skipped: list[dict[str, Any]] = []
    sim_cols: dict[str, np.ndarray] = {}
    for j, name in enumerate(INJECTED_PARAMETERS):
        flags = np.zeros(len(frame), dtype=bool)
        sim_cols[name] = flags
        if name not in frame.columns:
            injected[name] = 0
            continue
        values = frame[name].to_numpy(dtype=float, copy=True)
        window_vals = values[positions]
        real = ~np.isnan(window_vals)
        new = window_vals + params.delta(name) * shape[positions] * (1.0 + noise[:, j])
        if name == "ph":
            new = np.clip(new, 0.0, 14.0)
        elif name == "turbidity":
            new = np.maximum(new, 0.0)
        write_pos = positions[real]
        values[write_pos] = new[real]
        flags[write_pos] = True
        frame[name] = values
        injected[name] = int(real.sum())
        for p in positions[~real]:
            skipped.append({"timestamp": index[p].to_pydatetime(), "parameter": name, "reason": SKIP_REASON})

    frame["simulated"] = np.logical_or.reduce([sim_cols[n] for n in INJECTED_PARAMETERS])
    for name in INJECTED_PARAMETERS:
        frame[f"{name}_simulated"] = sim_cols[name]
    order = {n: i for i, n in enumerate(INJECTED_PARAMETERS)}
    skipped.sort(key=lambda s: (s["timestamp"], order[s["parameter"]]))
    return InjectionResult(frame=frame, params=params, injected_slots=injected, skipped=skipped)


def spill_params_from_settings(settings: Settings | None = None, **overrides: Any) -> SpillParams:
    """SpillParams starting at settings.scenario_timestamp with settings.random_seed; overrides win."""
    settings = settings or get_settings()
    base = SpillParams(start=settings.scenario_timestamp, seed=settings.random_seed)
    return replace(base, **overrides) if overrides else base
