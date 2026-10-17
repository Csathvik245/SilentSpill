"""Compliance agent: facts about the trace candidates, only from ECHO violations and TRI tool output.

A fact survives only if its facility is a trace candidate, its source_field is a real field of that
tool's result and its value appears in that field. source_file is set by code.
"""
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, field_validator

from ..schemas import AgentMode, ComplianceFact, ComplianceResult, TraceResult
from ..simulation.scenario import ScenarioResult
from . import fallback
from .base import run_agent
from .tools import Tools

VIOLATIONS_FILE_DEFAULT = "VA_NPDES_EFF_VIOLATIONS.csv"
TRI_FILE_DEFAULT = "2024_us.csv"

# Original CSV column names cited in source_fields -> key in the tool's list items.
VIOLATION_COLUMNS = {
    "PARAMETER_DESC": "parameter",
    "VIOLATION_DESC": "violation_desc",
    "MONITORING_PERIOD_END_DATE": "monitoring_period_end",
    "EXCEEDENCE_PCT": "exceedance_pct",
    "RNC_RESOLUTION_DESC": "status",
    "PERM_FEATURE_NMBR": "outfall",
}
TRI_COLUMNS = {
    "37. CHEMICAL": "chemical",
    "1. YEAR": "year",
    "53. 5.3 - WATER": "water_release",
    "50. UNIT OF MEASURE": "unit",
}
_LIST_FIELD = re.compile(r"^(violations|releases)(?:\[\d*\])?\.(\w+)$")

SYSTEM_PROMPT = """You are the Compliance agent in SilentSpill, a water-quality monitoring demo whose output is a draft for human review.

Role: state facts about the listed candidate facilities using only their ECHO effluent violation records and TRI (Toxics Release Inventory) records in TOOL RESULTS. Useful facts: numeric limit violation counts in the window, historical counts, the reported parameters, monitoring period end dates, exceedance percentages, and TRI water releases. If a facility has no TRI match, do not write a TRI fact for it.

Each fact must carry:
- "facility_id": copied exactly from TOOL RESULTS;
- "value": the exact value as it appears in the tool result (for example "3", "Solids, total suspended", "2023-12-31", "50.0");
- "source_field": the field it came from, written as a top-level field name (e.g. "numeric_violation_count", "historical_numeric_violation_count", "window_label") or a list field (e.g. "violations[].parameter", "violations[].monitoring_period_end", "violations[].exceedance_pct", "releases[].chemical", "releases[].water_release"), or an original CSV column listed in source_fields (e.g. "MONITORING_PERIOD_END_DATE", "53. 5.3 - WATER");
- "statement": one short sentence stating the fact.

Rules:
- Use only the values in TOOL RESULTS. Never invent facilities, numbers, dates or chemicals.
- Violations are "reported" or "alleged"; say so. Overdue reports are reporting violations, not pollution. TRI is annual self-reported data and cannot confirm a live event.
- Non-accusatory wording: never say a facility "caused" or "is responsible for" the spill, or is "the source of the spill", a "polluter", "culprit", "guilty" or "to blame"; no "dumping". Use "consistent with" only when the data supports it.

Return only a JSON object of this shape:
{"facts": [{"facility_id": "...", "statement": "...", "value": "...", "source_field": "..."}]}
Example:
{"facts": [{"facility_id": "VA0000000", "statement": "2 reported numeric limit violations in the window (reported or alleged).", "value": "2", "source_field": "numeric_violation_count"}]}"""


class ComplianceFactLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    facility_id: str
    statement: str
    value: str
    source_field: str

    @field_validator("value", "facility_id", "source_field", mode="before")
    @classmethod
    def _to_str(cls, v: Any) -> Any:
        return str(v) if isinstance(v, (int, float)) and not isinstance(v, bool) else v


class ComplianceLLM(BaseModel):
    model_config = ConfigDict(extra="ignore")
    facts: list[ComplianceFactLLM] = []


def _source_files() -> tuple[str, str]:
    """(violations CSV name, TRI CSV name) from settings, with the known names as defaults."""
    try:
        from ..config import get_settings

        s = get_settings()
        viol = sorted(s.echo_effluent_dir.glob("*.csv"))
        return (viol[0].name if len(viol) == 1 else VIOLATIONS_FILE_DEFAULT), s.tri_csv_path.name
    except Exception:  # noqa: BLE001 - names are cosmetic; never fail the chain over them
        return VIOLATIONS_FILE_DEFAULT, TRI_FILE_DEFAULT


