"""Tests for backend.app.agents guardrails, tools and the Groq JSON client, written from the step 7 contract.

No network: the Groq client is a scripted fake exposing .chat.completions.create(**kwargs).
Tool/ledger tests use the real spill scenario (module-scoped; skipped without a .env).
"""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import groq
import httpx
import pytest
from pydantic import BaseModel, SecretStr

from backend.app.agents import guardrails
from backend.app.agents.base import AgentError, GroqJSONClient, run_agent
from backend.app.agents.guardrails import (clean_text, enforce_flags, number_tokens, strip_accusatory,
                                           unknown_facilities, unknown_numbers)
from backend.app.agents.tools import ToolLedger, Tools
from backend.app.config import ConfigError, get_settings
from backend.app.schemas import TriageResult
from backend.app.simulation.scenario import run_spill_scenario

KEY = "gsk_TESTKEY_should_never_leak"
MODEL = "test-model"
NO_KEY_REASON = "GROQ_API_KEY or GROQ_MODEL not set"
ACCUSATORY = re.compile(
    r"\b(caused|cause of|responsible|responsibility|polluter|source of the spill|culprit|to blame|"
    r"dumped|dumping|guilty)\b",
    re.IGNORECASE,
)


# ---- fakes -----------------------------------------------------------------------------------
def fake_settings(key: str | None = KEY, model: str | None = MODEL) -> SimpleNamespace:
    return SimpleNamespace(
        groq_api_key=SecretStr(key) if key else None,
        groq_model=model,
        groq_configured=bool(key and model),
        simulation_mode=True,
    )


def completion(content: str) -> SimpleNamespace:
    msg = SimpleNamespace(content=content, role="assistant")
    return SimpleNamespace(choices=[SimpleNamespace(message=msg, finish_reason="stop", index=0)])


def rate_limit_error(retry_after: str | None = "1") -> groq.RateLimitError:
    headers = {"retry-after": retry_after} if retry_after is not None else {}
    req = httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")
    resp = httpx.Response(429, headers=headers, request=req)
    return groq.RateLimitError("Rate limit reached", response=resp, body=None)


class FakeGroq:
    """Returns (or raises) scripted items in order; the last item repeats."""

    def __init__(self, *script):
        self.script = list(script)
        self.calls: list[dict] = []
        self.chat = SimpleNamespace(completions=SimpleNamespace(create=self._create))

    def _create(self, **kwargs):
        self.calls.append(kwargs)
        item = self.script[min(len(self.calls) - 1, len(self.script) - 1)]
        if isinstance(item, BaseException):
            raise item
        return completion(item if isinstance(item, str) else json.dumps(item))


class Sleeps(list):
    def __call__(self, seconds: float) -> None:
        self.append(seconds)


class Answer(BaseModel):
    answer: str
    count: int


GOOD = {"answer": "ok", "count": 2}


def make(*script, settings=None, **kw):
    fake = FakeGroq(*script)
    sleeps = Sleeps()
    client = GroqJSONClient(settings=settings or fake_settings(), client=fake, sleep=sleeps, **kw)
    return client, fake, sleeps


# ---- strip_accusatory ------------------------------------------------------------------------
ACCUSATORY_CASES = [
    "VALLEY MILK PRODUCTS LLC caused the conductivity spike.",
    "The facility upstream is responsible for the turbidity rise.",
    "The polluter is located upstream of the gauge.",
    "This facility is the source of the spill.",
    "The plant was dumping wastewater and dumped solids.",
]


@pytest.mark.parametrize("text", ACCUSATORY_CASES)
def test_accusatory_text_is_stripped(text):
    clean, notes = strip_accusatory(text)
    assert not ACCUSATORY.search(clean), clean
    assert notes, "each rewrite or dropped sentence must be noted"
    assert KEY not in clean


def test_accusatory_all_four_required_words_in_one_text():
    text = ("Valley Milk caused the spike. The farm is responsible for the rise. "
            "The polluter is upstream. This is the source of the spill. Turbidity rose at the gauge.")
    clean, notes = strip_accusatory(text)
    low = clean.lower()
    for word in ("caused", "responsible", "polluter", "source of the spill"):
        assert word not in low
    assert len(notes) >= 4
    assert "turbidity rose at the gauge" in low  # neutral sentence kept


@pytest.mark.parametrize("text", ACCUSATORY_CASES)
def test_strip_accusatory_is_idempotent(text):
    once, _ = strip_accusatory(text)
    twice, notes = strip_accusatory(once)
    assert twice == once and notes == []


