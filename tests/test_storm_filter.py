import math
import re
from datetime import date, datetime, timedelta, timezone

import pandas as pd
import pytest

from backend.app.config import ConfigError, get_settings
from backend.app.data import baselines
from backend.app.detection.sensor_anomaly import detect_anomalies
from backend.app.detection.storm_filter import classify, rain_context
from backend.app.schemas import SensorEvent, StormFilterResult
from backend.app.simulation.spill_injector import inject_spill, spill_params_from_settings

STATION = "GHCND:TEST0001"
REAL = "real observed data, not simulated"
SIM = "simulated spill injected into real baseline"
BANNED = ("dumping", "caused", "responsible")


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def daily(values: dict[date, float]) -> pd.DataFrame:
    days = sorted(values)
    return pd.DataFrame({
        "date": pd.Series(days, dtype="object"),
        "prcp_inches": [float(values[d]) for d in days],
        "attributes": "",
        "station": STATION,
    })


def ev(parameter: str, start: datetime, direction: str = "rise", hours: float = 4.0, sim: bool = False) -> SensorEvent:
    return SensorEvent(
        timestamp=start, parameter=parameter, score=6.0, value=9.0, baseline_mean=1.5, baseline_std=0.2,
        direction=direction, peak_timestamp=start + timedelta(hours=1),
        end_timestamp=start + timedelta(hours=hours), n_points=int(hours * 4) + 1, includes_simulated=sim,
    )


def check_wording(result: StormFilterResult) -> None:
    for d in result.decisions:
        low = d.reason.lower()
        for word in BANNED:
            assert word not in low, d.reason
        assert re.findall(r"no rain(?! reading)", low) == [], d.reason


def run(events, rain, **kw) -> StormFilterResult:
    result = classify(events, daily(rain), 0.5, 24, STATION, kw.pop("data_origin", REAL), **kw)
    check_wording(result)
    return result


STORM_RAIN = {date(2026, 9, 21): 0.28, date(2026, 9, 22): 0.95, date(2026, 9, 23): 0.13}


# ---- rain_context ----------------------------------------------------------------------------
def test_rain_context_uses_event_day_and_day_before():
    rc = rain_context(daily(STORM_RAIN), utc(2026, 9, 22, 10), 0.5, 24, STATION)
    assert rc.station_id == STATION and rc.resolution == "daily"
    assert [d.day for d in rc.days] == [date(2026, 9, 21), date(2026, 9, 22)]
    assert rc.window_total_inches == pytest.approx(1.23)
    assert rc.exceeded is True and rc.missing_days == 0 and rc.threshold_inches == 0.5


def test_rain_context_missing_days_are_none_not_zero():
    rain = {date(2026, 9, 21): math.nan, date(2026, 9, 22): 0.2}
    rc = rain_context(daily(rain), utc(2026, 9, 22, 10), 0.5, 24, STATION)
    assert [d.prcp_inches for d in rc.days] == [None, 0.2]
    assert rc.window_total_inches == pytest.approx(0.2) and rc.exceeded is False and rc.missing_days == 1

    none = rain_context(daily({date(2026, 9, 21): math.nan}), utc(2026, 9, 22, 10), 0.5, 24, STATION)
    assert none.window_total_inches is None and none.exceeded is None and none.missing_days == 2
    assert all(d.prcp_inches is None for d in none.days)


def test_rain_context_48h_uses_three_days():
    rc = rain_context(daily(STORM_RAIN), utc(2026, 9, 23, 1), 0.5, 48, STATION)
    assert [d.day for d in rc.days] == [date(2026, 9, 21), date(2026, 9, 22), date(2026, 9, 23)]
    assert rc.window_total_inches == pytest.approx(1.36)


# ---- classify: turbidity rises ---------------------------------------------------------------
def test_turbidity_rise_with_rain_over_threshold_is_storm_runoff():
    r = run([ev("turbidity", utc(2026, 9, 22, 9))], STORM_RAIN)
    (d,) = r.decisions
    assert d.label == "storm_runoff" and r.overall == "storm_runoff"
    assert "1.23" in d.reason and "daily totals" in d.reason
    assert any(s in d.reason for s in ("2026-09-22", "09-22", "Sep 22", "September 22"))
    assert r.rain.exceeded is True and r.data_origin == REAL


def test_turbidity_rise_below_threshold_is_flagged():
    r = run([ev("turbidity", utc(2026, 9, 22, 9))], {date(2026, 9, 21): 0.1, date(2026, 9, 22): 0.2})
    (d,) = r.decisions
    assert d.label == "flag_for_review" and "below threshold" in d.reason
    assert r.overall == "flag_for_review"