def field_values(result: dict[str, Any], field: str, columns: dict[str, str]) -> list[Any] | None:
    """Values of `field` in a tool result, or None when the field does not exist there."""
    field = field.strip()
    if field in result and not isinstance(result[field], (list, dict)):
        return [result[field]]
    m = _LIST_FIELD.match(field)
    if m and isinstance(result.get(m.group(1)), list):
        items = result[m.group(1)]
        if any(isinstance(i, dict) and m.group(2) in i for i in items):
            return [i.get(m.group(2)) for i in items if isinstance(i, dict)]
        return None
    if field in columns:
        key = columns[field]
        items = result.get("violations") if "violations" in result else result.get("releases")
        if isinstance(items, list):
            return [i.get(key) for i in items if isinstance(i, dict) and field in (i.get("source_fields") or [])]
    return None


def value_matches(value: str, candidates: list[Any]) -> bool:
    """True when value equals one of the field's values (numbers compared numerically)."""
    v = value.strip()
    if not v:
        return False
    for fv in candidates:
        if fv is None or isinstance(fv, (list, dict)):
            continue
        if isinstance(fv, (int, float)) and not isinstance(fv, bool):
            try:
                if float(v.replace(",", "")) == float(fv):
                    return True
            except ValueError:
                pass
            continue
        s = str(fv)
        if v.casefold() == s.casefold() or (len(v) >= 3 and v.casefold() in s.casefold()):
            return True
    return False


def gather(trace: TraceResult, tools: Tools) -> tuple[dict[str, dict[str, Any]], dict[str, Any]]:
    """Call the violation and TRI tools for each candidate. Returns ({id: {violations, tri}}, context)."""
    data: dict[str, dict[str, Any]] = {}
    for c in trace.candidates:
        data[c.facility_id] = {
            "violations": tools.get_facility_violations(c.facility_id),
            "tri": tools.get_tri_releases(c.facility_id),
        }
    ctx = {"facilities": [{"facility_id": fid, **d} for fid, d in data.items()]}
    return data, ctx


def finalize(
    llm: ComplianceLLM, data: dict[str, dict[str, Any]], ctx: dict[str, Any], tools: Tools
) -> tuple[ComplianceResult, list[str]]:
    notes: list[str] = []
    ledger = tools.ledger
    known = ledger.facility_ids()
    viol_file, tri_file = _source_files()
    extra = fallback.context_numbers(ctx)
    facts: list[ComplianceFact] = []
    seen: set[tuple[str, str, str]] = set()
    for f in llm.facts:
        fid = f.facility_id.strip()
        if fid not in data or fid not in known:
            notes.append(f"compliance: rejected fact for {fid} (not a trace candidate returned by the tools)")
            continue
        source_file = None
        found_field = False
        for key, columns, fname in (("violations", VIOLATION_COLUMNS, viol_file), ("tri", TRI_COLUMNS, tri_file)):
            values = field_values(data[fid][key], f.source_field, columns)
            if values is None:
                continue
            found_field = True
            if value_matches(f.value, values):
                source_file = fname
                break
        if source_file is None:
            why = "value not found in that field" if found_field else "source_field is not a tool field"
            notes.append(f"compliance: rejected a fact for {fid} ({why})")
            continue
        stmt = fallback.clean_all([f.statement], ledger, extra, f"compliance {fid}", notes)
        if not stmt:
            notes.append(f"compliance: dropped a fact for {fid} whose statement failed the guardrails")
            continue
        k = (fid, f.source_field.strip(), f.value.strip())
        if k in seen:
            continue
        seen.add(k)
        facts.append(ComplianceFact(
            facility_id=fid, statement=stmt[0], value=f.value.strip(),
            source_field=f.source_field.strip(), source_file=source_file,
        ))
    return ComplianceResult(facts=facts), notes


def run(
    scenario: ScenarioResult, tools: Tools, client: Any, trace: TraceResult
) -> tuple[ComplianceResult, AgentMode, str | None, list[str]]:
    if not trace.candidates:
        return ComplianceResult(facts=[]), "skipped", "no trace candidates", []
    data, ctx = gather(trace, tools)
    user = (
        "TOOL RESULTS (JSON):\n" + fallback.to_json(ctx)
        + "\n\nTASK: List the compliance facts for these facilities. Return only the JSON object."
    )
    llm, mode, reason = run_agent(
        client, SYSTEM_PROMPT, user, ComplianceLLM,
        lambda: ComplianceLLM.model_validate(fallback.compliance(ctx)),
    )
    result, notes = finalize(llm, data, ctx, tools)
    return result, mode, reason, notes


def fallback_result(trace: TraceResult, tools: Tools) -> tuple[ComplianceResult, list[str]]:
    """Deterministic result without any model call."""
    if not trace.candidates:
        return ComplianceResult(facts=[]), []
    data, ctx = gather(trace, tools)
    return finalize(ComplianceLLM.model_validate(fallback.compliance(ctx)), data, ctx, tools)
