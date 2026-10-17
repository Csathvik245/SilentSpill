"""All pydantic models. Top-level responses carry the simulated and human-review flags."""
from __future__ import annotations

import math
from datetime import date, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field, computed_field

BANNER = (
    "SIMULATED DEMONSTRATION. The spill, plume footage and sensor spike are synthetic. "
    "This draft requires human review before any action."
)

SensorParameter = Literal["conductivity", "turbidity", "ph", "discharge"]
LocationType = Literal["outfall", "facility"]


def _simulated_default() -> bool:
    # Imported lazily so schemas stay importable without a complete .env (tests, docs tooling).
    from .config import ConfigError, get_settings

    try:
        return get_settings().simulation_mode
    except ConfigError:
        return True


class ResponseBase(BaseModel):
    """Every API response and agent output inherits these flags."""

    simulated: bool = Field(default_factory=_simulated_default)
    requires_human_review: Literal[True] = True


# ---- GeoJSON (plain dicts so the frontend gets standard GeoJSON) -----------------------------
class FeatureCollection(BaseModel):
    type: Literal["FeatureCollection"] = "FeatureCollection"
    features: list[dict[str, Any]] = []


# ---- River network ---------------------------------------------------------------------------
class TravelTime(BaseModel):
    """River travel time over a path of reaches.

    hours is set only when every reach has a velocity. When some reaches lack one,
    travel_time_hours_min sums the reaches that have it: a lower bound, never an estimate.
    """

    hours: float | None
    travel_time_hours_min: float | None = None
    is_lower_bound: bool = False
    km_with_velocity: float = 0.0
    km_total: float = 0.0
    missing_reach_count: int = 0
    reaches_missing_velocity: list[int] = []
    reason: str | None = None  # set whenever hours is None

    @computed_field  # type: ignore[prop-decorator]
    @property
    def display(self) -> str:
        """Wording for the report and API, e.g. '≥ 2.4 h (velocity available for 46.1 of 47.3 km)'."""
        if self.hours is not None:
            return f"{self.hours:.1f} h"
        if self.travel_time_hours_min is None:
            return "unavailable (no velocity data on this path)"
        # Round the bound down so the displayed '≥' value is never larger than the computed one.
        bound = math.floor(self.travel_time_hours_min * 10) / 10
        return (
            f"≥ {bound:.1f} h (velocity available for {self.km_with_velocity:.1f} "
            f"of {self.km_total:.1f} km)"
        )


class UpstreamReach(BaseModel):
    nhdplusid: int
    gnis_name: str | None = None
    length_km: float
    cumulative_km: float
    velocity_mps: float | None = None  # None when EROM has no velocity for the reach


class GaugeInfo(BaseModel):
    site_id: str
    name: str | None = None
    latitude: float
    longitude: float
    linked_reach_nhdplusid: int
    link_distance_m: float


class HealthResponse(ResponseBase):
    status: Literal["ok", "degraded"]
    simulation_mode: bool
    data_ready: dict[str, bool]


class ReachResponse(ResponseBase):
    reach: dict[str, Any]  # GeoJSON Feature
    upstream_path: list[UpstreamReach]
    gauge: GaugeInfo


# ---- Facilities ------------------------------------------------------------------------------
class ViolationRecord(BaseModel):
    parameter: str
    monitoring_period_end: date | None
    violation_desc: str
    exceedance_pct: float | None = None
    status: str | None = None  # RNC resolution text; violations are reported or alleged
    outfall: str | None = None
    source_fields: list[str] = []


class TriRelease(BaseModel):
    chemical: str
    year: int
    water_release: float | None
    unit: str
    source_fields: list[str] = []


class ParameterMatch(BaseModel):
    sensor_parameter: SensorParameter
    echo_parameter: str
    is_proxy: bool
    note: str


class CandidateFacility(BaseModel):
    facility_id: str  # NPDES permit id from local ECHO data
    name: str
    frs_id: str | None = None
    latitude: float
    longitude: float
    location_type: LocationType  # outfall point where one exists, otherwise facility point
    location_note: str
    distance_km_to_reach: float
    nearest_reach_nhdplusid: int
    upstream_distance_km: float | None = None
    travel_time: TravelTime | None = None
    numeric_violation_count: int = 0  # in the window; ranking uses these only
    reporting_violation_count: int = 0  # overdue reports: not pollution, never ranked
    historical_numeric_violation_count: int = 0  # before the window: historical, never ranked
    violation_window_start: date | None = None
    violation_window_end: date | None = None  # the scenario date, not today
    violation_window_label: str = ""  # e.g. "reported numeric limit violations, 2021–2026"
    violations: list[ViolationRecord] = []
    matched_parameters: list[ParameterMatch] = []
    tri_releases: list[TriRelease] = []
    tri_match: Literal["frs_id", "proximity_name"] | None = None
    tri_match_confidence: Literal["high", "low"] | None = None
    rank: int | None = None
    rank_basis: str | None = None


