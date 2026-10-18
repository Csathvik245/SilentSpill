"""Deterministic template output for each agent, built from pipeline and tool output only.

Each builder returns a plain dict shaped like that agent's model-facing JSON schema, so the agent
validates and post-processes it exactly as it would a model reply (guardrails included). Wording is
non-accusatory and every number is copied from the context the agent also gives the model.
"""
from __future__ import annotations

import json
from typing import Any

from ..schemas import TriageClass
from .guardrails import clean_text, number_tokens
from .tools import ToolLedger

FUSION_TO_TRIAGE: dict[str, TriageClass] = {
    "incident": "incident",
    "flag_for_review": "flag_for_review",
    "storm_runoff": "storm_runoff",
    "no_incident": "noise",
}
TRIAGE_RANK: dict[str, int] = {"incident": 3, "flag_for_review": 2, "storm_runoff": 1, "noise": 1}

_CLASS_WORDS = {
    "incident": "a possible incident",
    "flag_for_review": "an anomaly flagged for review",
    "storm_runoff": "storm_runoff (consistent with storm runoff)",
    "no_incident": "no incident",
}


def param_label(p: str) -> str:
    """Display name of a sensor parameter ("ph" -> "pH")."""
    return "pH" if p == "ph" else p


def map_fusion(classification: str) -> TriageClass:
    """Fusion classification -> triage classification (no_incident becomes noise)."""
    return FUSION_TO_TRIAGE[classification]


# ---- Helpers shared by the agents --------------------------------------------------------------
def to_json(obj: Any) -> str:
    """Stable JSON for prompts and for number checks (dates and other objects as strings)."""
    return json.dumps(obj, ensure_ascii=False, default=str, indent=1)


def context_numbers(*contexts: Any) -> set[str]:
    """Every number the model was shown, normalized like guardrails.number_tokens."""
    return set(number_tokens("\n".join(to_json(c) for c in contexts)))


def clean_all(
    texts: list[str], ledger: ToolLedger, extra: set[str], label: str, notes: list[str]
) -> list[str]:
    """clean_text on each string; drops emptied strings and appends prefixed notes."""
    out: list[str] = []
    for t in texts:
        if not isinstance(t, str) or not t.strip():
            continue
        cleaned, n = clean_text(t, ledger, extra)
        notes.extend(f"{label}: {x}" for x in n)
        if cleaned.strip():
            out.append(cleaned.strip())
    return out


# ---- Triage ------------------------------------------------------------------------------------
def triage(ctx: dict[str, Any]) -> dict[str, Any]:
    """Follow the fusion classification and restate its reasons."""
    fusion = ctx["fusion"]
    reasons = list(fusion["reasons"])
    storm = ctx.get("storm_filter") or {}
    if storm.get("overall"):
        reasons.append(f"Storm filter result: {storm['overall']}.")
    if ctx.get("prior_context"):
        reasons.append(
            "Incident memory has related past events on this reach (listed in the report under Related past "
            "events); they do not change this classification."
        )
    reasons.append("Triage follows the sensor and camera fusion result; it does not identify a source.")
    return {"classification": map_fusion(fusion["classification"]), "reasons": reasons}


# ---- Trace -------------------------------------------------------------------------------------
def candidate_note(c: dict[str, Any]) -> str:
    """Short neutral note for one candidate summary (see trace_agent.candidate_summary)."""
    parts = [f"Rank {c['rank']} by river distance: {c['upstream_distance_km']} km upstream"]
    if c.get("travel_time"):
        parts.append(f"river travel time {c['travel_time']}")
    parts.append(f"{c['numeric_violation_count']} {c['violation_window_label'] or 'reported numeric limit violations'}")
    note = "; ".join(parts) + "."
    matches = c.get("matched_parameters") or []
    if matches:
        words = [
            f"{m['echo_parameter']} for {param_label(m['sensor_parameter'])}" + (" (proxy)" if m["is_proxy"] else "")
            for m in matches
        ]
        note += " Reported parameter match: " + ", ".join(words) + "."
    else:
        note += " No reported parameter matches the anomalous sensor parameters."
    if c.get("permit_note"):
        note += " " + c["permit_note"]
    return note


def small_domestic_sentence(c: dict[str, Any]) -> str:
    """Plain statement that a candidate holds a small domestic general permit."""
    return f"{c['name']} ({c['facility_id']}) holds a small domestic general permit ({c['general_permit_id']})."


def trace(ctx: dict[str, Any]) -> dict[str, Any]:
    cands = ctx.get("candidates") or []
    return {
        "candidates": [
            {"facility_id": c["facility_id"], "name": c["name"], "note": candidate_note(c)} for c in cands
        ],
        "assumptions": [],
    }


# ---- Compliance --------------------------------------------------------------------------------
MAX_PARAMETER_FACTS = 6


