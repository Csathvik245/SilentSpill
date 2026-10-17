"""Tests for the step 7 agent chain (Triage -> Trace -> Compliance -> Report), written from the contract.

The Groq client is a scripted fake routed by agent (keyword in the system prompt); no live calls.
The real spill and storm scenarios run once per module and are skipped without a .env.
"""
from __future__ import annotations

import dataclasses
import json
import re
from types import SimpleNamespace
from typing import Any, Callable

import groq
import httpx
import pytest
from pydantic import BaseModel, SecretStr

from backend.app.agents import orchestrator
from backend.app.agents.base import GroqJSONClient
from backend.app.agents.orchestrator import run_chain
from backend.app.config import ConfigError, get_settings
from backend.app.schemas import (ComplianceResult, IncidentReport, IncidentResponse, ResponseBase, TraceResult,
                                 TriageResult)
from backend.app.simulation.scenario import run_spill_scenario, run_storm_scenario

KEY = "gsk_TESTKEY_should_never_leak"
MODEL = "test-model"
NO_KEY_REASON = "GROQ_API_KEY or GROQ_MODEL not set"
AGENTS = ("triage", "trace", "compliance", "report")
ACCUSATORY = re.compile(
    r"\b(caused|cause of|responsible|responsibility|polluter|source of the spill|culprit|to blame|"
    r"dumped|dumping|guilty)\b",
    re.IGNORECASE,
)
FUSION_MAP = {"incident": "incident", "flag_for_review": "flag_for_review", "storm_runoff": "storm_runoff",
              "no_incident": "noise"}
NEUTRAL_SPILL = "Candidate facilities are listed by river distance for human review."
NEUTRAL_STORM = "The readings are consistent with storm runoff after rain at the gauge."


# ---- fakes -----------------------------------------------------------------------------------
def fake_settings(key: str | None = KEY, model: str | None = MODEL) -> SimpleNamespace:
    return SimpleNamespace(groq_api_key=SecretStr(key) if key else None, groq_model=model,
                           groq_configured=bool(key and model), simulation_mode=True)


def completion(content: str) -> SimpleNamespace:
    msg = SimpleNamespace(content=content, role="assistant")
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop", index=0)])


def rate_limit_error(retry_after: str = "1") -> groq.RateLimitError:
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    return groq.RateLimitError("Rate limit reached", response=httpx.Response(
        429, headers={"retry-after": retry_after}, request=req), body=None)


def detect_agent(kwargs: dict) -> str | None:
    """Which agent a call is for: '<name> agent' in the system prompt, else the earliest keyword."""
    msgs = kwargs.get("messages") or []
    system = " ".join(str(m.get("content", "")) for m in msgs if m.get("role") == "system").lower()
    m = re.search(r"\b(triage|trace|compliance|report)\s+agent\b", system)
    if m:
        return m.group(1)
    hits = [(system.find(a), a) for a in AGENTS if a in system]
    if hits:
        return min(hits)[1]
    text = json.dumps(msgs).lower()  # last resort: schema field hints
    for hint, agent in (('"facts"', "compliance"), ('"candidates"', "trace"), ('"summary"', "report"),
                        ('"classification"', "triage")):
        if hint in text:
            return agent
    return None


class ScriptedGroq:
    """Fake groq client. scripts[agent] is a list consumed in order; then defaults[agent] repeats.

    Items may be dicts (sent as JSON), raw strings, exceptions (raised) or callables(kwargs).
    Unrouted calls get the union of all default payloads.
    """

    def __init__(self, defaults: dict[str, Any], scripts: dict[str, list] | None = None):
        self.defaults = defaults
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.calls: list[dict] = []
        self.routed: list[str | None] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def count(self, agent: str) -> int:
        return sum(1 for a in self.routed if a == agent)

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        agent = detect_agent(kwargs)
        self.routed.append(agent)
        if agent is None:
            item: Any = {k: v for d in self.defaults.values() for k, v in d.items()}
        elif self.scripts.get(agent):
            item = self.scripts[agent].pop(0)
        else:
            item = self.defaults[agent]
        if callable(item) and not isinstance(item, BaseException):
            item = item(kwargs)
        if isinstance(item, BaseException):
            raise item
        return completion(item if isinstance(item, str) else json.dumps(item))


class Sleeps(list):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


