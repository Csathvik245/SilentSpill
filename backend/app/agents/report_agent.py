"""Report agent: the final human-review IncidentReport.

Code builds the timeline, ranked candidates, confidence, classification and the required
limitations. The model writes only the summary and optional extra assumptions/limitations, which
pass through the guardrails.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, ConfigDict

from ..schemas import (
    AgentMode,
    ComplianceResult,
    IncidentReport,
    TimelineEntry,
    TraceResult,
    TriageResult,
)
from ..simulation.scenario import ScenarioResult
from . import fallback
from .base import run_agent
from .tools import Tools
from .trace_agent import candidate_summary

SYSTEM_PROMPT = """You are the Report agent in SilentSpill, a water-quality monitoring demo. You write the summary of a draft incident report that a person must review before any action.

Role: summarize, in 3 to 7 plain sentences, what TOOL RESULTS show: whether the data are simulated or real observed data, the classification and its confidence, the anomalous parameters, and (only if candidates are listed) the facilities with reported violations near this reach in their given rank order, with river distance, travel time text copied exactly (keep any "≥": it marks a lower bound) and reported violation counts. If the classification is storm_runoff, say the anomaly is consistent with storm runoff given the recorded rain, and that there is no candidate list; name no facility. The timeline, candidate table and required limitations are added by code: do not repeat the limitations.

Rules:
- Use only the values in TOOL RESULTS. Copy facility names, permit IDs and numbers exactly as written there. Never invent facilities, numbers, dates or chemicals.
- Non-accusatory wording: never say a facility "caused" or "is responsible for" the spill, or is "the source of the spill", a "polluter", "culprit", "guilty" or "to blame"; no "dumping" or "dumped". Use "facilities with reported violations near this reach" and "consistent with". Violations are reported or alleged. The report does not identify a source.
- Rain is daily totals only; a missing day is "no rain reading", not zero.
- extra_assumptions / extra_limitations: optional, short, and only if not already in required_limitations.

