"""Incident memory (Backboard), with a fake client: no live calls."""
from __future__ import annotations

import json
import logging

import pytest
from backboard_fake import FakeStore
from test_agents_chain import KEY as GROQ_KEY
from test_agents_chain import ScriptedGroq, Sleeps, fake_settings, good_responses

from backend.app.agents import memory
from backend.app.agents.base import GroqJSONClient
from backend.app.agents.memory import ASSISTANT_CACHE_FILE, ASSISTANT_NAME, IncidentMemory
from backend.app.agents.orchestrator import run_chain
from backend.app.agents.tools import ToolLedger, Tools
from backend.app.config import ConfigError, get_settings
from backend.app.schemas import IncidentResponse
from backend.app.simulation.scenario import run_spill_scenario, run_storm_scenario

BB_KEY = "bb_TESTKEY_should_never_leak"


class NoLLM:
    available = False
    unavailable_reason = "GROQ_API_KEY or GROQ_MODEL not set"


@pytest.fixture(scope="module")
def settings():
    try:
        return get_settings()
    except ConfigError:
        pytest.skip("no .env")


@pytest.fixture(scope="module")
def spill(settings):
    return run_spill_scenario(settings)


@pytest.fixture(scope="module")
def storm(settings):
    return run_storm_scenario(settings)


@pytest.fixture
def store():
    return FakeStore()


@pytest.fixture
def mem(store, tmp_path):
    return IncidentMemory(BB_KEY, tmp_path, store.factory)


def llm_client(scenario, scripts=None):
    fake = ScriptedGroq(good_responses(scenario), scripts)
    return GroqJSONClient(settings=fake_settings(), client=fake, sleep=Sleeps()), fake


def seed(store: FakeStore, mem: IncidentMemory, content: str, **meta) -> None:
    """Put one memory in the fake store through the module's own save path."""
    base = {"type": "run", "incident_id": "spill-20261009T2000Z", "reach_id": "10000500083697",
            "date": "2026-10-09", "classification": "incident", "simulated": True,
            "saved_at": "2026-10-17T12:00:00Z"}
    merged = {k: v for k, v in {**base, **meta}.items() if v is not None}
    assert mem.save(content, merged).mode == "backboard"


# ---- missing key -------------------------------------------------------------------------------
def test_missing_key_skips_without_any_call(spill, store, tmp_path):
    m = IncidentMemory(None, tmp_path, store.factory)
    assert not m.configured
    for r in (m.recall(1, "q"), m.save("c", {})):
        assert r.mode == "skipped" and "BACKBOARD_API_KEY" in r.reason
    resp = run_chain(spill, client=NoLLM(), memory_store=m)
    assert resp.memory_mode == "skipped"
    assert resp.report.related_past_events == [] and resp.report.related_past_events_text == "none"
    assert store.calls == [] and store.keys_seen == []
    assert not (tmp_path / ASSISTANT_CACHE_FILE).exists()


def test_no_memory_store_means_skipped(spill):
    resp = run_chain(spill, client=NoLLM())
    assert resp.memory_mode == "skipped" and resp.report.related_past_events_text == "none"


def test_from_settings_reads_key_name_only(settings):
    m = IncidentMemory.from_settings(settings.model_copy(update={"backboard_api_key": None}))
    assert not m.configured


# ---- assistant ---------------------------------------------------------------------------------
def test_assistant_created_once_and_cached(store, mem, tmp_path):
    assert mem.save("a", {}).mode == "backboard"
    assert mem.save("b", {}).mode == "backboard"
    assert len(store.names("create_assistant")) == 1
    assert store.names("create_assistant")[0]["name"] == ASSISTANT_NAME == "SilentSpill incident memory"
    cached = json.loads((tmp_path / ASSISTANT_CACHE_FILE).read_text(encoding="utf-8"))
    assert cached == {"name": ASSISTANT_NAME, "assistant_id": "asst-1"}
    # A new process (new IncidentMemory) reuses the cache: no list, no create.
    store.calls.clear()
    IncidentMemory(BB_KEY, tmp_path, store.factory).save("c", {})
    assert not store.names("list_assistants") and not store.names("create_assistant")
    assert BB_KEY not in (tmp_path / ASSISTANT_CACHE_FILE).read_text(encoding="utf-8")


def test_existing_assistant_by_name_is_reused(store, tmp_path):
    IncidentMemory(BB_KEY, tmp_path / "a", store.factory).save("x", {})
    IncidentMemory(BB_KEY, tmp_path / "b", store.factory).save("y", {})  # empty cache dir
    assert len(store.assistants) == 1 and len(store.names("create_assistant")) == 1