def good_responses(scenario) -> dict[str, dict]:
    """Well-behaved model output shaped like TriageLLM / TraceLLM / ComplianceLLM / ReportLLM."""
    cls = FUSION_MAP[scenario.fusion.classification]
    cands = scenario.candidates
    facts = []
    for c in cands:
        if c.violations:
            facts.append({"facility_id": c.facility_id,
                          "statement": "The facility has a reported numeric limit violation for this parameter.",
                          "value": c.violations[0].parameter, "source_field": "violations[].parameter"})
    storm = scenario.fusion.classification == "storm_runoff"
    return {
        "triage": {"classification": cls, "reasons": ["The fused signals were reviewed against rain context."]},
        "trace": {"candidates": [{"facility_id": c.facility_id, "name": c.name,
                                  "note": "Listed by river distance from the gauge."} for c in cands],
                  "assumptions": ["Upstream tracing follows the river network."]},
        "compliance": {"facts": facts},
        "report": {"summary": NEUTRAL_STORM if storm else NEUTRAL_SPILL,
                   "extra_assumptions": [], "extra_limitations": []},
    }


def chain(scenario, scripts: dict[str, list] | None = None, defaults: dict | None = None,
          settings: SimpleNamespace | None = None, **client_kw):
    fake = ScriptedGroq(defaults or good_responses(scenario), scripts)
    sleeps = Sleeps()
    client = GroqJSONClient(settings=settings or fake_settings(), client=fake, sleep=sleeps, **client_kw)
    resp = run_chain(scenario, client=client)
    return resp, fake, sleeps


# ---- helpers ---------------------------------------------------------------------------------
def iter_models(obj) -> Any:
    if isinstance(obj, BaseModel):
        yield obj
        for name in type(obj).model_fields:
            yield from iter_models(getattr(obj, name))
    elif isinstance(obj, (list, tuple)):
        for x in obj:
            yield from iter_models(x)
    elif isinstance(obj, dict):
        for x in obj.values():
            yield from iter_models(x)


def prose(resp: IncidentResponse) -> list[str]:
    """All model-influenced text (not guardrail notes, which may quote what was removed)."""
    r = resp.report
    out = list(resp.triage.reasons) + list(resp.trace.candidate_notes.values()) + list(resp.trace.assumptions)
    out += [f.statement for f in resp.compliance.facts]
    out += [r.summary] + list(r.assumptions) + list(r.limitations) + [t.event for t in r.timeline]
    return out


def all_notes(resp: IncidentResponse) -> str:
    return " ".join(resp.report.guardrail_notes)


def check_response(resp: IncidentResponse) -> IncidentResponse:
    assert isinstance(resp, IncidentResponse)
    IncidentResponse.model_validate_json(resp.model_dump_json())
    TriageResult.model_validate(resp.triage.model_dump())
    TraceResult.model_validate(resp.trace.model_dump())
    ComplianceResult.model_validate(resp.compliance.model_dump())
    IncidentReport.model_validate(resp.report.model_dump())
    assert set(resp.agent_modes) == set(AGENTS)
    assert resp.agent_modes["report"] == resp.report.report_mode
    for m in iter_models(resp):
        if isinstance(m, ResponseBase):
            assert m.requires_human_review is True
            assert m.simulated is True  # SIMULATION_MODE is true for the demo (checked in fixture)
    for s in prose(resp):
        assert not ACCUSATORY.search(s), s
    dump = resp.model_dump_json()
    assert KEY not in dump
    return resp


# ---- fixtures --------------------------------------------------------------------------------
@pytest.fixture(scope="module")
def settings():
    try:
        s = get_settings()
    except ConfigError:
        pytest.skip("no .env")
    if not s.simulation_mode:
        pytest.skip("SIMULATION_MODE is false")
    return s


@pytest.fixture(scope="module")
def spill(settings):
    sc = run_spill_scenario(settings)
    assert sc.fusion.classification == "incident" and sc.candidates
    return sc


@pytest.fixture(scope="module")
def storm(settings):
    sc = run_storm_scenario(settings)
    assert sc.fusion.classification == "storm_runoff" and not sc.candidates
    return sc


@pytest.fixture(scope="module")
def spill_llm(spill):
    return chain(spill)


@pytest.fixture(scope="module")
def spill_fallback(spill):
    return chain(spill, settings=fake_settings(key=None))


@pytest.fixture(scope="module")
def storm_fallback(storm):
    return chain(storm, settings=fake_settings(key=None))


