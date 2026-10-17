"""Storm filter: separate rain-driven turbidity from events that need human review.

Rain is GHCND daily totals, so "the last N hours" is approximated by whole calendar days
(noaa.window_total). A day with no reading is a gap: it is never treated as 0 or as dry weather.
"""
from __future__ import annotations

from datetime import datetime, timezone

import pandas as pd

from ..data import noaa
from ..schemas import RainContext, RainDay, SensorEvent, StormDecision, StormFilterResult


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def rain_context(
    daily: pd.DataFrame, event_time: datetime, threshold_inches: float, window_hours: int, station_id: str
) -> RainContext:
    """Daily rain over the event's UTC calendar day plus the earlier days the window needs."""
    total, days, n_missing = noaa.window_total(daily, _utc(event_time), window_hours)
    lookup = dict(zip(daily["date"], daily["prcp_inches"]))
    rain_days = []
    for d in days:
        v = lookup.get(d)
        rain_days.append(RainDay(day=d, prcp_inches=None if v is None or pd.isna(v) else float(v)))
    return RainContext(
        station_id=station_id,
        days=rain_days,
        window_total_inches=total,
        threshold_inches=threshold_inches,
        exceeded=None if total is None else total >= threshold_inches,
        missing_days=n_missing,
    )


def _fmt_days(rain: RainContext) -> str:
    parts = []
    for d in rain.days:
        val = "no reading" if d.prcp_inches is None else f"{d.prcp_inches:.2f} in"
        parts.append(f"{d.day.isoformat()}: {val}")
    return "; ".join(parts)


def _missing_note(rain: RainContext) -> str:
    if not rain.missing_days:
        return ""
    s = "" if rain.missing_days == 1 else "s"
    return f" {rain.missing_days} day{s} in the window had no rain reading, so the total may be incomplete."


def _turbidity_rise(rain: RainContext) -> tuple[str, str]:
    t = rain.threshold_inches
    if rain.window_total_inches is None:
        return "flag_for_review", (
            f"Turbidity rise with no rain reading on any day checked ({_fmt_days(rain)}; daily totals). "
            "Rain cannot be confirmed or ruled out, so this is not treated as storm runoff."
        )
    total = rain.window_total_inches
    if total >= t:
        return "storm_runoff", (
            f"Turbidity rise with {total:.2f} in of rain recorded ({_fmt_days(rain)}; daily totals), "
            f"at or above the {t:.2f} in threshold; consistent with storm runoff." + _missing_note(rain)
        )
    return "flag_for_review", (
        f"Turbidity rise; rain recorded below threshold ({total:.2f} in < {t:.2f} in) "
        f"({_fmt_days(rain)}; daily totals). Not treated as storm runoff." + _missing_note(rain)
    )


def _matches(storm: SensorEvent, event: SensorEvent, window: pd.Timedelta) -> bool:
    """Storm turbidity run overlaps the event, or starts within `window` of it."""
    t0, t1 = _utc(storm.timestamp), _utc(storm.end_timestamp)
    e0, e1 = _utc(event.timestamp), _utc(event.end_timestamp)
    overlaps = t0 <= e1 and e0 <= t1
    starts_near = e0 - window <= t0 <= e1 + window
    return overlaps or starts_near


def _other(
    event: SensorEvent, storms: list[SensorEvent], rises: list[SensorEvent], window: pd.Timedelta
) -> tuple[str, str]:
    name = {"ph": "pH"}.get(event.parameter, event.parameter)
    match = next((s for s in storms if _matches(s, event, window)), None)
    if match is not None:
        kind = " (dilution)" if event.parameter == "conductivity" and event.direction == "drop" else ""
        return "storm_runoff", (
            f"{name} {event.direction}{kind} coincides with a turbidity rise starting "
            f"{_utc(match.timestamp):%Y-%m-%d %H:%M} UTC that met the rain threshold; "
            "consistent with the storm pattern."
        )
    if any(_matches(r, event, window) for r in rises):
        return "flag_for_review", (
            f"{name} {event.direction} coincides with a turbidity rise, but rain above the threshold "
            "is not confirmed, so there is no matching turbidity-and-rain pattern; "
            "it is not treated as storm runoff."
        )
    return "flag_for_review", (
        f"{name} {event.direction} has no matching turbidity-and-rain pattern; "
        "it is not treated as storm runoff."
    )


def classify(
    events: list[SensorEvent],
    daily: pd.DataFrame,
    threshold_inches: float,
    window_hours: int,
    station_id: str,
    data_origin: str,
    match_window: str = "6h",
) -> StormFilterResult:
    """Label each event storm_runoff or flag_for_review; overall is storm_runoff only if all are."""
    window = pd.Timedelta(match_window)
    labels: dict[int, tuple[str, str]] = {}
    storms: list[SensorEvent] = []
    rises: list[SensorEvent] = []
    for i, ev in enumerate(events):
        if ev.parameter == "turbidity" and ev.direction == "rise":
            rain = rain_context(daily, ev.timestamp, threshold_inches, window_hours, station_id)
            labels[i] = _turbidity_rise(rain)
            rises.append(ev)
            if labels[i][0] == "storm_runoff":
                storms.append(ev)
    for i, ev in enumerate(events):
        if i not in labels:
            labels[i] = _other(ev, storms, rises, window)

    decisions = [StormDecision(event=ev, label=labels[i][0], reason=labels[i][1]) for i, ev in enumerate(events)]
    if not decisions:
        overall = "no_anomaly"
    elif all(d.label == "storm_runoff" for d in decisions):
        overall = "storm_runoff"
    else:
        overall = "flag_for_review"
    rain = None
    if events:
        first = min(events, key=lambda e: _utc(e.timestamp))
        rain = rain_context(daily, first.timestamp, threshold_inches, window_hours, station_id)
    return StormFilterResult(overall=overall, decisions=decisions, rain=rain, data_origin=data_origin)