def test_neutral_text_unchanged():
    text = "Conductivity rose at the gauge. The facility has reported violations near this reach."
    assert strip_accusatory(text) == (text, [])


def test_accusatory_is_word_boundaried():
    # "because" and "irresponsibly" style substrings must not trigger rewrites of unrelated words.
    text = "Readings rose because of rain."
    clean, notes = strip_accusatory(text)
    assert clean == text and notes == []


# ---- numbers ---------------------------------------------------------------------------------
def test_number_tokens_normalizes_and_ignores_ids_and_dates():
    toks = number_tokens("Travel time 7.40 h over 2.0 km for VA0052621 on 2026-10-09T20:00Z.")
    assert "7.4" in toks and "2" in toks
    for bad in ("7.40", "2.0", "0052621", "52621", "2026", "10", "09", "9", "20", "00"):
        assert bad not in toks, (bad, toks)


def test_number_tokens_plain_values():
    toks = number_tokens("Confidence 0.95, 3 violations, 17.57 km.")
    assert {"0.95", "3", "17.57"} <= set(toks)


def test_unknown_numbers_reports_only_numbers_not_in_tools():
    allowed = {"7.4", "3"}
    bad = unknown_numbers("Travel 7.40 h, 3 violations, peak 4242.42 uS/cm.", allowed)
    assert "4242.42" in bad
    assert "7.4" not in bad and "3" not in bad and "7.40" not in bad


# ---- facilities ------------------------------------------------------------------------------
IDS = {"VA0052621", "VAG408560"}
NAMES = {"VALLEY MILK PRODUCTS LLC", "ERBACH ENTERPRISES LLC PROPERTY 2"}


def test_unknown_facility_id_reported():
    bad = unknown_facilities("Permit VA9999999 is upstream.", IDS, NAMES)
    assert any("VA9999999" in b for b in bad)


def test_fake_company_name_reported():
    bad = unknown_facilities("Upstream there is (ACME CHEMICAL CORP) near the river.", IDS, NAMES)
    assert any("ACME" in b.upper() for b in bad)


def test_known_facilities_not_reported():
    assert unknown_facilities("VA0052621 (VALLEY MILK PRODUCTS LLC) is listed first.", IDS, NAMES) == []
    assert unknown_facilities("Permit VAG408560 (Erbach Enterprises LLC Property 2).", IDS, NAMES) == []


# ---- enforce_flags ---------------------------------------------------------------------------
def test_enforce_flags_sets_flags():
    try:
        sim = get_settings().simulation_mode
    except ConfigError:
        sim = True
    if not sim:
        pytest.skip("SIMULATION_MODE is false")
    t = TriageResult.model_construct(
        classification="incident", fusion_classification="incident", confidence=0.9, reasons=[],
        simulated=False, requires_human_review=False,
    )
    out = enforce_flags(t)
    assert out.simulated is True and out.requires_human_review is True


# ---- GroqJSONClient --------------------------------------------------------------------------
def test_client_available_with_key_and_model():
    client, fake, _ = make(GOOD)
    assert client.available is True
    assert fake.calls == []


@pytest.mark.parametrize("key,model", [(None, MODEL), (KEY, None), (None, None)])
def test_missing_key_or_model_not_available_and_no_calls(key, model):
    client, fake, _ = make(GOOD, settings=fake_settings(key, model))
    assert client.available is False
    value, mode, reason = run_agent(client, "system", "user", Answer, lambda: Answer(answer="fb", count=0))
    assert mode == "fallback" and reason == NO_KEY_REASON
    assert value == Answer(answer="fb", count=0)
    assert fake.calls == []


def test_complete_json_valid_call_parameters():
    client, fake, _ = make(GOOD)
    out = client.complete_json("You are a test agent.", "Return JSON.", Answer)
    assert out == Answer(**GOOD)
    assert len(fake.calls) == 1
    kw = fake.calls[0]
    assert kw["model"] == MODEL
    assert 0 <= kw["temperature"] <= 0.2
    assert kw["response_format"] == {"type": "json_object"}
    roles = [m["role"] for m in kw["messages"]]
    assert roles[0] == "system" and "user" in roles
    assert KEY not in repr(fake.calls)


def test_default_temperature_is_low():
    client, fake, _ = make(GOOD)
    client.complete_json("s", "u", Answer)
    assert fake.calls[0]["temperature"] == pytest.approx(0.1)