# ---- happy path ------------------------------------------------------------------------------
def test_spill_all_agents_llm_mode(spill, spill_llm):
    resp, fake, _ = spill_llm
    check_response(resp)
    assert resp.agent_modes == {a: "llm" for a in AGENTS}
    assert resp.report.report_mode == "llm" and resp.report.fallback_reason is None
    assert NEUTRAL_SPILL in resp.report.summary
    assert resp.triage.classification == "incident"
    assert resp.triage.confidence == pytest.approx(spill.fusion.confidence)
    assert resp.report.confidence == pytest.approx(spill.fusion.confidence)
    assert resp.report.classification == "incident"
    assert resp.report.data_origin == spill.data_origin
    assert resp.report.banner
    assert resp.report.timeline
    assert {a for a in fake.routed if a} >= {"triage", "report"}
    assert KEY not in repr(fake.calls)


def test_incident_id_deterministic(spill, spill_llm, spill_fallback):
    a, b = spill_llm[0], spill_fallback[0]
    assert a.incident_id == b.incident_id
    assert a.incident_id.startswith("spill-")
    assert spill.scenario_timestamp.strftime("%Y%m%d") in a.incident_id


# ---- required: spill gives a ranked candidate list --------------------------------------------
def test_spill_gives_ranked_candidate_list(spill, spill_llm):
    resp, _, _ = spill_llm
    cands = resp.trace.candidates
    assert [c.facility_id for c in cands] == [c.facility_id for c in spill.candidates]
    assert [c.rank for c in cands] == list(range(1, len(cands) + 1))
    dists = [c.upstream_distance_km for c in cands]
    assert all(d is not None for d in dists) and dists == sorted(dists)
    assert [c.model_dump() for c in cands] == [c.model_dump() for c in spill.candidates]
    assert [c.model_dump() for c in resp.report.ranked_candidates] == [c.model_dump() for c in cands]
    assert cands[0].facility_id == "VA0052621" and cands[0].name == "VALLEY MILK PRODUCTS LLC"


def test_ranked_list_ignores_model_order_and_numbers(spill):
    reversed_bad = {"candidates": [
        {"facility_id": c.facility_id, "name": c.name,
         "note": f"About 99.9 km upstream with 4242 violations and a 0.01 h travel time (rank {i + 1})."}
        for i, c in enumerate(reversed(spill.candidates))],
        "assumptions": ["Velocity is 77.7 m/s everywhere."]}
    resp, _, _ = chain(spill, scripts={"trace": [reversed_bad]})
    check_response(resp)
    assert resp.agent_modes["trace"] == "llm"
    assert [c.model_dump() for c in resp.trace.candidates] == [c.model_dump() for c in spill.candidates]
    assert [c.rank for c in resp.trace.candidates] == list(range(1, len(spill.candidates) + 1))
    text = " ".join(prose(resp))
    for bad in ("99.9", "4242", "0.01", "77.7"):
        assert bad not in text


def test_model_dropping_a_candidate_keeps_it(spill):
    first_only = {"candidates": [{"facility_id": spill.candidates[0].facility_id,
                                  "name": spill.candidates[0].name, "note": "Closest by river distance."}],
                  "assumptions": []}
    resp, _, _ = chain(spill, scripts={"trace": [first_only]})
    check_response(resp)
    assert [c.facility_id for c in resp.trace.candidates] == [c.facility_id for c in spill.candidates]


# ---- required: hallucinated facility rejected -------------------------------------------------
def test_hallucinated_facility_id_rejected(spill):
    good = good_responses(spill)["trace"]
    bad = {"candidates": [{"facility_id": "VA9999999", "name": "ACME CHEMICAL CORP",
                           "note": "Nearest plant."}] + good["candidates"], "assumptions": []}
    resp, _, _ = chain(spill, scripts={"trace": [bad]})
    check_response(resp)
    ids = [c.facility_id for c in resp.trace.candidates]
    assert "VA9999999" not in ids
    assert ids == [c.facility_id for c in spill.candidates]
    dump = resp.model_dump_json(exclude={"report": {"guardrail_notes"}})
    assert "VA9999999" not in dump and "ACME" not in dump.upper()
    assert "VA9999999" in all_notes(resp)  # the rejected id is named only in the audit note
    assert "ACME" not in resp.model_dump_json().upper()  # a rejected name appears nowhere, notes included


