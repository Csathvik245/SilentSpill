"""Runs Triage -> Trace -> Compliance -> Report on a scenario and returns the IncidentResponse.

run_chain never raises: an unexpected exception inside an agent switches that agent to its
deterministic fallback (reason = exception type name), and a failing fallback to a minimal result.
"""
from __future__ import annotations

from collections.abc import Callable
from typing import Any, Literal, TypeVar

import networkx as nx

from ..schemas import (
    AgentMode,
    ComplianceResult,
    IncidentReport,
    IncidentResponse,
    RelatedPastEvent,
    TraceResult,
    TriageResult,
)
from ..simulation.scenario import ScenarioResult, run_spill_scenario, run_storm_scenario
from . import compliance_agent, fallback, memory, report_agent, trace_agent, triage_agent
from .base import GroqJSONClient
from .guardrails import enforce_flags
from .memory import IncidentMemory, MemoryResult
from .tools import ToolLedger, Tools

AGENTS = ("triage", "trace", "compliance", "report")
T = TypeVar("T")


class _Unavailable:
    """Stand-in client when GroqJSONClient itself cannot be built."""

    available = False


def make_client(settings: Any = None) -> Any:
    try:
        return GroqJSONClient(settings=settings, temperature=0.1, timeout=30.0)
    except Exception:  # noqa: BLE001 - a broken client means template mode, never a crash
        return _Unavailable()


def incident_id(scenario: ScenarioResult) -> str:
    return f"{scenario.case}-{scenario.scenario_timestamp:%Y%m%dT%H%MZ}"


def _guarded(
    name: str,
    primary: Callable[[], tuple[T, AgentMode, str | None, list[str]]],
    secondary: Callable[[], tuple[T, list[str]]],
    minimal: Callable[[], T],
) -> tuple[T, AgentMode, str | None, list[str]]:
    """Run an agent; on any exception use its fallback, and if that fails too a minimal result."""
    try:
        return primary()
    except Exception as e:  # noqa: BLE001
        reason = type(e).__name__
    try:
        result, notes = secondary()
        return result, "fallback", reason, notes
    except Exception as e2:  # noqa: BLE001
        return minimal(), "fallback", reason, [f"{name}: fallback also failed ({type(e2).__name__}); minimal result used"]


def _minimal_report(scenario: ScenarioResult, trace: TraceResult, reason: str | None) -> IncidentReport:
    try:
        limitations = report_agent.required_limitations(scenario)
    except Exception:  # noqa: BLE001
        limitations = ["Simulated or observed data; see data notes."]
    return IncidentReport(
        summary="Draft incident report could not be generated in full; requires human review before any action.",
        timeline=[],
        ranked_candidates=list(trace.candidates),
        confidence=scenario.fusion.confidence,
        assumptions=list(trace.assumptions),
        limitations=limitations,
        classification=scenario.fusion.classification,
        data_origin=scenario.data_origin,
        report_mode="fallback",
        fallback_reason=reason,
    )


def _recall(
    scenario: ScenarioResult, tools: Any, store: IncidentMemory | None
) -> tuple[list[RelatedPastEvent], list[str], MemoryResult]:
    """Related past events on this reach, through the guardrails. Never raises."""
    if store is None:
        return [], [], MemoryResult("skipped", "incident memory not enabled for this run")
    try:
        query = memory.recall_query(scenario.reach_nhdplusid, scenario.fusion.classification)
        result = store.recall(scenario.reach_nhdplusid, query)
        if result.mode != "backboard" or not result.value:
            return [], [], result
        # The guardrails accept only facilities this run's tools returned: list them in the ledger
        # first (storm runoff has none, so every facility in a memory is rejected there).
        tools.find_candidate_facilities()
        events, notes = memory.sanitize_memories(result.value, tools.ledger)
        return events, notes, result
    except Exception as e:  # noqa: BLE001
        return [], [], MemoryResult("failed", type(e).__name__)


def _save(scenario: ScenarioResult, response: IncidentResponse, store: IncidentMemory | None) -> MemoryResult:
    if store is None:
        return MemoryResult("skipped", "incident memory not enabled for this run")
    try:
        content, metadata = memory.run_memory(
            response.incident_id, scenario.reach_nhdplusid, scenario.scenario_timestamp.date(),
            scenario.fusion.classification, scenario.fusion.confidence, memory.evidence_summary(scenario.fusion),
        )
        return store.save(content, metadata)
    except Exception as e:  # noqa: BLE001
        return MemoryResult("failed", type(e).__name__)