def test_turbidity_rise_with_no_rain_reading_is_flagged():
    rain = {date(2026, 9, 21): math.nan, date(2026, 9, 22): math.nan}
    r = run([ev("turbidity", utc(2026, 9, 22, 9))], rain)
    (d,) = r.decisions
    assert d.label == "flag_for_review" and "no rain reading" in d.reason.lower()
    assert r.rain.exceeded is None and r.rain.window_total_inches is None


def test_partial_missing_is_noted():
    over = run([ev("turbidity", utc(2026, 9, 22, 9))], {date(2026, 9, 21): math.nan, date(2026, 9, 22): 0.95})
    assert over.decisions[0].label == "storm_runoff"
    assert "reading" in over.decisions[0].reason.lower() and "1" in over.decisions[0].reason
    assert over.rain.missing_days == 1
    under = run([ev("turbidity", utc(2026, 9, 22, 9))], {date(2026, 9, 21): math.nan, date(2026, 9, 22): 0.1})
    assert under.decisions[0].label == "flag_for_review"
    assert "below threshold" in under.decisions[0].reason and "reading" in under.decisions[0].reason.lower()


def test_daily_window_is_event_day_and_day_before_only():
    # Heavy rain two days before the event is outside a 24 h window.
    rain = {date(2026, 9, 21): 0.95, date(2026, 9, 22): math.nan, date(2026, 9, 23): math.nan}
    r = run([ev("turbidity", utc(2026, 9, 23, 1))], rain)
    assert r.decisions[0].label == "flag_for_review" and "no rain reading" in r.decisions[0].reason.lower()
    assert [d.day for d in r.rain.days] == [date(2026, 9, 22), date(2026, 9, 23)]


def test_turbidity_drop_is_not_storm_runoff_on_its_own():
    r = run([ev("turbidity", utc(2026, 9, 22, 9), direction="drop")], STORM_RAIN)
    assert r.decisions[0].label == "flag_for_review" and r.overall == "flag_for_review"


# ---- classify: conductivity / pH -------------------------------------------------------------
def labels(r: StormFilterResult) -> dict[str, str]:
    return {d.event.parameter: d.label for d in r.decisions}


def test_conductivity_overlapping_storm_turbidity_is_storm_runoff():
    turb = ev("turbidity", utc(2026, 9, 22, 8), hours=12)
    cond = ev("conductivity", utc(2026, 9, 22, 19), direction="drop")  # starts 11 h later, but overlaps
    r = run([turb, cond], STORM_RAIN)
    assert labels(r) == {"turbidity": "storm_runoff", "conductivity": "storm_runoff"}
    assert r.overall == "storm_runoff"


def test_conductivity_within_match_window_before_turbidity_is_storm_runoff():
    turb = ev("turbidity", utc(2026, 9, 22, 8), hours=4)
    cond = ev("conductivity", utc(2026, 9, 22, 5), direction="drop", hours=1)  # ends 2 h before turbidity
    assert labels(run([cond, turb], STORM_RAIN))["conductivity"] == "storm_runoff"


def test_conductivity_and_ph_far_from_turbidity_are_flagged():
    turb = ev("turbidity", utc(2026, 9, 22, 8), hours=4)
    cond = ev("conductivity", utc(2026, 9, 23, 6), hours=2)  # 14 h after the turbidity run ends
    ph = ev("ph", utc(2026, 9, 23, 6), direction="drop", hours=2)
    r = run([turb, cond, ph], STORM_RAIN, match_window="6h")
    assert labels(r) == {"turbidity": "storm_runoff", "conductivity": "flag_for_review", "ph": "flag_for_review"}
    assert r.overall == "flag_for_review"
    for d in r.decisions:
        if d.event.parameter != "turbidity":
            assert "not treated as storm runoff" in d.reason


def test_conductivity_alone_is_flagged_even_with_rain():
    r = run([ev("conductivity", utc(2026, 9, 22, 9), direction="drop")], STORM_RAIN)
    assert r.decisions[0].label == "flag_for_review"


def test_conductivity_next_to_flagged_turbidity_is_flagged():
    rain = {date(2026, 9, 21): math.nan, date(2026, 9, 22): math.nan}
    turb = ev("turbidity", utc(2026, 9, 22, 8))
    cond = ev("conductivity", utc(2026, 9, 22, 8), direction="drop")
    assert labels(run([turb, cond], rain)) == {"turbidity": "flag_for_review", "conductivity": "flag_for_review"}