def compliance(ctx: dict[str, Any]) -> dict[str, Any]:
    """Counts, violation parameters and TRI releases, each tied to the tool field it came from."""
    facts: list[dict[str, str]] = []
    for entry in ctx.get("facilities", []):
        v = entry["violations"]
        fid = v["facility_id"]
        label = v.get("window_label") or "reported numeric limit violations in the window"
        facts.append({
            "facility_id": fid, "value": str(v["numeric_violation_count"]),
            "source_field": "numeric_violation_count",
            "statement": f"{v['numeric_violation_count']} {label} (reported or alleged).",
        })
        if v.get("historical_numeric_violation_count"):
            n = v["historical_numeric_violation_count"]
            facts.append({
                "facility_id": fid, "value": str(n), "source_field": "historical_numeric_violation_count",
                "statement": f"{n} reported numeric limit violations before the window (historical, not ranked).",
            })
        if v.get("reporting_violation_count"):
            n = v["reporting_violation_count"]
            facts.append({
                "facility_id": fid, "value": str(n), "source_field": "reporting_violation_count",
                "statement": f"{n} reporting violations (overdue reports, not pollution measurements; not ranked).",
            })
        seen: list[str] = []
        for rec in v.get("violations", []):
            p = rec.get("parameter")
            if p and p not in seen:
                seen.append(p)
        for p in seen[:MAX_PARAMETER_FACTS]:
            facts.append({
                "facility_id": fid, "value": p, "source_field": "violations[].parameter",
                "statement": f"Reported numeric limit violation parameter in the window: {p}.",
            })
        tri = entry.get("tri") or {}
        for rel in tri.get("releases", []):
            if rel.get("water_release") is None:
                continue
            facts.append({
                "facility_id": fid, "value": str(rel["water_release"]), "source_field": "releases[].water_release",
                "statement": (
                    f"TRI {rel['year']} self-reported annual water release of {rel['chemical']}: "
                    f"{rel['water_release']} {rel['unit']}."
                ),
            })
    return {"facts": facts}


# ---- Report ------------------------------------------------------------------------------------
def report(ctx: dict[str, Any]) -> dict[str, Any]:
    """Template summary from the report context (see report_agent.build_context)."""
    case = ctx["case"]
    fusion_class = ctx["fusion_classification"]
    conf = ctx["confidence"]
    sentences: list[str] = []
    if case == "storm" or fusion_class == "storm_runoff":
        rain = ctx.get("rain") or {}
        origin = "Real observed data, not simulated" if case == "storm" else "Scenario data"
        s = f"{origin}: the gauge anomalies are classified as storm_runoff (confidence {conf})"
        if rain.get("window_total_inches") is not None:
            s += (
                f", consistent with {rain['window_total_inches']} in of rain in the daily totals "
                f"(threshold {rain['threshold_inches']} in)"
            )
        sentences.append(s + ".")
        sentences.append("Storm runoff gets no candidate list, and no permit holder is named.")
    else:
        origin = "Simulated spill scenario" if case == "spill" else "Observed data"
        params = ", ".join(param_label(p) for p in ctx.get("anomalous_parameters") or []) or "none"
        sentences.append(
            f"{origin}: the gauge and camera evidence is classified as {_CLASS_WORDS[fusion_class]} "
            f"(confidence {conf}) for human review; anomalous parameters: {params}."
        )
        cands = ctx.get("candidates") or []
        if cands:
            sentences.append(
                f"{ctx['candidate_count']} facilities with reported violations near this reach are listed "
                "upstream, ranked by river distance, not travel time."
            )
            for c in cands:
                s = f"Rank {c['rank']}: {c['name']} ({c['facility_id']}), {c['upstream_distance_km']} km upstream"
                if c.get("travel_time"):
                    s += f", river travel time {c['travel_time']}"
                s += f", {c['numeric_violation_count']} {c['violation_window_label'] or 'reported numeric limit violations'}"
                matches = c.get("matched_parameters") or []
                if matches:
                    m = matches[0]
                    s += f"; reported {m['echo_parameter']} matches {param_label(m['sensor_parameter'])}"
                    s += " (proxy)" if m["is_proxy"] else ""
                if c.get("small_domestic_general_permit"):
                    s += f"; holds a small domestic general permit ({c['general_permit_id']})"
                elif c.get("permit_type"):
                    s += f"; {c['permit_type']} permit"
                sentences.append(s + ".")
        else:
            sentences.append("No upstream permit holders with reported violations were found near this reach.")
        sentences.append(
            "The listed records are consistent with nearby permitted discharges but do not identify a source."
        )
    sentences.append("This draft requires human review before any action.")
    return {"summary": " ".join(sentences), "extra_assumptions": [], "extra_limitations": []}