def run_chain_detailed(
    scenario: ScenarioResult, client: Any = None, settings: Any = None, G: nx.DiGraph | None = None,
    memory_store: IncidentMemory | None = None,
) -> tuple[IncidentResponse, dict[str, str | None]]:
    """run_chain plus each agent's fallback reason (None when the model was used).

    With memory_store, related past events on the reach are recalled before Triage (as
    prior_context, through the guardrails) and the completed run is saved afterwards.
    """
    if client is None:
        client = make_client(settings)
    try:
        tools: Any = Tools(scenario, G=G, ledger=ToolLedger())
    except Exception:  # noqa: BLE001 - every agent then falls through to its minimal result
        tools = None
    modes: dict[str, AgentMode] = {}
    reasons: dict[str, str | None] = {}
    all_notes: list[str] = []

    prior, notes, recalled = _recall(scenario, tools, memory_store)
    all_notes += notes

    triage, modes["triage"], reasons["triage"], notes = _guarded(
        "triage",
        lambda: triage_agent.run(scenario, tools, client, prior),
        lambda: triage_agent.fallback_result(scenario, tools, prior),
        lambda: TriageResult(
            classification=fallback.map_fusion(scenario.fusion.classification),
            fusion_classification=scenario.fusion.classification,
            confidence=scenario.fusion.confidence,
            reasons=list(scenario.fusion.reasons) or ["Follows the fusion classification."],
        ),
    )
    all_notes += notes

    trace, modes["trace"], reasons["trace"], notes = _guarded(
        "trace",
        lambda: trace_agent.run(scenario, tools, client, triage),
        lambda: trace_agent.fallback_result(scenario, tools),
        lambda: TraceResult(
            candidates=[c.model_copy(deep=True) for c in scenario.candidates]
            if trace_agent.should_trace(scenario) else [],
        ),
    )
    all_notes += notes

    compliance, modes["compliance"], reasons["compliance"], notes = _guarded(
        "compliance",
        lambda: compliance_agent.run(scenario, tools, client, trace),
        lambda: compliance_agent.fallback_result(trace, tools),
        lambda: ComplianceResult(facts=[]),
    )
    all_notes += notes

    prior_notes = list(all_notes)
    report, modes["report"], reasons["report"], report_notes = _guarded(
        "report",
        lambda: report_agent.run(scenario, tools, client, triage, trace, compliance, prior_notes),
        lambda: report_agent.fallback_result(scenario, tools, triage, trace, compliance, "", prior_notes),
        lambda: _minimal_report(scenario, trace, None),
    )
    # Exception paths: the report carries the reason and every note.
    report.report_mode = modes["report"]
    report.fallback_reason = reasons["report"] if modes["report"] == "fallback" else None
    report.guardrail_notes = prior_notes + list(report_notes)
    report.related_past_events = prior
    report.related_past_events_text = memory.describe(prior)

    response = IncidentResponse(
        incident_id=incident_id(scenario),
        triage=triage,
        trace=trace,
        compliance=compliance,
        report=report,
        agent_modes=modes,
    )
    try:
        response = enforce_flags(response)
    except Exception:  # noqa: BLE001 - schema defaults already set both flags
        pass
    saved = _save(scenario, response, memory_store)
    response.memory_mode, response.memory_reason = memory.combine(recalled, saved)
    return response, reasons


def run_chain(
    scenario: ScenarioResult, client: Any = None, settings: Any = None, G: nx.DiGraph | None = None,
    memory_store: IncidentMemory | None = None,
) -> IncidentResponse:
    """Triage -> Trace -> Compliance -> Report, with guardrails and fallbacks. Never raises."""
    return run_chain_detailed(scenario, client=client, settings=settings, G=G, memory_store=memory_store)[0]


def run_scenario(
    case: Literal["spill", "storm"], client: Any = None, settings: Any = None, G: nx.DiGraph | None = None,
    memory_store: IncidentMemory | None = None,
) -> IncidentResponse:
    """Run the deterministic scenario pipeline, then the agent chain."""
    if case == "spill":
        scenario = run_spill_scenario(settings=settings, G=G)
    elif case == "storm":
        scenario = run_storm_scenario(settings=settings, G=G)
    else:
        raise ValueError(f"unknown case {case!r}; expected 'spill' or 'storm'")
    return run_chain(scenario, client=client, settings=settings, G=G, memory_store=memory_store)