def test_real_id_with_wrong_name_rejected(spill):
    c0 = spill.candidates[0]
    good = good_responses(spill)["trace"]
    cands = [{"facility_id": c0.facility_id, "name": "ACME DAIRY HOLDINGS INC",
              "note": "ACME DAIRY HOLDINGS INC is upstream."}] + good["candidates"][1:]
    resp, _, _ = chain(spill, scripts={"trace": [{"candidates": cands, "assumptions": []}]})
    check_response(resp)
    got = {c.facility_id: c for c in resp.trace.candidates}
    assert got[c0.facility_id].name == c0.name  # name comes from the tool, never the model
    assert "ACME" not in resp.trace.candidate_notes.get(c0.facility_id, "").upper()
    dump = resp.model_dump_json(exclude={"report": {"guardrail_notes"}})
    assert "ACME" not in dump.upper()
    assert resp.report.guardrail_notes and c0.facility_id in all_notes(resp)
    assert "ACME" not in resp.model_dump_json().upper()


def test_fake_company_in_report_prose_dropped(spill):
    summary = (NEUTRAL_SPILL + " ACME CHEMICAL CORP operates a plant near the gauge. "
               "Permit VA7777777 is also listed.")
    resp, _, _ = chain(spill, scripts={"report": [
        {"summary": summary, "extra_assumptions": ["ACME CHEMICAL CORP may discharge here."],
         "extra_limitations": []}]})
    check_response(resp)
    assert resp.report.report_mode == "llm"
    text = " ".join(prose(resp)).upper()
    assert "ACME" not in text and "VA7777777" not in text
    assert NEUTRAL_SPILL in resp.report.summary
    assert resp.report.guardrail_notes
    assert "ACME" not in resp.model_dump_json().upper()  # not even quoted in the guardrail notes


# ---- required: accusatory text stripped -------------------------------------------------------
def test_accusatory_text_stripped_in_chain(spill):
    c0 = spill.candidates[0]
    scripts = {
        "triage": [{"classification": "incident",
                    "reasons": ["A facility upstream caused the spike.", "The polluter is upstream."]}],
        "trace": [{"candidates": [{"facility_id": c.facility_id, "name": c.name,
                                   "note": "This facility is responsible for the spill."}
                                  for c in spill.candidates],
                   "assumptions": ["The closest facility is the source of the spill."]}],
        "report": [{"summary": (f"{c0.name} caused the spill. {c0.name} is responsible for the turbidity rise. "
                                "The polluter was dumping waste. This is the source of the spill. "
                                + NEUTRAL_SPILL),
                    "extra_assumptions": ["The polluter is guilty."], "extra_limitations": []}],
    }
    resp, _, _ = chain(spill, scripts=scripts)
    check_response(resp)  # asserts no accusatory phrase in any prose
    low = " ".join(prose(resp)).lower()
    for word in ("caused", "responsible", "polluter", "source of the spill"):
        assert word not in low
    assert resp.report.guardrail_notes


# ---- required: invalid JSON retried once then falls back -------------------------------------
def test_invalid_json_report_retried_once_then_fallback(spill):
    resp, fake, _ = chain(spill, scripts={"report": ["not json at all", "{still: not json"]})
    check_response(resp)
    assert fake.count("report") == 2
    assert resp.report.report_mode == "fallback" and resp.agent_modes["report"] == "fallback"
    assert resp.report.fallback_reason and KEY not in resp.report.fallback_reason
    assert resp.report.summary.strip()
    assert resp.agent_modes["triage"] == "llm"


def test_invalid_json_triage_retried_once_then_fallback(spill):
    resp, fake, _ = chain(spill, scripts={"triage": ["<<garbage>>", "nope"]})
    check_response(resp)
    assert fake.count("triage") == 2
    assert resp.agent_modes["triage"] == "fallback"
    assert resp.triage.classification == "incident"  # fallback maps fusion
    assert resp.agent_modes["report"] == "llm"


def test_invalid_json_then_valid_stays_llm(spill):
    resp, fake, _ = chain(spill, scripts={"report": ["oops"]})
    check_response(resp)
    assert fake.count("report") == 2 and resp.report.report_mode == "llm"