def test_invalid_json_retried_once_then_agent_error():
    client, fake, _ = make("this is not json {", "still not json")
    with pytest.raises(AgentError) as ei:
        client.complete_json("s", "u", Answer)
    assert len(fake.calls) == 2
    assert KEY not in str(ei.value)
    # The retry carries a corrective message, so its messages differ from the first call.
    assert fake.calls[1]["messages"] != fake.calls[0]["messages"]


def test_invalid_json_then_fallback_via_run_agent():
    client, fake, _ = make("not json", "not json either")
    value, mode, reason = run_agent(client, "s", "u", Answer, lambda: Answer(answer="fb", count=0))
    assert len(fake.calls) == 2
    assert mode == "fallback" and value.answer == "fb"
    assert reason and isinstance(reason, str) and KEY not in reason


def test_invalid_json_then_valid_succeeds():
    client, fake, _ = make("oops", GOOD)
    assert client.complete_json("s", "u", Answer) == Answer(**GOOD)
    assert len(fake.calls) == 2


def test_schema_validation_error_retried_with_error_text():
    client, fake, _ = make({"answer": "ok"}, GOOD)  # first lacks "count"
    assert client.complete_json("s", "u", Answer) == Answer(**GOOD)
    assert len(fake.calls) == 2
    retry_text = json.dumps(fake.calls[1]["messages"]).lower()
    assert "count" in retry_text  # the validation error text is passed back


def test_schema_validation_error_twice_falls_back():
    client, fake, _ = make({"answer": "ok"}, {"count": "x"})
    value, mode, reason = run_agent(client, "s", "u", Answer, lambda: Answer(answer="fb", count=0))
    assert len(fake.calls) == 2 and mode == "fallback" and reason


def test_rate_limit_backoff_then_success():
    client, fake, sleeps = make(rate_limit_error("1"), rate_limit_error("1"), GOOD)
    assert client.complete_json("s", "u", Answer) == Answer(**GOOD)
    assert len(fake.calls) == 3
    assert len(sleeps) == 2 and all(s >= 0 for s in sleeps)


def test_rate_limit_without_retry_after_backs_off_exponentially():
    client, fake, sleeps = make(rate_limit_error(None), rate_limit_error(None), GOOD)
    assert client.complete_json("s", "u", Answer) == Answer(**GOOD)
    assert len(sleeps) == 2 and sleeps[0] > 0 and sleeps[1] >= sleeps[0]


def test_rate_limit_exhausted_raises_agent_error():
    client, fake, sleeps = make(rate_limit_error("0"), max_rate_limit_retries=2)
    with pytest.raises(AgentError) as ei:
        client.complete_json("s", "u", Answer)
    assert len(fake.calls) in (2, 3)  # bounded: no endless retry
    assert len(sleeps) <= 2
    assert KEY not in str(ei.value)


def test_rate_limit_exhausted_falls_back():
    client, fake, _ = make(rate_limit_error("0"))
    value, mode, reason = run_agent(client, "s", "u", Answer, lambda: Answer(answer="fb", count=0))
    assert mode == "fallback" and reason and KEY not in reason
    assert len(fake.calls) <= 4


