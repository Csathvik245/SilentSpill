"""Trace agent: candidate facilities upstream of the spill reach, ranked by river distance.

Code calls the tools; the model only annotates candidates. Every CandidateFacility value comes from
tool output, in tool (distance) order. Storm runoff and non-incidents get no candidate list.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..schemas import AgentMode, CandidateFacility, TraceResult
from ..simulation.scenario import ScenarioResult
from . import fallback
from .base import run_agent
from .tools import Tools

TRACE_CLASSES = ("incident", "flag_for_review")

SYSTEM_PROMPT = """You are the Trace agent in SilentSpill, a water-quality monitoring demo whose output is a draft for human review.

Role: the TOOL RESULTS list permitted facilities with reported violations whose outfall (or facility point) lies near the river upstream of the spill reach, already ranked by river distance. For each candidate write one short, factual note: river distance upstream, river travel time (copy the travel_time text exactly, including any "≥", which marks a lower bound), the reported numeric limit violations in the window, and which reported parameters match the anomalous sensor parameters (say "proxy" for TSS/TDS matches). Add assumptions only if they are not already obvious.

Rules:
- Use only the values in TOOL RESULTS. Copy facility_id and name exactly. Never add a facility that is not listed, never invent numbers, dates or chemicals, and copy numbers exactly as written.
- You cannot change the ranking; it is by river distance, not travel time and not by your judgement.
- Non-accusatory wording: never say a facility "caused" or "is responsible for" the spill, or is "the source of the spill", a "polluter", "culprit" or "to blame"; no "dumping". Say "facilities with reported violations near this reach" and "consistent with". Violations are reported or alleged.

Return only a JSON object of this shape:
{"candidates": [{"facility_id": "...", "name": "...", "note": "..."}], "assumptions": ["..."]}
Example:
{"candidates": [{"facility_id": "VA0000000", "name": "EXAMPLE NAME LLC", "note": "1.5 km upstream by river; river travel time ≥ 2.0 h (lower bound); 2 reported numeric limit violations in the window; reported TSS matches turbidity (proxy)."}], "assumptions": ["Travel times assume the mean annual EROM velocity."]}"""

FIXED_ASSUMPTIONS = [
    "Candidates are ranked by river distance upstream of the spill reach, then distance to the river; not by travel time.",
    "Travel times use the mean annual EROM velocity, treated as ft/s (a documented assumption); missing velocities are never filled in.",
    "Only permits with at least one reported numeric limit violation in the window are listed.",
]


class TraceCandidateLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    facility_id: str
    name: str = ""
    note: str = ""


class TraceLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    candidates: list[TraceCandidateLLM] = []
    assumptions: list[str] = []


def candidate_summary(c: dict[str, Any], travel: dict[str, Any] | None) -> dict[str, Any]:
    """What the model sees (and may cite) for one candidate."""
    tt = travel or c.get("travel_time") or {}
    return {
        "rank": c.get("rank"),
        "facility_id": c["facility_id"],
        "name": c["name"],
        "upstream_distance_km": None if c.get("upstream_distance_km") is None else round(c["upstream_distance_km"], 2),
        "distance_km_to_reach": round(c["distance_km_to_reach"], 2),
        "travel_time": tt.get("display"),
        "travel_time_is_lower_bound": tt.get("is_lower_bound"),
        "location_type": c["location_type"],
        "location_note": c["location_note"],
        "numeric_violation_count": c["numeric_violation_count"],
        "historical_numeric_violation_count": c["historical_numeric_violation_count"],
        "reporting_violation_count": c["reporting_violation_count"],
        "violation_window_label": c["violation_window_label"],
        "matched_parameters": [
            {k: m[k] for k in ("sensor_parameter", "echo_parameter", "is_proxy")} for m in c.get("matched_parameters", [])
        ],
        "tri_match": c.get("tri_match"),
        "permit_type": c.get("permit_type"),
        "general_permit_id": c.get("general_permit_id"),
        "small_domestic_general_permit": c.get("small_domestic_general_permit", False),
        "facility_type": c.get("facility_type"),
        "permit_note": c.get("permit_note"),
    }


def should_trace(scenario: ScenarioResult) -> bool:
    return scenario.fusion.classification in TRACE_CLASSES and bool(scenario.candidates)


def skip_reason(scenario: ScenarioResult) -> str:
    if scenario.fusion.classification not in TRACE_CLASSES:
        return f"fusion classification {scenario.fusion.classification} gets no candidate list"
    return "no candidate facilities within the traced distance"


def no_trace_result(scenario: ScenarioResult) -> TraceResult:
    cls = scenario.fusion.classification
    if cls == "storm_runoff":
        msg = "Storm runoff gets no candidate list: the anomaly is consistent with rain, so no permit holder is named."
    elif cls not in TRACE_CLASSES:
        msg = f"No candidate list: the fusion classification is {cls}."
    else:
        msg = "No permits with reported numeric limit violations were found near the traced upstream reaches."
    return TraceResult(candidates=[], assumptions=[msg])


def gather(scenario: ScenarioResult, tools: Tools) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Call the tools (recorded in the ledger). Returns (candidate dumps in rank order, model context)."""
    reaches = tools.trace_upstream()
    cands = tools.find_candidate_facilities()
    summaries = []
    for c in cands:
        try:
            travel = tools.travel_time(c["facility_id"])
        except ValueError:
            travel = None
        summaries.append(candidate_summary(c, travel))
    total_km = max((r.get("cumulative_km") or 0.0 for r in reaches), default=0.0)
    ctx = {
        "case": scenario.case,
        "fusion_classification": scenario.fusion.classification,
        "anomalous_parameters": scenario.anomalous_parameters,
        "upstream_trace": {"reach_count": len(reaches), "max_km": scenario.max_km, "traced_km": round(total_km, 1)},
        "candidate_count": len(summaries),
        "rank_basis": cands[0].get("rank_basis") if cands else None,
        "candidates": summaries,
    }
    return cands, ctx


