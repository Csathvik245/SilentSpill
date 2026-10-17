"""Tests for backend.app.detection.fusion, written from the step 6 contract.

Unit tests use hand-built SensorEvent / VisionSummary objects. The scenario tests run the real
committed snapshot (spill and 2026-09-22 storm) against rendered clips, as in test_storm_filter.py.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from functools import lru_cache

import networkx as nx
import pytest

from backend.app.config import ConfigError, get_settings
from backend.app.data import baselines
from backend.app.detection import fusion
from backend.app.detection.fusion import DEFAULT_MAX_DELAY, fuse
from backend.app.detection.sensor_anomaly import detect_anomalies
from backend.app.detection.storm_filter import classify
from backend.app.detection.vision import analyze_frames
from backend.app.schemas import (FusedEvent, FusionResult, SensorEvent, StormDecision, StormFilterResult,
                                 VisionDetection, VisionFrame, VisionSummary)
from backend.app.simulation import plume_renderer as pr
from backend.app.simulation.spill_injector import inject_spill, spill_params_from_settings

BANNED = ("caused", "responsible", "dumping")
SIM = "simulated spill injected into real baseline"
REAL = "real observed data, not simulated"
T0 = datetime(2026, 10, 9, 20, tzinfo=timezone.utc)
N = 90  # 45 min of scenario time; the plume starts at frame 20


def utc(*a) -> datetime:
    return datetime(*a, tzinfo=timezone.utc)


def ev(parameter: str, start: datetime, hours: float = 1.0, direction: str = "rise") -> SensorEvent:
    return SensorEvent(
        timestamp=start, parameter=parameter, score=6.0, value=9.0, baseline_mean=1.5, baseline_std=0.2,
        direction=direction, peak_timestamp=start + timedelta(minutes=30),
        end_timestamp=start + timedelta(hours=hours), n_points=int(hours * 4) + 1, includes_simulated=True,
    )


def storm(events: list[SensorEvent], label: str = "flag_for_review") -> StormFilterResult:
    labels = [label] * len(events) if isinstance(label, str) else label
    decisions = [StormDecision(event=e, label=l, reason="test decision") for e, l in zip(events, labels)]
    if not decisions:
        overall = "no_anomaly"
    elif all(l == "storm_runoff" for l in labels):
        overall = "storm_runoff"
    else:
        overall = "flag_for_review"
    return StormFilterResult(overall=overall, decisions=decisions, rain=None, data_origin=SIM)


def vis(first: datetime | None = T0, minutes: int = 60, conf: float = 0.5) -> VisionSummary:
    """A summary with plume frames every 30 s from first to first + minutes (None -> no plume)."""
    frames, dets = [], []
    n = 2 * minutes + 1 if first else 10
    start = first or T0
    for i in range(n):
        ts = start + timedelta(seconds=30 * i)
        hit = first is not None
        frames.append(VisionFrame(frame_index=i, timestamp=ts, plume_detected=hit,
                                  area_fraction=0.015 if hit else 0.0, confidence=conf if hit else 0.0))
        if hit:
            dets.append(VisionDetection(frame_index=i, time_s=i / 10, timestamp=ts, label="plume",
                                        confidence=conf, area_fraction=0.015, bbox=(1, 2, 3, 4), detector="hsv"))
    return VisionSummary(clip="test", clip_kind="spill" if first else "glare", frames_analyzed=n, frames=frames,
                         plume_detections=dets, first_plume=dets[0].timestamp if dets else None,
                         last_plume=dets[-1].timestamp if dets else None, debris_status="not run")


def check(r: FusionResult) -> FusionResult:
    assert isinstance(r, FusionResult)
    assert r.simulated is True and r.requires_human_review is True
    assert 0.0 <= r.confidence <= 1.0
    assert r.reasons and all(isinstance(s, str) and s for s in r.reasons)
    for s in r.reasons:
        low = s.lower()
        for word in BANNED:
            assert word not in low, s
    for e in r.events:
        assert isinstance(e, FusedEvent) and 0.0 <= e.confidence <= 1.0
    return r


def mentions(r: FusionResult, *words: str) -> bool:
    text = " ".join(r.reasons).lower()
    return all(w in text for w in words)


def mentions_delay(r: FusionResult) -> bool:
    text = " ".join(r.reasons).lower()
    return ("delay" in text or "allowance" in text) and f"{r.max_delay_hours:.1f} h" in text


# ---- delay window ----------------------------------------------------------------------------
def test_default_max_delay():
    assert DEFAULT_MAX_DELAY == timedelta(hours=3)


def test_event_2_5h_after_last_plume_matches():
    v = vis(T0, 60)  # plume 20:00-21:00
    e = ev("conductivity", v.last_plume + timedelta(hours=2.5))
    r = check(fuse([e], storm([e]), v, max_delay=timedelta(hours=3)))
    assert r.classification == "incident" and r.max_delay_hours == pytest.approx(3.0)
    assert mentions_delay(r)


def test_event_3_5h_after_last_plume_does_not_match():
    v = vis(T0, 60)
    e = ev("conductivity", v.last_plume + timedelta(hours=3.5))
    r = check(fuse([e], storm([e]), v, max_delay=timedelta(hours=3)))
    assert r.classification == "flag_for_review" and r.single_source is True
    assert r.confidence <= 0.4


def test_event_before_plume_within_delay_matches_either_order():
    v = vis(T0, 60)
    near = ev("turbidity", v.first_plume - timedelta(hours=3.5), hours=1.0)  # ends 2.5 h before
    far = ev("turbidity", v.first_plume - timedelta(hours=4.5), hours=1.0)   # ends 3.5 h before
    assert check(fuse([near], storm([near]), v)).classification == "incident"
    assert check(fuse([far], storm([far]), v)).classification == "flag_for_review"


def test_long_event_overlapping_window_matches():
    v = vis(T0, 60)
    e = ev("ph", v.first_plume - timedelta(hours=10), hours=12, direction="drop")  # spans the plume
    assert check(fuse([e], storm([e]), v)).classification == "incident"


def test_max_delay_parameter_is_used():
    v = vis(T0, 60)
    e = ev("conductivity", v.last_plume + timedelta(hours=2.5))
    r = check(fuse([e], storm([e]), v, max_delay=timedelta(hours=2)))
    assert r.classification == "flag_for_review" and r.max_delay_hours == pytest.approx(2.0)


# ---- classification branches -----------------------------------------------------------------
def test_incident_fields_and_confidence():
    v = vis(T0, 60, conf=0.5)
    events = [ev("conductivity", T0), ev("ph", T0, direction="drop"), ev("conductivity", T0 + timedelta(hours=1))]
    r = check(fuse(events, storm(events), v))
    assert r.classification == "incident" and r.single_source is False
    assert set(r.sources) == {"sensor", "vision"}
    # 2 distinct parameters, max plume confidence 0.5
    assert r.confidence == pytest.approx(min(0.95, 0.5 + 0.1 * 2 + 0.2 * 0.5))
    inc = [e for e in r.events if set(e.sources) == {"sensor", "vision"}]
    assert inc
    e = inc[0]
    assert e.single_source is False and e.vision_detections
    assert {s.parameter for s in e.sensor_events} == {"conductivity", "ph"}
    assert e.storm_filter == "flag_for_review"
    assert mentions_delay(r)


def test_incident_confidence_capped():
    v = vis(T0, 30, conf=1.0)
    events = [ev("conductivity", T0), ev("ph", T0, direction="drop"), ev("turbidity", T0)]
    r = check(fuse(events, storm(events), v))
    assert r.classification == "incident" and r.confidence == pytest.approx(0.95)


def test_storm_runoff_without_plume():
    events = [ev("turbidity", T0, hours=6), ev("conductivity", T0 + timedelta(hours=1), direction="drop")]
    s = storm(events, "storm_runoff")
    for v in (None, vis(None)):
        r = check(fuse(events, s, v))
        assert r.classification == "storm_runoff" and r.single_source is True
        assert 0.6 <= r.confidence <= 0.95
        assert r.events and all(e.storm_filter == "storm_runoff" for e in r.events)
        assert "vision" not in r.sources


def test_vision_only_is_flag_for_review():
    for s in (None, storm([])):
        r = check(fuse([], s, vis(T0, 60, conf=1.0)))
        assert r.classification == "flag_for_review" and r.single_source is True
        assert r.sources == ["vision"]
        assert 0.0 < r.confidence <= 0.4
        assert mentions(r, "single")
        assert all(e.storm_filter == "not_applicable" for e in r.events)


def test_sensor_only_is_flag_for_review():
    events = [ev("conductivity", T0), ev("ph", T0, direction="drop")]
    for v in (None, vis(None)):
        r = check(fuse(events, storm(events), v))
        assert r.classification == "flag_for_review" and r.single_source is True
        assert r.sources == ["sensor"]
        assert 0.0 < r.confidence <= 0.4
        assert mentions(r, "single")
        assert r.events and all(e.storm_filter == "flag_for_review" for e in r.events)


def test_mixed_storm_and_review_events_without_plume_is_flagged():
    events = [ev("turbidity", T0, hours=6), ev("ph", T0 + timedelta(days=1), direction="drop")]
    s = storm(events, ["storm_runoff", "flag_for_review"])
    r = check(fuse(events, s, None))
    assert r.classification == "flag_for_review" and r.confidence <= 0.4


def test_plume_with_only_non_matching_events_is_flagged():
    v = vis(T0, 60)
    far = ev("conductivity", T0 + timedelta(days=2))
    r = check(fuse([far], storm([far]), v))
    assert r.classification == "flag_for_review" and r.single_source is True and r.confidence <= 0.4


def test_plume_with_only_storm_events_is_not_incident():
    v = vis(T0, 60)
    e = ev("turbidity", T0, hours=4)
    r = check(fuse([e], storm([e], "storm_runoff"), v))
    assert r.classification != "incident"


def test_nothing_is_no_incident():
    for s, v in ((None, None), (storm([]), None), (None, vis(None)), (storm([]), vis(None))):
        r = check(fuse([], s, v))
        assert r.classification == "no_incident" and r.confidence == 0.0


def test_incident_beats_single_source_confidence():
    v = vis(T0, 60, conf=0.1)
    e = ev("ph", T0, direction="drop")
    inc = check(fuse([e], storm([e]), v))
    sensor_only = check(fuse([e], storm([e]), None))
    vision_only = check(fuse([], None, vis(T0, 60, conf=1.0)))
    assert inc.classification == "incident"
    assert inc.confidence > sensor_only.confidence and inc.confidence > vision_only.confidence
    assert max(sensor_only.confidence, vision_only.confidence) <= 0.4


# ---- camera_gauge_max_delay ------------------------------------------------------------------
def test_camera_gauge_max_delay_from_graph_and_fallback():
    G = nx.DiGraph()
    G.add_node(1, length_km=3.16, velocity_fps=0.962)
    G.add_node(2, length_km=3.16, velocity_fps=None)
    hours = 3160 / (0.962 * 0.3048) / 3600
    assert fusion.camera_gauge_max_delay(G, 1).total_seconds() / 3600 == pytest.approx(hours, rel=1e-3)
    assert fusion.camera_gauge_max_delay(G, 2) == DEFAULT_MAX_DELAY      # no velocity
    assert fusion.camera_gauge_max_delay(nx.DiGraph(), 99) == DEFAULT_MAX_DELAY  # unknown reach


# ---- scenarios on the committed snapshot and rendered clips ----------------------------------
@pytest.fixture(scope="module")
def settings():
    try:
        return get_settings()
    except ConfigError:
        pytest.skip("no .env")


@pytest.fixture(scope="module")
def snap():
    return baselines.load_snapshot()


@lru_cache(maxsize=None)
def _vision(kind: str, ts: datetime, seed: int) -> VisionSummary:
    clip = pr.render_clip(kind, scenario_timestamp=ts, seed=seed, n_frames=N)
    return analyze_frames(clip.frames, clip.timestamps, clip.river_mask, clip_name=f"{kind}_test", clip_kind=kind)


@pytest.fixture(scope="module")
def spill_case(settings, snap):
    ts = settings.scenario_timestamp
    injected = inject_spill(snap.sensors, spill_params_from_settings()).frame
    events = detect_anomalies(injected, baseline_end=ts, start=ts, end=ts + timedelta(hours=24))
    r = classify(events, snap.daily_prcp, settings.rain_threshold_inches, settings.rain_window_hours,
                 settings.noaa_station_id, SIM)
    return events, r


@pytest.fixture(scope="module")
def storm_case(settings, snap):
    d = settings.storm_case_date
    day = utc(d.year, d.month, d.day)
    events = detect_anomalies(snap.sensors, start=day, end=day + timedelta(hours=48))
    r = classify(events, snap.daily_prcp, settings.rain_threshold_inches, settings.rain_window_hours,
                 settings.noaa_station_id, REAL)
    return events, r


def visions(settings, kind):
    return _vision(kind, settings.scenario_timestamp, settings.random_seed)


def test_spill_clip_and_spill_sensors_is_incident(settings, spill_case):
    events, r = spill_case
    assert events and r.overall == "flag_for_review"
    v = visions(settings, "spill")
    assert v.first_plume is not None and v.first_plume >= settings.scenario_timestamp
    res = check(fuse(events, r, v))
    assert res.classification == "incident"
    assert res.single_source is False and set(res.sources) == {"sensor", "vision"}
    assert res.confidence > 0.4
    assert any(set(e.sources) == {"sensor", "vision"} and e.sensor_events for e in res.events)


@pytest.mark.parametrize("kind", pr.NEGATIVE_KINDS)
def test_negative_clip_alone_is_no_incident(settings, kind):
    v = visions(settings, kind)
    assert v.first_plume is None and v.plume_detections == []
    assert check(fuse([], None, v)).classification == "no_incident"


@pytest.mark.parametrize("kind", pr.NEGATIVE_KINDS)
def test_negative_clip_with_spill_sensors_is_not_incident(settings, spill_case, kind):
    events, r = spill_case
    res = check(fuse(events, r, visions(settings, kind)))
    assert res.classification == "flag_for_review"
    assert res.single_source is True and res.confidence <= 0.4
    assert "vision" not in res.sources


def test_storm_case_stays_storm_runoff(settings, storm_case):
    events, r = storm_case
    assert r.overall == "storm_runoff"
    res = check(fuse(events, r, None))
    assert res.classification == "storm_runoff" and res.single_source is True


@pytest.mark.parametrize("kind", pr.NEGATIVE_KINDS)
def test_storm_case_with_negative_clip_stays_storm_runoff(settings, storm_case, kind):
    events, r = storm_case
    res = check(fuse(events, r, visions(settings, kind)))
    assert res.classification == "storm_runoff"
