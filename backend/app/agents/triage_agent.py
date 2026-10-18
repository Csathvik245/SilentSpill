"""Triage agent: classify the fused detection as incident, flag_for_review, storm_runoff or noise.

The model may restate or downgrade the fusion classification but never upgrade it, and the
confidence is always the fusion confidence.
"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict

from ..schemas import AgentMode, RelatedPastEvent, TriageClass, TriageResult
from ..simulation.scenario import ScenarioResult
from . import fallback
from .base import run_agent
from .tools import Tools

SYSTEM_PROMPT = """You are the Triage agent in SilentSpill, a water-quality monitoring demo whose output is a draft for human review.

Role: read the TOOL RESULTS (sensor/camera fusion result, storm filter, daily rain, gauge anomaly events, camera summary) and classify the event as one of:
- "incident": independent sources agree on an anomaly that is not explained by rain;
- "flag_for_review": an anomaly that needs a person to look at it;
- "storm_runoff": the anomaly is consistent with storm runoff (turbidity rise with rain at or above the threshold);
- "noise": no meaningful anomaly.
Never choose a stronger classification than the fusion result ("incident" > "flag_for_review" > "storm_runoff" = "noise"), and never choose "storm_runoff" unless the fusion result is storm_runoff.

Rules:
- Use only the values in TOOL RESULTS. Do not invent numbers, dates, facility names, permit IDs or chemicals. Copy numbers exactly as written there.
- Non-accusatory wording: never say anything "caused" or "is responsible for" a spill; no "polluter", "culprit", "to blame", "dumping" or "source of the spill". Use "consistent with". Triage does not identify a source.
- Rain is daily totals only; a missing day is "no rain reading", never zero rain.
- prior_context lists related past events on this reach from incident memory (earlier runs and human review decisions). You may mention them briefly as context, but they never change the classification: classify from the current fusion result only.
- Write 2 to 5 short, factual reasons.

Return only a JSON object of this shape:
{"classification": "incident" | "flag_for_review" | "storm_runoff" | "noise", "reasons": ["...", "..."]}
Example:
{"classification": "flag_for_review", "reasons": ["Conductivity rise and turbidity rise at the gauge within the camera time allowance.", "No rain reading for the event days, so storm runoff cannot be confirmed or ruled out."]}"""


class TriageLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    classification: TriageClass
    reasons: list[str] = []


def prior_context(events: list[RelatedPastEvent] | None) -> list[dict[str, Any]]:
    """Recalled past events as the model sees them (already through the guardrails)."""
    return [
        {
            "memory_type": e.memory_type, "incident_id": e.incident_id, "scenario_date": e.scenario_date,
            "classification": e.classification, "review_decision": e.review_decision, "text": e.text,
        }
        for e in events or []
    ]


def build_context(scenario: ScenarioResult, prior: list[RelatedPastEvent] | None = None) -> dict[str, Any]:
    """Compact JSON context for the model. Numbers it may cite come from everything but prior_context."""
    f = scenario.fusion
    vision = None
    if scenario.vision is not None:
        v = scenario.vision
        vision = {
            "clip_kind": v.clip_kind,
            "frames_analyzed": v.frames_analyzed,
            "plume_frames": len(v.plume_detections),
            "first_plume": v.first_plume,
            "last_plume": v.last_plume,
            "debris_detections": len(v.debris),
            "debris_status": v.debris_status,
            "debris_note": v.debris_note,
        }
    return {
        "case": scenario.case,
        "data_origin": scenario.data_origin,
        "fusion": {
            "classification": f.classification,
            "confidence": f.confidence,
            "single_source": f.single_source,
            "sources": f.sources,
            "reasons": f.reasons,
        },
        "storm_filter": {
            "overall": scenario.storm.overall,
            "decisions": [{"parameter": d.event.parameter, "label": d.label, "reason": d.reason}
                          for d in scenario.storm.decisions],
        },
        "rain": scenario.storm.rain.model_dump(mode="json") if scenario.storm.rain else None,
        "sensor_events": [
            {
                "parameter": e.parameter, "direction": e.direction, "onset": e.timestamp,
                "peak": e.peak_timestamp, "end": e.end_timestamp, "peak_z": round(e.score, 1),
                "includes_simulated": e.includes_simulated,
            }
            for e in scenario.sensor_events
        ],
        "anomalous_parameters": scenario.anomalous_parameters,
        "vision": vision,
        "prior_context": prior_context(prior),
    }


def allowed_numbers(ctx: dict[str, Any]) -> set[str]:
    """Numbers in the context except prior_context: memories never make a number citable."""
    return fallback.context_numbers({k: v for k, v in ctx.items() if k != "prior_context"})


def guard_classification(model_class: str, fusion_class: str) -> tuple[TriageClass, str | None]:
    """Never stronger than fusion; storm_runoff only when fusion says so. Returns (class, note)."""
    mapped = fallback.map_fusion(fusion_class)
    if model_class not in fallback.TRIAGE_RANK:
        return mapped, f"triage: unknown classification replaced by fusion result {mapped}"
    if fallback.TRIAGE_RANK[model_class] > fallback.TRIAGE_RANK[mapped] or (
        model_class == "storm_runoff" and mapped != "storm_runoff"
    ):
        return mapped, (
            f"triage: model classification {model_class} is stronger than or inconsistent with "
            f"fusion {fusion_class}; replaced with {mapped}"
        )
    return model_class, None  # type: ignore[return-value]


def finalize(
    llm: TriageLLM, scenario: ScenarioResult, tools: Tools, ctx: dict[str, Any] | None = None
) -> tuple[TriageResult, list[str]]:
    """Apply guardrails to a model (or fallback) reply and build the TriageResult."""
    ctx = ctx or build_context(scenario)
    notes: list[str] = []
    fusion = scenario.fusion
    cls, note = guard_classification(llm.classification, fusion.classification)
    if note:
        notes.append(note)
    extra = allowed_numbers(ctx)
    reasons = fallback.clean_all(llm.reasons, tools.ledger, extra, "triage", notes)
    if not reasons:
        reasons = fallback.clean_all(fallback.triage(ctx)["reasons"], tools.ledger, extra, "triage", notes)
        notes.append("triage: no usable reasons from the model; template reasons used")
    result = TriageResult(
        classification=cls,
        fusion_classification=fusion.classification,
        confidence=fusion.confidence,
        reasons=reasons,
    )
    return result, notes


def run(
    scenario: ScenarioResult, tools: Tools, client: Any, prior: list[RelatedPastEvent] | None = None
) -> tuple[TriageResult, AgentMode, str | None, list[str]]:
    ctx = build_context(scenario, prior)
    user = (
        "TOOL RESULTS (JSON):\n" + fallback.to_json(ctx)
        + "\n\nTASK: Classify this event and give short reasons. Return only the JSON object."
    )
    llm, mode, reason = run_agent(
        client, SYSTEM_PROMPT, user, TriageLLM,
        lambda: TriageLLM.model_validate(fallback.triage(ctx)),
    )
    result, notes = finalize(llm, scenario, tools, ctx)
    return result, mode, reason, notes


def fallback_result(
    scenario: ScenarioResult, tools: Tools, prior: list[RelatedPastEvent] | None = None
) -> tuple[TriageResult, list[str]]:
    """Deterministic result without any model call."""
    ctx = build_context(scenario, prior)
    return finalize(TriageLLM.model_validate(fallback.triage(ctx)), scenario, tools, ctx)
