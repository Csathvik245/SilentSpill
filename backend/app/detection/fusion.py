"""Fuse gauge anomalies, the storm filter and camera plume detections into one classification.

The camera views the reach next to the gauge, so both can see the same water up to max_delay
apart (the reach's river travel time), in either order. A sensor event corroborates the plume
when its run [start, end] overlaps [first_plume - max_delay, last_plume + max_delay].

Confidence rules (all results still need human review):
  incident        min(0.95, 0.5 + 0.1 * distinct matching parameters + 0.2 * max plume confidence)
  storm_runoff    0.6 + 0.05 per extra distinct parameter in the storm pattern, capped at 0.7
  flag_for_review sensor only 0.4, vision only 0.3 (a single unconfirmed source stays low)
  no_incident     0.0
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import networkx as nx

from ..schemas import (
    FusedEvent,
    FusionResult,
    SensorEvent,
    StormFilterResult,
    VisionDetection,
    VisionSummary,
)

DEFAULT_MAX_DELAY = timedelta(hours=3)  # demo reach ≈ 3.0 h (3.16 km at 0.293 m/s)
SENSOR_ONLY_CONFIDENCE = 0.4
VISION_ONLY_CONFIDENCE = 0.3
STORM_CONFIDENCE = 0.6
STORM_CONFIDENCE_MAX = 0.7
INCIDENT_CONFIDENCE_MAX = 0.95


def _utc(ts: datetime) -> datetime:
    return ts.replace(tzinfo=timezone.utc) if ts.tzinfo is None else ts.astimezone(timezone.utc)


def _hours(td: timedelta) -> float:
    return td.total_seconds() / 3600.0


def _name(parameter: str) -> str:
    return {"ph": "pH"}.get(parameter, parameter)


def camera_gauge_max_delay(G: nx.DiGraph | None = None, reach_id: int | None = None) -> timedelta:
    """Travel time over the camera/gauge reach from EROM velocity; DEFAULT_MAX_DELAY if unavailable."""
    try:
        from ..data import nhd_graph

        if reach_id is None:
            from ..config import get_settings

            reach_id = get_settings().reach_nhdplusid
        if G is None:
            G = nhd_graph.load_graph()
        tt = nhd_graph.travel_time(G, [reach_id])
    except Exception:  # missing GeoPackage, config or reach: use the documented default
        return DEFAULT_MAX_DELAY
    hours = tt.hours if tt.hours is not None else tt.travel_time_hours_min
    if hours is None or hours <= 0:
        return DEFAULT_MAX_DELAY
    return timedelta(hours=hours)


def _overlaps(event: SensorEvent, lo: datetime, hi: datetime) -> bool:
    return _utc(event.timestamp) <= hi and lo <= _utc(event.end_timestamp)


def _storm_labels(events: list[SensorEvent], storm: StormFilterResult | None) -> list[str | None]:
    """Storm-filter label per sensor event (None when the filter did not see that event)."""
    if storm is None:
        return [None] * len(events)
    out: list[str | None] = []
    for ev in events:
        label = next((d.label for d in storm.decisions if d.event is ev), None)
        if label is None:
            label = next((d.label for d in storm.decisions if d.event == ev), None)
        out.append(label)
    return out


def _storm_filter_field(labels: list[str | None]) -> str:
    known = [lab for lab in labels if lab is not None]
    if not known:
        return "not_applicable"
    return "storm_runoff" if all(lab == "storm_runoff" for lab in known) else "flag_for_review"


def _span(events: list[SensorEvent], detections: list[VisionDetection],
          first: datetime | None = None, last: datetime | None = None) -> tuple[datetime, datetime]:
    starts = [_utc(e.timestamp) for e in events]
    ends = [_utc(e.end_timestamp) for e in events]
    if first is not None and last is not None:
        starts.append(_utc(first))
        ends.append(_utc(last))
    for d in detections:
        if d.timestamp is not None:
            starts.append(_utc(d.timestamp))
            ends.append(_utc(d.timestamp))
    return min(starts), max(ends)


def _sensor_event(events: list[SensorEvent], labels: list[str | None], confidence: float) -> FusedEvent:
    start, end = _span(events, [])
    return FusedEvent(
        start=start, end=end, sources=["sensor"], sensor_events=events, single_source=True,
        confidence=confidence, storm_filter=_storm_filter_field(labels),
    )


def _params(events: list[SensorEvent]) -> str:
    seen: list[str] = []
    for e in sorted(events, key=lambda e: _utc(e.timestamp)):
        label = f"{_name(e.parameter)} {e.direction}"
        if label not in seen:
            seen.append(label)
    return ", ".join(seen)


def fuse(
    sensor_events: list[SensorEvent],
    storm: StormFilterResult | None,
    vision: VisionSummary | None,
    max_delay: timedelta = DEFAULT_MAX_DELAY,
) -> FusionResult:
    """Classify incident / storm_runoff / flag_for_review / no_incident from both sources."""
    delay_h = _hours(max_delay)
    allowance = (
        f"Camera and gauge observations are compared with a ±{delay_h:.1f} h allowance "
        "(river travel time over the reach between them)."
    )
    labels = _storm_labels(sensor_events, storm)
    storm_ev = [e for e, lab in zip(sensor_events, labels) if lab == "storm_runoff"]
    storm_lab = [lab for lab in labels if lab == "storm_runoff"]
    review = [(e, lab) for e, lab in zip(sensor_events, labels) if lab != "storm_runoff"]

    first = last = None
    detections: list[VisionDetection] = []
    plume_conf = 0.0
    if vision is not None and vision.first_plume is not None and vision.last_plume is not None:
        first, last = _utc(vision.first_plume), _utc(vision.last_plume)
        detections = list(vision.plume_detections)
        confs = [d.confidence for d in detections] or [f.confidence for f in vision.frames if f.plume_detected]
        plume_conf = max(confs, default=0.0)
    plume = first is not None

    matching: list[tuple[SensorEvent, str | None]] = []
    other: list[tuple[SensorEvent, str | None]] = list(review)
    if plume:
        lo, hi = first - max_delay, last + max_delay
        matching = [(e, lab) for e, lab in review if _overlaps(e, lo, hi)]
        other = [(e, lab) for e, lab in review if not _overlaps(e, lo, hi)]

    reasons: list[str] = []
    if vision is None:
        reasons.append("No camera analysis was available for this window.")
    elif plume:
        reasons.append(
            f"Camera clip '{vision.clip}' shows dye-coloured water from {first:%Y-%m-%d %H:%M} to "
            f"{last:%Y-%m-%d %H:%M} UTC in {len(detections)} of {vision.frames_analyzed} frames "
            f"(peak plume confidence {plume_conf:.2f})."
        )
    else:
        reasons.append(
            f"Camera clip '{vision.clip}' shows no dye-coloured water in {vision.frames_analyzed} frames."
        )
    if not sensor_events:
        reasons.append("The gauge shows no anomalous readings in this window.")
    if storm_ev:
        reasons.append(
            f"{len(storm_ev)} gauge event(s) ({_params(storm_ev)}) match the storm-runoff pattern "
            "(turbidity rise with rain at or above the threshold) and are not counted as corroboration."
        )

    events: list[FusedEvent] = []
    storm_event = (
        _sensor_event(storm_ev, storm_lab, STORM_CONFIDENCE) if storm_ev else None
    )

    if plume and matching:
        m_events = [e for e, _ in matching]
        n_params = len({e.parameter for e in m_events})
        confidence = min(INCIDENT_CONFIDENCE_MAX, 0.5 + 0.1 * n_params + 0.2 * plume_conf)
        start, end = _span(m_events, detections, first, last)
        events.append(FusedEvent(
            start=start, end=end, sources=["sensor", "vision"], sensor_events=m_events,
            vision_detections=detections, single_source=False, confidence=confidence,
            storm_filter=_storm_filter_field([lab for _, lab in matching]),
        ))
        reasons.append(
            f"Gauge anomalies in {n_params} parameter(s) ({_params(m_events)}) fall within the "
            f"±{delay_h:.1f} h allowance of the camera plume, so two independent sources agree "
            "(not single-source)."
        )
        if other:
            events.append(_sensor_event([e for e, _ in other], [lab for _, lab in other], SENSOR_ONLY_CONFIDENCE))
            reasons.append(
                f"{len(other)} other gauge event(s) ({_params([e for e, _ in other])}) fall outside the "
                "allowance and are listed separately for review."
            )
        if storm_event:
            events.append(storm_event)
        reasons.append(allowance)
        reasons.append("Classified as a possible incident for human review; it does not identify a source.")
        return FusionResult(
            classification="incident", confidence=confidence, single_source=False,
            sources=["sensor", "vision"], events=events, max_delay_hours=delay_h, reasons=reasons,
        )

    if not plume and sensor_events and not review and storm is not None and storm.overall == "storm_runoff":
        n_params = len({e.parameter for e in storm_ev})
        confidence = min(STORM_CONFIDENCE_MAX, STORM_CONFIDENCE + 0.05 * (n_params - 1))
        storm_event = _sensor_event(storm_ev, storm_lab, confidence)
        reasons.append(
            "Every gauge event is consistent with storm runoff and the camera does not confirm a plume; "
            "this rests on the gauge and rain record alone (single-source)."
        )
        reasons.append(allowance)
        return FusionResult(
            classification="storm_runoff", confidence=confidence, single_source=True,
            sources=["sensor"], events=[storm_event], max_delay_hours=delay_h, reasons=reasons,
        )

    if plume or review:
        sources: list[str] = []
        confidence = 0.0
        if review:
            sources.append("sensor")
            confidence = SENSOR_ONLY_CONFIDENCE
            events.append(_sensor_event([e for e, _ in review], [lab for _, lab in review], SENSOR_ONLY_CONFIDENCE))
            reasons.append(
                f"Gauge anomalies ({_params([e for e, _ in review])}) are not explained by the storm filter."
            )
        if plume:
            sources.append("vision")
            confidence = max(confidence, VISION_ONLY_CONFIDENCE)
            start, end = _span([], detections, first, last)
            events.append(FusedEvent(
                start=start, end=end, sources=["vision"], vision_detections=detections,
                single_source=True, confidence=VISION_ONLY_CONFIDENCE,
            ))
        if plume and review:
            reasons.append(
                f"The gauge anomalies and the camera plume do not line up within the ±{delay_h:.1f} h "
                "allowance, so neither confirms the other (single-source)."
            )
        elif plume:
            reasons.append(
                f"No unexplained gauge anomaly falls within ±{delay_h:.1f} h of the camera plume; "
                "camera only (single-source)."
            )
        else:
            reasons.append("The camera does not confirm a plume; gauge only (single-source).")
        if storm_event:
            events.append(storm_event)
        reasons.append(allowance)
        reasons.append("Flagged for human review; a single unconfirmed source is kept at low confidence.")
        return FusionResult(
            classification="flag_for_review", confidence=confidence, single_source=True,
            sources=sources, events=events, max_delay_hours=delay_h, reasons=reasons,
        )

    # Only storm-labelled events remain here when the storm filter's overall is not storm_runoff.
    if storm_event:
        events.append(storm_event)
    reasons.append("Neither source shows an unexplained change; no incident.")
    return FusionResult(
        classification="no_incident", confidence=0.0, single_source=False, sources=[],
        events=events, max_delay_hours=delay_h, reasons=reasons,
    )