def _norm(name: str) -> str:
    return " ".join(name.upper().split())


def finalize(
    llm: TraceLLM, cands: list[dict[str, Any]], ctx: dict[str, Any], tools: Tools
) -> tuple[TraceResult, list[str]]:
    """Reject unknown ids/names, keep the tool ranking, copy values from tool output."""
    notes: list[str] = []
    ledger = tools.ledger
    known_ids = ledger.facility_ids()
    tool_names = {c["facility_id"]: c["name"] for c in cands}
    extra = fallback.context_numbers(ctx)
    accepted: dict[str, str] = {}
    for item in llm.candidates:
        fid = item.facility_id.strip()
        if fid not in known_ids or fid not in tool_names:
            notes.append(f"trace: rejected model candidate {fid} (not returned by any tool)")
            continue
        if item.name.strip() and _norm(item.name) != _norm(tool_names[fid]):
            notes.append(f"trace: model name for {fid} does not match tool name; tool name kept")
            continue
        if fid in accepted:
            continue
        cleaned = fallback.clean_all([item.note], ledger, extra, f"trace {fid}", notes)
        accepted[fid] = cleaned[0] if cleaned else ""
    candidates = [CandidateFacility.model_validate(c) for c in cands]
    for i, c in enumerate(candidates, 1):
        if c.rank is None:
            c.rank = i
    candidate_notes = {fid: note for fid, note in accepted.items() if note}
    assumptions = list(FIXED_ASSUMPTIONS)
    for a in fallback.clean_all(llm.assumptions, ledger, extra, "trace assumptions", notes):
        if a not in assumptions:
            assumptions.append(a)
    return TraceResult(candidates=candidates, candidate_notes=candidate_notes, assumptions=assumptions), notes


def run(
    scenario: ScenarioResult, tools: Tools, client: Any, triage: Any = None
) -> tuple[TraceResult, AgentMode, str | None, list[str]]:
    if not should_trace(scenario):
        return no_trace_result(scenario), "skipped", skip_reason(scenario), []
    cands, ctx = gather(scenario, tools)
    user = (
        "TOOL RESULTS (JSON):\n" + fallback.to_json(ctx)
        + "\n\nTASK: Write one short note per candidate (keep every candidate, same ids and names). "
        "Return only the JSON object."
    )
    llm, mode, reason = run_agent(
        client, SYSTEM_PROMPT, user, TraceLLM, lambda: TraceLLM.model_validate(fallback.trace(ctx))
    )
    result, notes = finalize(llm, cands, ctx, tools)
    return result, mode, reason, notes


def fallback_result(scenario: ScenarioResult, tools: Tools) -> tuple[TraceResult, list[str]]:
    """Deterministic result without any model call."""
    if not should_trace(scenario):
        return no_trace_result(scenario), []
    cands, ctx = gather(scenario, tools)
    return finalize(TraceLLM.model_validate(fallback.trace(ctx)), cands, ctx, tools)