class FacilitiesResponse(ResponseBase):
    comid: int
    max_km: float
    candidates: list[CandidateFacility]
    geojson: FeatureCollection


# ---- Sensors, rain, vision, fusion -----------------------------------------------------------
class SensorSample(BaseModel):
    timestamp: datetime
    values: dict[SensorParameter, float | None]
    simulated: bool = False  # True on injected rows


class SensorSeriesResponse(ResponseBase):
    incident_id: str
    site_id: str
    baseline: list[SensorSample]
    injected: list[SensorSample]


class SensorEvent(BaseModel):
    """One run of consecutive anomalous slots for one parameter."""

    timestamp: datetime  # onset: first anomalous slot
    parameter: SensorParameter
    score: float  # peak |z| in the run
    value: float  # value at the peak
    baseline_mean: float  # pre-event rolling baseline at the peak
    baseline_std: float
    direction: Literal["rise", "drop"]
    peak_timestamp: datetime
    end_timestamp: datetime
    n_points: int
    includes_simulated: bool = False  # True if any slot in the run was injected


class RainDay(BaseModel):
    day: date
    prcp_inches: float | None


class RainContext(BaseModel):
    station_id: str
    resolution: Literal["daily"] = "daily"
    days: list[RainDay]
    window_total_inches: float | None
    threshold_inches: float
    exceeded: bool | None  # None when rain data is missing
    missing_days: int = 0
    note: str = "Daily totals only; rain timing within a day is unknown."


class StormDecision(BaseModel):
    event: SensorEvent
    label: Literal["storm_runoff", "flag_for_review"]
    reason: str


class StormFilterResult(ResponseBase):
    overall: Literal["storm_runoff", "flag_for_review", "no_anomaly"]
    decisions: list[StormDecision]
    rain: RainContext | None
    data_origin: str  # e.g. "real observed data, not simulated" or "simulated spill injected into real baseline"


class VisionDetection(BaseModel):
    frame_index: int
    time_s: float
    label: str  # e.g. "plume" or "debris"
    confidence: float = Field(ge=0, le=1)
    area_fraction: float | None = None
    bbox: tuple[int, int, int, int] | None = None
    detector: Literal["hsv", "yolo"]


class FusedEvent(BaseModel):
    start: datetime
    end: datetime
    sources: list[Literal["sensor", "vision"]]
    sensor_events: list[SensorEvent] = []
    vision_detections: list[VisionDetection] = []
    single_source: bool
    confidence: float = Field(ge=0, le=1)
    storm_filter: Literal["storm_runoff", "flag_for_review", "not_applicable"] = "not_applicable"


# ---- Agents ----------------------------------------------------------------------------------
class TriageResult(ResponseBase):
    classification: Literal["incident", "storm_runoff", "noise"]
    confidence: float = Field(ge=0, le=1)
    reasons: list[str]


class TraceResult(ResponseBase):
    candidates: list[CandidateFacility]
    assumptions: list[str] = []


class ComplianceFact(BaseModel):
    facility_id: str
    statement: str
    value: str
    source_field: str
    source_file: str


class ComplianceResult(ResponseBase):
    facts: list[ComplianceFact]


class TimelineEntry(BaseModel):
    timestamp: datetime | None
    event: str


class IncidentReport(ResponseBase):
    banner: str = BANNER
    summary: str
    timeline: list[TimelineEntry]
    ranked_candidates: list[CandidateFacility]
    confidence: float = Field(ge=0, le=1)
    assumptions: list[str]
    limitations: list[str]
    generated_by: Literal["groq", "fallback_template"]


class IncidentResponse(ResponseBase):
    incident_id: str
    triage: TriageResult
    trace: TraceResult
    compliance: ComplianceResult
    report: IncidentReport


class ScenarioRunResponse(ResponseBase):
    incident_id: str
    status: Literal["completed"]


class StreamEvent(ResponseBase):
    incident_id: str
    seq: int
    kind: Literal["sensor_tick", "plume_detection", "agent_output", "complete"]
    scenario_time: datetime | None = None
    payload: dict[str, Any]