# ---- classify: result shape ------------------------------------------------------------------
def test_no_events_is_no_anomaly():
    r = run([], STORM_RAIN)
    assert r.overall == "no_anomaly" and r.decisions == [] and r.rain is None
    assert r.simulated is True and r.requires_human_review is True


def test_result_flags_and_rain_for_earliest_event():
    later = ev("turbidity", utc(2026, 9, 25, 9))
    first = ev("turbidity", utc(2026, 9, 22, 9))
    r = run([later, first], STORM_RAIN, data_origin=SIM)
    assert isinstance(r, StormFilterResult)
    assert r.simulated is True and r.requires_human_review is True and r.data_origin == SIM
    assert r.rain.days[-1].day == date(2026, 9, 22)
    assert len(r.decisions) == 2


# ---- scenarios on the committed snapshot -----------------------------------------------------
@pytest.fixture(scope="module")
def settings():
    try:
        return get_settings()
    except ConfigError:
        pytest.skip("no .env")


@pytest.fixture(scope="module")
def snap():
    return baselines.load_snapshot()


def _classify(settings, events, snap, origin):
    r = classify(events, snap.daily_prcp, settings.rain_threshold_inches, settings.rain_window_hours,
                 settings.noaa_station_id, origin)
    check_wording(r)
    return r


def test_storm_case_is_storm_runoff_on_unmodified_real_data(settings, snap):
    before = snap.sensors.copy()
    d = settings.storm_case_date
    day = utc(d.year, d.month, d.day)
    events = detect_anomalies(snap.sensors, start=day, end=day + timedelta(hours=48))
    pd.testing.assert_frame_equal(snap.sensors, before)
    assert not [c for c in snap.sensors.columns if c.endswith("_simulated")]
    assert events and not any(e.includes_simulated for e in events)
    r = _classify(settings, events, snap, REAL)
    turb = [d for d in r.decisions if d.event.parameter == "turbidity" and d.event.direction == "rise"]
    assert any(d.label == "storm_runoff" for d in turb)
    assert r.overall == "storm_runoff" and r.data_origin == REAL
    assert r.rain.exceeded is True


def test_storm_precursor_spike_under_low_rain_is_flagged(settings, snap):
    # A one-reading turbidity spike on the evening of 09-21 only has 09-20 (no reading) and 09-21
    # (0.28 in) in its daily window, so the strict daily rule flags it rather than calling it storm runoff.
    events = detect_anomalies(snap.sensors, start=utc(2026, 9, 21), end=utc(2026, 9, 22))
    r = _classify(settings, events, snap, REAL)
    turb = [d for d in r.decisions if d.event.parameter == "turbidity" and d.event.direction == "rise"]
    assert turb and all(d.label == "flag_for_review" for d in turb)
    assert all("below threshold" in d.reason for d in turb)


def test_spill_case_is_flagged_for_review(settings, snap):
    params = spill_params_from_settings()
    ts = settings.scenario_timestamp
    assert params.start == ts
    injected = inject_spill(snap.sensors, params).frame
    events = detect_anomalies(injected, baseline_end=ts, start=ts, end=ts + timedelta(hours=24))
    found = {(e.parameter, e.direction) for e in events}
    assert {("conductivity", "rise"), ("ph", "drop"), ("turbidity", "rise")} <= found
    assert all(e.includes_simulated for e in events)
    r = _classify(settings, events, snap, SIM)
    turb = [d for d in r.decisions if d.event.parameter == "turbidity" and d.event.direction == "rise"]
    assert turb and all(d.label == "flag_for_review" for d in turb)
    assert all("no rain reading" in d.reason.lower() for d in turb)
    assert not any(d.label == "storm_runoff" for d in r.decisions)
    assert r.overall == "flag_for_review" and r.rain.exceeded is None


def test_real_october_event_without_rain_reading_is_not_storm_runoff(settings, snap):
    # Natural runoff-like event ~2026-10-11 09:00Z; the rain gauge has no reading those days.
    events = detect_anomalies(snap.sensors, start=utc(2026, 10, 11), end=utc(2026, 10, 12, 12))
    r = _classify(settings, events, snap, REAL)
    turb = [d for d in r.decisions if d.event.parameter == "turbidity" and d.event.direction == "rise"]
    assert turb
    assert all("no rain reading" in d.reason.lower() and d.label == "flag_for_review" for d in turb)
    assert r.overall != "storm_runoff"