def test_stale_cached_assistant_is_replaced_once(store, mem, tmp_path):
    (tmp_path / ASSISTANT_CACHE_FILE).write_text(
        json.dumps({"name": ASSISTANT_NAME, "assistant_id": "deleted-id"}), encoding="utf-8")
    r = mem.save("x", {})
    assert r.mode == "backboard" and len(store.memories) == 1
    assert json.loads((tmp_path / ASSISTANT_CACHE_FILE).read_text())["assistant_id"] == "asst-1"


# ---- save --------------------------------------------------------------------------------------
def test_completed_run_is_saved(spill, store, mem):
    resp = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert resp.memory_mode == "backboard" and resp.memory_reason is None
    saved = store.names("add_memory")
    assert len(saved) == 1
    meta, content = saved[0]["metadata"], saved[0]["content"]
    assert meta["type"] == "run" and meta["incident_id"] == resp.incident_id
    assert meta["reach_id"] == str(spill.reach_nhdplusid)
    assert meta["date"] == spill.scenario_timestamp.date().isoformat()
    assert meta["classification"] == spill.fusion.classification == "incident"
    assert meta["confidence"] == str(spill.fusion.confidence)
    assert all(isinstance(v, (str, bool)) for v in meta.values())  # Backboard rejects numbers
    assert meta["evidence_summary"] and spill.fusion.reasons[0] in meta["evidence_summary"]
    assert meta["simulated"] is True
    for part in (str(spill.reach_nhdplusid), meta["date"], "incident", str(spill.fusion.confidence),
                 "Simulated: true"):
        assert part in content
    assert store.calls[-1][0] == "aclose"


def test_storm_run_is_saved_with_its_classification(storm, store, mem):
    resp = run_chain(storm, client=NoLLM(), memory_store=mem)
    meta = store.names("add_memory")[0]["metadata"]
    assert resp.memory_mode == "backboard" and meta["classification"] == "storm_runoff"
    assert meta["date"] == storm.scenario_timestamp.date().isoformat() and meta["simulated"] is True


# ---- recall ------------------------------------------------------------------------------------
def test_second_run_recalls_the_first(spill, store, mem):
    first = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert first.report.related_past_events_text == "none"
    second = run_chain(spill, client=NoLLM(), memory_store=mem)
    search = store.names("search_memories")[-1]
    assert search["limit"] == 3 and str(spill.reach_nhdplusid) in search["query"]
    events = second.report.related_past_events
    assert len(events) == 1 and events[0].memory_type == "run"
    assert events[0].incident_id == first.incident_id and events[0].classification == "incident"
    assert first.incident_id in second.report.related_past_events_text
    assert any("Incident memory has related past events" in r for r in second.triage.reasons)
    # Memory never changes the fusion classification or confidence.
    assert second.triage.classification == first.triage.classification
    assert second.triage.confidence == second.report.confidence == spill.fusion.confidence


def test_recall_keeps_only_the_same_reach(spill, store, mem):
    seed(store, mem, "SilentSpill run elsewhere: incident.", reach_id="999")
    resp = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert resp.report.related_past_events == []


def test_recall_limit_is_three(spill, store, mem):
    for i in range(5):
        seed(store, mem, f"Past run {i % 2} on this reach: classified incident.")
    resp = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert store.names("search_memories")[0]["limit"] == 3
    assert len(resp.report.related_past_events) == 3


def test_prior_context_reaches_triage_and_cannot_upgrade(storm, store, mem):
    seed(store, mem, "Human review of SilentSpill run spill-20261009T2000Z: decision confirmed.",
         type="review", decision="confirmed", classification=None, date="2026-09-22")
    client, fake = llm_client(storm, scripts={"triage": [
        {"classification": "incident", "reasons": ["A past review confirmed an incident on this reach."]}]})
    resp = run_chain(storm, client=client, memory_store=mem)
    prompt = json.dumps(fake.calls[0]["messages"])
    assert "prior_context" in prompt and "decision confirmed" in prompt
    assert resp.triage.classification == "storm_runoff" and resp.triage.fusion_classification == "storm_runoff"
    assert resp.triage.confidence == storm.fusion.confidence
    assert any("stronger than" in n for n in resp.report.guardrail_notes)
    assert resp.report.related_past_events[0].review_decision == "confirmed"


def test_triage_cannot_cite_a_number_from_memory(spill, store, mem):
    seed(store, mem, "Past run on this reach: classified incident.")
    client, _ = llm_client(spill, scripts={"triage": [
        {"classification": "incident",
         "reasons": ["Gauge and camera agree on this event.", "A past run on this reach reported 4242 frames."]}]})
    resp = run_chain(spill, client=client, memory_store=mem)
    assert all("4242" not in r for r in resp.triage.reasons)
    assert "Gauge and camera agree on this event." in resp.triage.reasons


