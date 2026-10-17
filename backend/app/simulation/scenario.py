"""The two deterministic demo cases, run end to end from local data only.

Spill: a simulated spill injected into the real gauge baseline at SCENARIO_TIMESTAMP, plus the
simulated camera clip. Storm: the real observed 2026-09-22 storm, unmodified, with no clip.
Both read the committed baseline snapshot and the cached Roboflow responses; nothing here uses
the network. Same settings and seed give the same result.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Literal

import networkx as nx
import pandas as pd

from ..config import Settings, get_settings
from ..data import nhd_graph
from ..data.baselines import load_snapshot
from ..data.facilities import find_candidate_facilities
from ..detection import fusion, storm_filter, vision
from ..detection.sensor_anomaly import detect_anomalies
from ..schemas import CandidateFacility, FusionResult, SensorEvent, StormFilterResult, VisionSummary
from .plume_renderer import render_clip
from .spill_injector import InjectionResult, inject_spill, spill_params_from_settings

SPILL_ORIGIN = "simulated spill injected into real baseline"
STORM_ORIGIN = "real observed data, not simulated"
DEFAULT_MAX_KM = 50.0
DETECTION_HOURS = 24  # spill: detection window after the scenario timestamp
STORM_HOURS = 48  # storm: the storm day and the day after


@dataclass
class ScenarioResult:
    case: Literal["spill", "storm"]
    scenario_timestamp: datetime  # spill start, or 00:00Z on the storm day
    data_origin: str
    reach_nhdplusid: int
    max_km: float
    sensors: pd.DataFrame  # gauge series used (injected rows flagged for the spill)
    sensor_events: list[SensorEvent]
    storm: StormFilterResult
    vision: VisionSummary | None
    fusion: FusionResult
    max_delay: timedelta
    anomalous_parameters: list[str]
    candidates: list[CandidateFacility] = field(default_factory=list)
    injection: InjectionResult | None = None


def _anomalous(events: list[SensorEvent], storm: StormFilterResult) -> list[str]:
    """Parameters with a gauge event the storm filter did not explain as storm runoff."""
    explained = {id(d.event) for d in storm.decisions if d.label == "storm_runoff"}
    return sorted({e.parameter for e in events if id(e) not in explained} & {"conductivity", "turbidity", "ph"})


def _spill_vision(settings: Settings) -> VisionSummary:
    clip = render_clip("spill", settings.scenario_timestamp, settings.random_seed)
    debris, status = vision.load_debris("spill")
    return vision.analyze_frames(
        clip.frames, clip.timestamps, clip.river_mask, clip_name="spill", clip_kind="spill",
        debris=debris, debris_status=status,
    )


def run_spill_scenario(
    settings: Settings | None = None,
    G: nx.DiGraph | None = None,
    max_km: float = DEFAULT_MAX_KM,
    with_vision: bool = True,
    with_candidates: bool = True,
) -> ScenarioResult:
    """Simulated spill at SCENARIO_TIMESTAMP: inject, detect, storm filter, camera, fusion, candidates."""
    s = settings or get_settings()
    snap = load_snapshot()
    ts = s.scenario_timestamp
    injection = inject_spill(snap.sensors, spill_params_from_settings(s))
    events = detect_anomalies(
        injection.frame, baseline_end=ts, start=ts, end=ts + timedelta(hours=DETECTION_HOURS)
    )
    storm = storm_filter.classify(
        events, snap.daily_prcp, s.rain_threshold_inches, s.rain_window_hours, s.noaa_station_id, SPILL_ORIGIN
    )
    G = G if G is not None else nhd_graph.load_graph()
    max_delay = fusion.camera_gauge_max_delay(G, s.reach_nhdplusid)
    seen = _spill_vision(s) if with_vision else None
    fused = fusion.fuse(events, storm, seen, max_delay)
    anomalous = _anomalous(events, storm)
    candidates: list[CandidateFacility] = []
    if with_candidates and fused.classification in ("incident", "flag_for_review"):
        candidates = find_candidate_facilities(
            G, s.reach_nhdplusid, max_km, anomalous=anomalous or None, reference_date=ts.date()
        )
    return ScenarioResult(
        case="spill", scenario_timestamp=ts, data_origin=SPILL_ORIGIN, reach_nhdplusid=s.reach_nhdplusid,
        max_km=max_km, sensors=injection.frame, sensor_events=events, storm=storm, vision=seen,
        fusion=fused, max_delay=max_delay, anomalous_parameters=anomalous, candidates=candidates,
        injection=injection,
    )


def run_storm_scenario(
    settings: Settings | None = None,
    G: nx.DiGraph | None = None,
    max_km: float = DEFAULT_MAX_KM,
) -> ScenarioResult:
    """The real 2026-09-22 storm, unmodified. Storm runoff gets no candidate facility list."""
    s = settings or get_settings()
    snap = load_snapshot()
    d = s.storm_case_date
    day = datetime(d.year, d.month, d.day, tzinfo=timezone.utc)
    events = detect_anomalies(snap.sensors, start=day, end=day + timedelta(hours=STORM_HOURS))
    storm = storm_filter.classify(
        events, snap.daily_prcp, s.rain_threshold_inches, s.rain_window_hours, s.noaa_station_id, STORM_ORIGIN
    )
    G = G if G is not None else nhd_graph.load_graph()
    max_delay = fusion.camera_gauge_max_delay(G, s.reach_nhdplusid)
    fused = fusion.fuse(events, storm, None, max_delay)
    anomalous = _anomalous(events, storm)
    candidates: list[CandidateFacility] = []
    if fused.classification in ("incident", "flag_for_review"):
        candidates = find_candidate_facilities(
            G, s.reach_nhdplusid, max_km, anomalous=anomalous or None, reference_date=d
        )
    return ScenarioResult(
        case="storm", scenario_timestamp=day, data_origin=STORM_ORIGIN, reach_nhdplusid=s.reach_nhdplusid,
        max_km=max_km, sensors=snap.sensors, sensor_events=events, storm=storm, vision=None,
        fusion=fused, max_delay=max_delay, anomalous_parameters=anomalous, candidates=candidates,
    )