# ---- required: fallback output passes the same schemas ---------------------------------------
@pytest.mark.parametrize("case", ["spill", "storm"])
def test_fallback_output_passes_schemas(case, spill_fallback, storm_fallback):
    resp, fake, _ = spill_fallback if case == "spill" else storm_fallback
    check_response(resp)
    # Storm runoff gets no candidate list, so trace and compliance are skipped by design, not failed.
    expected = {a: "fallback" for a in AGENTS}
    if case == "storm":
        expected.update(trace="skipped", compliance="skipped")
    assert resp.agent_modes == expected
    assert resp.report.report_mode == "fallback"
    assert resp.report.summary.strip()
    assert resp.report.limitations


def test_missing_key_all_fallback_no_calls(spill_fallback, storm_fallback):
    for resp, fake, _ in (spill_fallback, storm_fallback):
        assert fake.calls == []
        assert resp.report.fallback_reason == NO_KEY_REASON
        assert all(m in ("fallback", "skipped") for m in resp.agent_modes.values())
        assert resp.agent_modes["triage"] == resp.agent_modes["report"] == "fallback"


def test_skipped_means_no_model_call_by_design(storm):
    # A configured client: triage and report call the model; trace and compliance never do.
    resp, fake, _ = chain(storm)
    assert resp.agent_modes == {"triage": "llm", "trace": "skipped", "compliance": "skipped", "report": "llm"}
    assert len(fake.calls) == 2
    assert resp.report.fallback_reason is None  # skipped is not a failure


def test_missing_model_all_fallback_no_calls(spill):
    resp, fake, _ = chain(spill, settings=fake_settings(model=None))
    check_response(resp)
    assert fake.calls == [] and resp.report.fallback_reason == NO_KEY_REASON


def test_fallback_spill_keeps_ranked_list(spill, spill_fallback):
    resp = spill_fallback[0]
    assert [c.model_dump() for c in resp.trace.candidates] == [c.model_dump() for c in spill.candidates]
    assert [c.model_dump() for c in resp.report.ranked_candidates] == [c.model_dump() for c in spill.candidates]
    assert resp.triage.classification == "incident"
    assert resp.triage.confidence == pytest.approx(spill.fusion.confidence)


def test_fallback_is_deterministic(spill, spill_fallback):
    again, _, _ = chain(spill, settings=fake_settings(key=None))
    assert again.model_dump_json() == spill_fallback[0].model_dump_json()


# ---- required: storm gives storm_runoff and no blame list -------------------------------------
def _storm_blame_scripts(storm_sc) -> dict[str, list]:
    return {
        "triage": [{"classification": "storm_runoff", "reasons": ["Rain preceded the rise."]}],
        "trace": [{"candidates": [{"facility_id": "VA0052621", "name": "VALLEY MILK PRODUCTS LLC",
                                   "note": "Upstream dairy."}], "assumptions": []}],
        "compliance": [{"facts": [{"facility_id": "VA0052621", "statement": "Has violations.",
                                   "value": "3", "source_field": "numeric_violation_count"}]}],
        "report": [{"summary": ("VALLEY MILK PRODUCTS LLC caused the turbidity rise. " + NEUTRAL_STORM),
                    "extra_assumptions": [], "extra_limitations": []}],
    }


@pytest.mark.parametrize("mode", ["llm", "fallback"])
def test_storm_gives_storm_runoff_no_blame_list(storm, storm_fallback, mode):
    if mode == "llm":
        resp, _, _ = chain(storm, scripts=_storm_blame_scripts(storm))
    else:
        resp = storm_fallback[0]
    check_response(resp)
    assert resp.incident_id.startswith("storm-")
    assert resp.triage.classification == "storm_runoff"
    assert resp.report.classification == "storm_runoff"
    assert resp.trace.candidates == [] and resp.report.ranked_candidates == []
    assert resp.compliance.facts == []
    s = resp.report.summary
    assert "storm" in s.lower()
    assert not ACCUSATORY.search(s)
    assert "VA0052621" not in s and "VALLEY MILK" not in s.upper()
    assert resp.report.data_origin == storm.data_origin
    assert resp.report.confidence == pytest.approx(storm.fusion.confidence)


# ---- triage cannot upgrade -------------------------------------------------------------------
def test_triage_cannot_upgrade_flag_for_review(spill):
    flagged = dataclasses.replace(spill, fusion=spill.fusion.model_copy(
        update={"classification": "flag_for_review", "confidence": 0.35}))
    resp, _, _ = chain(flagged, scripts={"triage": [
        {"classification": "incident", "reasons": ["Strong multi-source evidence."]}]})
    check_response(resp)
    assert resp.triage.classification == "flag_for_review"
    assert resp.triage.fusion_classification == "flag_for_review"
    assert resp.triage.confidence == pytest.approx(0.35)
    assert resp.report.confidence == pytest.approx(0.35)
    assert resp.report.guardrail_notes
    # flag_for_review with candidates still gets a ranked list
    assert [c.facility_id for c in resp.trace.candidates] == [c.facility_id for c in spill.candidates]