Return only a JSON object of this shape:
{"summary": "...", "extra_assumptions": ["..."], "extra_limitations": ["..."]}
Example:
{"summary": "Simulated spill scenario: gauge and camera evidence is classified as a possible incident (confidence 0.9) for human review. 2 facilities with reported violations near this reach are listed upstream, ranked by river distance. Rank 1: EXAMPLE NAME LLC (VA0000000), 1.5 km upstream, river travel time ≥ 2.0 h, 2 reported numeric limit violations in the window. These records are consistent with nearby permitted discharges but do not identify a source.", "extra_assumptions": [], "extra_limitations": []}"""


class ReportLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    summary: str
    extra_assumptions: list[str] = []
    extra_limitations: list[str] = []


def required_limitations(scenario: ScenarioResult) -> list[str]:
    """Limitations present in every report, in both modes."""
    out: list[str] = []
    if scenario.case == "spill":
        out.append(
            "The spill, the gauge sensor spike and the plume camera footage are simulated: the spike is "
            "injected into a real gauge baseline and the footage is rendered."
        )
    else:
        out.append(
            "This case uses real observed gauge and rain data, not simulated; there is no camera footage for it."
        )
    out.append(
        "Facility coordinates are reported locations from ECHO (outfall points where available, otherwise "
        "facility geocodes), not surveyed discharge points."
    )
    rain_note = (
        "Rain is daily totals only (the event day and the day before); timing within a day is unknown, and a "
        "missing day is reported as no rain reading, not zero rain."
    )
    rain = scenario.storm.rain
    if rain is not None and rain.missing_days:
        rain_note += f" {rain.missing_days} of {len(rain.days)} days checked had no rain reading."
    out.append(rain_note)
    out.append(
        "Travel times shown with ≥ are lower bounds: some reaches on the path have no EROM velocity, and "
        "missing velocities are never filled in."
    )
    out.append(
        "Conductivity→TDS and turbidity→TSS parameter matches are proxies: indirect indicators, not the same "
        "measurement."
    )
    v = scenario.vision
    if v is None:
        out.append(
            "No camera footage for this case, so the Roboflow debris model was not run; it detects trash and "
            "people, not plumes."
        )
    elif v.debris_status.startswith("ok") and not v.debris:
        out.append(
            f"The Roboflow debris model detected nothing on the sampled frames ({v.debris_status}); it detects "
            "trash and people, not plumes."
        )
    elif v.debris_status.startswith("ok"):
        out.append(
            f"The Roboflow debris model reported {len(v.debris)} detections ({v.debris_status}); it detects "
            "trash and people, not plumes, so these are supporting context only."
        )
    else:
        out.append(
            f"Roboflow debris model status: {v.debris_status}; it detects trash and people, not plumes."
        )
    out.append(
        "Violations are reported or alleged ECHO records, counted only within the stated window; historical "
        "violations are shown separately and not ranked."
    )
    out.append("TRI is annual self-reported data and cannot confirm a live event.")
    return out


def _fmt(ts: Any) -> str:
    return f"{ts:%Y-%m-%d %H:%M} UTC"


def build_timeline(scenario: ScenarioResult) -> list[TimelineEntry]:
    entries: list[TimelineEntry] = []
    sim = " (simulated)" if scenario.case == "spill" else ""
    if scenario.case == "spill":
        entries.append(TimelineEntry(timestamp=scenario.scenario_timestamp, event="Simulated spill release begins."))
    rain = scenario.storm.rain
    if rain is not None and scenario.case == "storm":
        for d in rain.days:
            amount = "no rain reading" if d.prcp_inches is None else f"{d.prcp_inches} in of rain"
            entries.append(TimelineEntry(
                timestamp=datetime(d.day.year, d.day.month, d.day.day, tzinfo=timezone.utc),
                event=f"Daily total at {rain.station_id}: {amount} (daily totals only).",
            ))
    v = scenario.vision
    if v is not None and v.first_plume is not None:
        entries.append(TimelineEntry(
            timestamp=v.first_plume,
            event=f"Camera first shows a plume{sim} ({len(v.plume_detections)} of {v.frames_analyzed} frames).",
        ))
        if v.last_plume is not None:
            entries.append(TimelineEntry(timestamp=v.last_plume, event=f"Last camera frame with a plume{sim}."))
    for e in scenario.sensor_events:
        entries.append(TimelineEntry(
            timestamp=e.timestamp,
            event=(
                f"Gauge {fallback.param_label(e.parameter)} {e.direction} begins{' (includes simulated values)' if e.includes_simulated else ''}; "
                f"peak at {_fmt(e.peak_timestamp)}, ends {_fmt(e.end_timestamp)}."
            ),
        ))
    f = scenario.fusion
    start = f.events[0].start if f.events else None
    entries.append(TimelineEntry(
        timestamp=start,
        event=f"Fusion classification: {f.classification} (confidence {f.confidence}, sources: {', '.join(f.sources) or 'none'}).",
    ))
    return sorted(entries, key=lambda t: (t.timestamp is None, t.timestamp or scenario.scenario_timestamp))


def build_context(
    scenario: ScenarioResult, triage: TriageResult, trace: TraceResult, compliance: ComplianceResult,
    timeline: list[TimelineEntry], limitations: list[str],
) -> dict[str, Any]:
    cands = [candidate_summary(c.model_dump(mode="json"), None) for c in trace.candidates]
    for s in cands:
        s.pop("location_note", None)
        if trace.candidate_notes.get(s["facility_id"]):
            s["note"] = trace.candidate_notes[s["facility_id"]]
    rain = scenario.storm.rain
    return {
        "case": scenario.case,
        "data_origin": scenario.data_origin,
        "fusion_classification": scenario.fusion.classification,
        "triage_classification": triage.classification,
        "confidence": scenario.fusion.confidence,
        "triage_reasons": triage.reasons,
        "anomalous_parameters": scenario.anomalous_parameters,
        "rain": None if rain is None else {
            "window_total_inches": rain.window_total_inches,
            "threshold_inches": rain.threshold_inches,
            "exceeded": rain.exceeded,
            "days": [d.model_dump(mode="json") for d in rain.days],
        },
        "candidate_count": len(cands),
        "candidates": cands,
        "compliance_facts": [
            {"facility_id": f.facility_id, "statement": f.statement, "value": f.value} for f in compliance.facts
        ],
        "timeline": [{"timestamp": t.timestamp, "event": t.event} for t in timeline],
        "required_limitations": limitations,
    }


def _storm_ok(summary: str) -> bool:
    return "storm" in summary.lower()


def finalize(
    llm: ReportLLM, scenario: ScenarioResult, tools: Tools, triage: TriageResult, trace: TraceResult,
    ctx: dict[str, Any], timeline: list[TimelineEntry], limitations: list[str],
) -> tuple[IncidentReport, list[str]]:
    notes: list[str] = []
    ledger = tools.ledger
    extra = fallback.context_numbers(ctx)
    parts = fallback.clean_all([llm.summary], ledger, extra, "report summary", notes)
    summary = parts[0] if parts else ""
    storm = scenario.fusion.classification == "storm_runoff"
    if not summary or (storm and not _storm_ok(summary)):
        notes.append("report: model summary unusable after guardrails; template summary used")
        parts = fallback.clean_all([fallback.report(ctx)["summary"]], ledger, extra, "report summary", notes)
        summary = parts[0] if parts else "Draft incident report; requires human review before any action."
    assumptions = list(trace.assumptions)
    for a in fallback.clean_all(llm.extra_assumptions, ledger, extra, "report assumptions", notes):
        if a not in assumptions:
            assumptions.append(a)
    lims = list(limitations)
    for x in fallback.clean_all(llm.extra_limitations, ledger, extra, "report limitations", notes):
        if x not in lims:
            lims.append(x)
    report = IncidentReport(
        summary=summary,
        timeline=timeline,
        ranked_candidates=[c.model_copy(deep=True) for c in trace.candidates],
        confidence=scenario.fusion.confidence,
        assumptions=assumptions,
        limitations=lims,
        classification=scenario.fusion.classification,
        data_origin=scenario.data_origin,
        report_mode="fallback",
    )
    return report, notes


def _prepare(
    scenario: ScenarioResult, triage: TriageResult, trace: TraceResult, compliance: ComplianceResult
) -> tuple[list[TimelineEntry], list[str], dict[str, Any]]:
    timeline = build_timeline(scenario)
    limitations = required_limitations(scenario)
    ctx = build_context(scenario, triage, trace, compliance, timeline, limitations)
    return timeline, limitations, ctx


def run(
    scenario: ScenarioResult, tools: Tools, client: Any, triage: TriageResult, trace: TraceResult,
    compliance: ComplianceResult, prior_notes: list[str] | None = None,
) -> tuple[IncidentReport, AgentMode, str | None, list[str]]:
    """Returns (report, mode, reason, own notes). report.guardrail_notes = prior_notes + own notes."""
    timeline, limitations, ctx = _prepare(scenario, triage, trace, compliance)
    user = (
        "TOOL RESULTS (JSON):\n" + fallback.to_json(ctx)
        + "\n\nTASK: Write the report summary. Return only the JSON object."
    )
    llm, mode, reason = run_agent(
        client, SYSTEM_PROMPT, user, ReportLLM, lambda: ReportLLM.model_validate(fallback.report(ctx))
    )
    report, notes = finalize(llm, scenario, tools, triage, trace, ctx, timeline, limitations)
    report.report_mode = mode
    report.fallback_reason = reason if mode == "fallback" else None
    report.guardrail_notes = list(prior_notes or []) + notes
    return report, mode, reason, notes


def fallback_result(
    scenario: ScenarioResult, tools: Tools, triage: TriageResult, trace: TraceResult,
    compliance: ComplianceResult, reason: str, prior_notes: list[str] | None = None,
) -> tuple[IncidentReport, list[str]]:
    """Deterministic report without any model call."""
    timeline, limitations, ctx = _prepare(scenario, triage, trace, compliance)
    report, notes = finalize(
        ReportLLM.model_validate(fallback.report(ctx)), scenario, tools, triage, trace, ctx, timeline, limitations
    )
    report.fallback_reason = reason
    report.guardrail_notes = list(prior_notes or []) + notes
    return report, notes