# ---- guardrails on recalled memories -----------------------------------------------------------
def test_memory_with_unknown_facility_is_rejected(spill, store, mem):
    seed(store, mem,
         "Past run on this reach: classified incident. ACME CHEMICAL CORP (VA9999999) released dye "
         "upstream. Readings showed conductivity of 7777 uS/cm.")
    client, fake = llm_client(spill)
    resp = run_chain(spill, client=client, memory_store=mem)
    ev = resp.report.related_past_events
    assert len(ev) == 1 and ev[0].text == "Past run on this reach: classified incident."
    dump = resp.model_dump_json()
    for bad in ("ACME", "VA9999999", "7777"):
        assert bad not in dump, bad  # not in the report, triage, or the guardrail notes
        assert bad not in json.dumps(fake.calls[0]["messages"]), bad  # never shown to the model
    assert any(n.startswith("memory 1:") for n in resp.report.guardrail_notes)
    assert "VA9999999" not in {c.facility_id for c in resp.report.ranked_candidates}


def test_memory_with_only_bad_content_is_dropped(spill, store, mem):
    seed(store, mem, "ACME CHEMICAL CORP (VA9999999) is the polluter.")
    resp = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert resp.report.related_past_events == [] and resp.report.related_past_events_text == "none"
    assert any("dropped" in n for n in resp.report.guardrail_notes)


def test_memory_known_facility_survives(spill, store, mem):
    c = spill.candidates[0]
    seed(store, mem, f"Past run on this reach listed {c.name} ({c.facility_id}) first by river distance.")
    resp = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert c.facility_id in resp.report.related_past_events[0].text


def test_sanitize_rejects_bad_structured_fields(spill):
    ledger = Tools(spill, ledger=ToolLedger()).ledger
    events, _ = memory.sanitize_memories([{
        "content": "Past run on this reach.",
        "metadata": {"incident_id": "rm -rf; DROP", "classification": "guilty", "decision": "blame",
                     "date": "not a date"},
    }], ledger)
    e = events[0]
    assert e.incident_id is None and e.classification is None and e.review_decision is None
    assert e.scenario_date is None


# ---- failures ----------------------------------------------------------------------------------
@pytest.mark.parametrize("method", ["search_memories", "add_memory", "list_assistants", "create_assistant"])
def test_failed_call_never_fails_the_run(spill, store, mem, method, caplog):
    store.fail[method] = 500
    with caplog.at_level(logging.WARNING, logger="silentspill.memory"):
        resp = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert isinstance(resp, IncidentResponse) and resp.report.classification == "incident"
    assert resp.memory_mode == "failed" and resp.memory_reason == "FakeAPIError (status 500)"
    assert resp.report.related_past_events_text == "none"
    assert "Backboard" in caplog.text and "FakeAPIError" in caplog.text
    assert BB_KEY not in caplog.text and BB_KEY not in resp.model_dump_json()


def test_client_construction_failure_is_failed(spill, tmp_path, caplog):
    def broken(api_key):
        raise ValueError(f"bad key {api_key}")  # a message that would leak the key if echoed

    with caplog.at_level(logging.WARNING, logger="silentspill.memory"):
        resp = run_chain(spill, client=NoLLM(), memory_store=IncidentMemory(BB_KEY, tmp_path, broken))
    assert resp.memory_mode == "failed" and resp.memory_reason == "ValueError"
    assert BB_KEY not in caplog.text and BB_KEY not in resp.model_dump_json()


def test_tests_cannot_reach_live_backboard(tmp_path):
    r = IncidentMemory(BB_KEY, tmp_path).save("x", {})  # default factory is blocked by conftest
    assert r.mode == "failed" and r.reason == "LiveBackboardBlocked"


def test_failed_recall_then_saved_still_reports_failed(spill, store, mem):
    store.fail["search_memories"] = 503
    resp = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert resp.memory_mode == "failed" and len(store.memories) == 1  # the save still happened


def test_groq_key_and_backboard_key_never_in_memory(spill, store, mem):
    client, _ = llm_client(spill)
    run_chain(spill, client=client, memory_store=mem)
    blob = json.dumps(store.memories)
    assert BB_KEY not in blob and GROQ_KEY not in blob


# ---- report section ----------------------------------------------------------------------------
def test_describe():
    assert memory.describe([]) == "none"


def test_recall_reads_metadata_when_search_omits_it(spill, store, mem):
    seed(store, mem, "Past run on this reach: classified incident.")
    resp = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert len(store.names("get_memory")) == 1  # one full read per search hit
    assert resp.report.related_past_events[0].incident_id == "spill-20261009T2000Z"


def test_failed_full_read_is_failed(spill, store, mem):
    seed(store, mem, "Past run on this reach: classified incident.")
    store.fail["get_memory"] = 502
    resp = run_chain(spill, client=NoLLM(), memory_store=mem)
    assert resp.memory_mode == "failed" and resp.report.related_past_events_text == "none"