@pytest.mark.parametrize("exc", [
    RuntimeError(f"Invalid API Key {KEY}"),
    groq.APIConnectionError(request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")),
    groq.APITimeoutError(request=httpx.Request("POST", "https://api.groq.com/openai/v1/chat/completions")),
    groq.AuthenticationError(
        f"Invalid API Key {KEY}",
        response=httpx.Response(401, request=httpx.Request("POST", "https://api.groq.com/x")), body=None,
    ),
])
def test_other_errors_become_agent_error_without_key(exc):
    client, fake, sleeps = make(exc)
    with pytest.raises(AgentError) as ei:
        client.complete_json("s", "u", Answer)
    assert KEY not in str(ei.value) and KEY not in repr(ei.value)
    value, mode, reason = run_agent(client, "s", "u", Answer, lambda: Answer(answer="fb", count=0))
    assert mode == "fallback" and reason and KEY not in reason


def test_agent_error_is_runtime_error():
    assert issubclass(AgentError, RuntimeError)


# ---- tools and ledger on the real spill scenario ---------------------------------------------
@pytest.fixture(scope="module")
def spill():
    try:
        get_settings()
    except ConfigError:
        pytest.skip("no .env")
    return run_spill_scenario()


@pytest.fixture()
def tools(spill):
    return Tools(spill, ledger=ToolLedger(calls=[]))


def test_find_candidate_facilities_matches_scenario(spill, tools):
    out = tools.find_candidate_facilities()
    assert out == [c.model_dump(mode="json") for c in spill.candidates]
    assert [c["facility_id"] for c in out][0] == "VA0052621"
    calls = tools.ledger.calls
    assert calls and calls[-1]["tool"] == "find_candidate_facilities"
    ids = tools.ledger.facility_ids()
    names = {n.upper() for n in tools.ledger.facility_names()}
    for c in spill.candidates:
        assert c.facility_id in ids and c.name.upper() in names


def test_get_facility_violations_shape_and_ledger(spill, tools):
    c = spill.candidates[0]
    out = tools.get_facility_violations(c.facility_id)
    for k in ("facility_id", "name", "window_label", "window_start", "window_end", "numeric_violation_count",
              "historical_numeric_violation_count", "reporting_violation_count", "violations"):
        assert k in out
    assert out["facility_id"] == c.facility_id and out["name"] == c.name
    assert out["numeric_violation_count"] == c.numeric_violation_count
    assert str(c.numeric_violation_count) in tools.ledger.numbers()
    json.dumps(out)  # JSON-serializable


def test_get_tri_releases_shape(spill, tools):
    c = spill.candidates[0]
    out = tools.get_tri_releases(c.facility_id)
    for k in ("facility_id", "name", "tri_match", "tri_match_confidence", "releases"):
        assert k in out
    json.dumps(out)


def test_travel_time_matches_candidate(spill, tools):
    for c in spill.candidates:
        out = tools.travel_time(c.facility_id)
        assert out["display"] == c.travel_time.display


def test_trace_upstream_starts_at_reach(spill, tools):
    out = tools.trace_upstream()
    assert out and out[0]["nhdplusid"] == spill.reach_nhdplusid
    json.dumps(out)


@pytest.mark.parametrize("method", ["get_facility_violations", "get_tri_releases", "travel_time"])
def test_unknown_facility_id_raises(tools, method):
    with pytest.raises(ValueError):
        getattr(tools, method)("VA9999999")


def test_clean_text_drops_unknown_facilities_and_numbers(spill, tools):
    tools.find_candidate_facilities()
    tools.get_facility_violations("VA0052621")
    text = ("VALLEY MILK PRODUCTS LLC (VA0052621) is listed first by river distance. "
            "ACME CHEMICAL CORP is also nearby. "
            "Conductivity reached 4242.42 units at the gauge. "
            "Permit VA9999999 also appears upstream. "
            "VALLEY MILK PRODUCTS LLC caused the spike.")
    clean, notes = clean_text(text, tools.ledger)
    assert "VA0052621" in clean and "listed first by river distance" in clean
    assert "ACME" not in clean.upper()
    assert "4242.42" not in clean
    assert "VA9999999" not in clean
    assert not ACCUSATORY.search(clean)
    assert len(notes) >= 3
    assert KEY not in clean and all(KEY not in n for n in notes)


def test_clean_text_extra_numbers_allowed(tools):
    tools.find_candidate_facilities()
    clean, _ = clean_text("Fusion confidence was 0.123456.", tools.ledger, extra_numbers={"0.123456"})
    assert "0.123456" in clean
    clean2, notes = clean_text("Fusion confidence was 0.123456.", tools.ledger)
    assert "0.123456" not in clean2 and notes


def test_guardrails_module_exposes_contract_names():
    for name in ("ACCUSATORY", "strip_accusatory", "number_tokens", "unknown_numbers", "unknown_facilities",
                 "clean_text", "enforce_flags"):
        assert hasattr(guardrails, name), name


# ---- pipeline outputs are citable; invented numbers are not ----------------------------------
def test_pipeline_values_survive_the_number_guardrail(spill):
    tools = Tools(spill)  # records the pipeline outputs on construction
    cond = next(e for e in spill.sensor_events if e.parameter == "conductivity")
    valid = [
        f"Conductivity rose from a baseline of {cond.baseline_mean:.0f} uS/cm to {cond.value:.2f} uS/cm.",
        f"The conductivity rise peaked at {cond.value:.0f} uS/cm, z {cond.score:.1f}.",
        "The simulated spill began at 2026-10-09 20:00 UTC.",
        "The rain threshold is 0.5 in over 24 hours.",
        "Camera and gauge observations are compared with a ±3.0 h allowance.",
        f"The camera saw dye in {len(spill.vision.plume_detections)} of {spill.vision.frames_analyzed} frames.",
    ]
    for s in valid:
        out, notes = clean_text(s, tools.ledger)
        assert out.strip() == s, notes
    for s in ("Conductivity peaked at 912 uS/cm.", "The plume travelled 3.7 km.", "Travel time is 12.6 h."):
        out, _ = clean_text(s, tools.ledger)
        assert out.strip() == "", s