def test_triage_cannot_turn_storm_into_incident(storm):
    resp, _, _ = chain(storm, scripts={"triage": [{"classification": "incident", "reasons": ["Big spike."]}]})
    check_response(resp)
    assert resp.triage.classification == "storm_runoff"
    assert resp.trace.candidates == []


def test_triage_cannot_relabel_incident_as_storm(spill):
    resp, _, _ = chain(spill, scripts={"triage": [{"classification": "storm_runoff", "reasons": ["Rain."]}]})
    check_response(resp)
    assert resp.triage.classification == "incident"
    assert resp.triage.confidence == pytest.approx(spill.fusion.confidence)


def test_triage_may_downgrade(spill):
    resp, _, _ = chain(spill, scripts={"triage": [{"classification": "flag_for_review",
                                                    "reasons": ["Needs a closer look."]}]})
    check_response(resp)
    assert resp.triage.classification == "flag_for_review"
    assert resp.triage.confidence <= spill.fusion.confidence


# ---- numbers not in tools are dropped --------------------------------------------------------
def test_numbers_not_in_tools_dropped(spill):
    resp, _, _ = chain(spill, scripts={
        "triage": [{"classification": "incident", "reasons": ["Conductivity peaked at 4242.42 uS/cm.",
                                                               "Both sensor and camera agree."]}],
        "report": [{"summary": NEUTRAL_SPILL + " The plume travelled 31.337 km in 0.123 h.",
                    "extra_assumptions": ["About 8888 people live nearby."], "extra_limitations": []}],
    })
    check_response(resp)
    text = " ".join(prose(resp))
    for bad in ("4242.42", "31.337", "0.123", "8888"):
        assert bad not in text
    assert NEUTRAL_SPILL in resp.report.summary
    assert "Both sensor and camera agree." in " ".join(resp.triage.reasons)
    assert resp.report.guardrail_notes


# ---- compliance facts ------------------------------------------------------------------------
def test_compliance_keeps_only_verbatim_tool_facts(spill):
    c0 = next(c for c in spill.candidates if c.violations)
    real = {"facility_id": c0.facility_id, "statement": "A reported numeric limit violation is on record.",
            "value": c0.violations[0].parameter, "source_field": "violations[].parameter"}
    bad = [
        {"facility_id": "VA9999999", "statement": "Unknown permit.", "value": "1", "source_field": "numeric_violation_count"},
        {"facility_id": c0.facility_id, "statement": "Made up.", "value": "Mercury, total", "source_field": "violations[].parameter"},
        {"facility_id": c0.facility_id, "statement": "Bad field.", "value": c0.violations[0].parameter, "source_field": "secret_field"},
        {"facility_id": c0.facility_id, "statement": "Wrong count.", "value": "424242", "source_field": "numeric_violation_count"},
    ]
    resp, _, _ = chain(spill, scripts={"compliance": [{"facts": [real] + bad}]})
    check_response(resp)
    facts = resp.compliance.facts
    assert any(f.facility_id == c0.facility_id and f.value == real["value"]
               and f.source_field == "violations[].parameter" for f in facts)
    cand_ids = {c.facility_id for c in spill.candidates}
    for f in facts:
        assert f.facility_id in cand_ids
        assert f.source_file and f.source_file.lower().endswith(".csv")
        assert f.value not in ("Mercury, total", "424242")
        assert f.source_field != "secret_field"
    assert "VA9999999" not in resp.compliance.model_dump_json()


# ---- 429 backoff -----------------------------------------------------------------------------
def test_rate_limit_backoff_then_success_in_chain(spill):
    resp, fake, sleeps = chain(spill, scripts={"triage": [rate_limit_error("1"), rate_limit_error("1")]})
    check_response(resp)
    assert resp.agent_modes["triage"] == "llm"
    assert fake.count("triage") == 3
    assert len(sleeps) >= 2 and all(s >= 0 for s in sleeps)


def test_rate_limit_exhausted_falls_back_in_chain(spill):
    resp, fake, sleeps = chain(spill, scripts={"report": [rate_limit_error("0")] * 10})
    check_response(resp)
    assert resp.report.report_mode == "fallback"
    assert resp.report.fallback_reason and KEY not in resp.report.fallback_reason
    assert fake.count("report") <= 4


# ---- required limitations in both modes -------------------------------------------------------
LIMITATION_CHECKS: dict[str, Callable[[str], bool]] = {
    "coordinates": lambda s: "coordinate" in s or "surveyed" in s or "reported location" in s,
    "rain daily": lambda s: "daily" in s and "no rain reading" in s,
    "lower bound": lambda s: "≥" in s or "lower bound" in s,
    "proxies": lambda s: "proxy" in s or "proxies" in s,
    "roboflow": lambda s: ("roboflow" in s or "debris" in s) and "plume" in s,
    "violations": lambda s: "violation" in s and ("reported" in s or "alleged" in s) and "window" in s,
    "tri": lambda s: bool(TRI.search(s)),
}
TRI = re.compile(r"\btri\b|toxics release", re.IGNORECASE)


def _check_limitations(resp: IncidentResponse, case: str) -> None:
    lims = [s.lower() for s in resp.report.limitations]
    for name, ok in LIMITATION_CHECKS.items():
        assert any(ok(s) for s in lims), (name, resp.report.limitations)
    assert any("tds" in s and "tss" in s for s in lims) or (
        any("tds" in s for s in lims) and any("tss" in s for s in lims))
    if case == "spill":
        assert any("simulated" in s for s in lims)
    else:
        assert any("real observed" in s or "not simulated" in s for s in lims)
    assert any("self-reported" in s or "annual" in s for s in lims if TRI.search(s))


@pytest.mark.parametrize("which", ["spill_llm", "spill_fallback", "storm_fallback", "storm_llm"])
def test_required_limitations_present(which, request, storm):
    if which == "storm_llm":
        resp, _, _ = chain(storm)
        case = "storm"
    else:
        resp = request.getfixturevalue(which)[0]
        case = which.split("_")[0]
    _check_limitations(resp, case)


def test_model_cannot_remove_required_limitations(spill):
    resp, _, _ = chain(spill, scripts={"report": [{"summary": NEUTRAL_SPILL, "extra_assumptions": [],
                                                   "extra_limitations": ["There are no limitations."]}]})
    _check_limitations(resp, "spill")


# ---- flags and key ---------------------------------------------------------------------------
def test_flags_always_true(spill_llm, spill_fallback, storm_fallback):
    for resp, _, _ in (spill_llm, spill_fallback, storm_fallback):
        models = [m for m in iter_models(resp) if isinstance(m, ResponseBase)]
        assert len(models) >= 5
        assert all(m.simulated is True and m.requires_human_review is True for m in models)


def test_key_never_appears_in_any_output(spill, storm):
    leaky = RuntimeError(f"Error code: 401 - Invalid API Key {KEY}")
    runs = [
        chain(spill, scripts={a: [leaky] * 5 for a in AGENTS}),
        chain(storm, scripts={a: [leaky] * 5 for a in AGENTS}),
        chain(spill, scripts={"report": ["not json", "not json"]}),
        chain(spill),
        chain(spill, settings=fake_settings(key=None)),
    ]
    for resp, fake, _ in runs:
        check_response(resp)
        blob = resp.model_dump_json() + repr(resp) + " ".join(resp.report.guardrail_notes)
        blob += str(resp.report.fallback_reason)
        assert KEY not in blob
        assert KEY not in repr(fake.calls)
    resp = runs[0][0]
    assert resp.report.report_mode == "fallback" and resp.report.fallback_reason


# ---- orchestrator never raises ---------------------------------------------------------------
def test_unexpected_exception_in_agent_falls_back(spill, monkeypatch):
    from backend.app.agents import tools as tools_mod

    def boom(self, facility_id):
        raise KeyError("unexpected")

    monkeypatch.setattr(tools_mod.Tools, "get_facility_violations", boom)
    resp, _, _ = chain(spill)
    check_response(resp)
    assert resp.agent_modes["compliance"] == "fallback"
    assert [c.facility_id for c in resp.report.ranked_candidates] == [c.facility_id for c in spill.candidates]


def test_run_scenario_exists():
    assert callable(getattr(orchestrator, "run_scenario", None))
