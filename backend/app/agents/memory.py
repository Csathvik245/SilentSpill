"""Incident memory on Backboard: save each completed run and each human review, recall past events.

Optional. Without BACKBOARD_API_KEY every call is skipped; any failing call is logged with its
error type and status code (never the key) and the run carries on with memory_mode "failed".
The Backboard SDK is async; this module exposes plain functions that run it on a private event loop.

Recalled memories are untrusted text. sanitize_memories passes them through the same guardrails as
model output, against the current run's tool ledger, so a memory cannot add a facility or a number.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from ..schemas import MemoryMode, RelatedPastEvent, ReviewDecision
from .guardrails import clean_text
from .tools import ToolLedger

log = logging.getLogger("silentspill.memory")

ASSISTANT_NAME = "SilentSpill incident memory"
ASSISTANT_CACHE_FILE = "backboard_assistant.json"
RECALL_LIMIT = 3
CLIENT_TIMEOUT_S = 15
_INCIDENT_ID = re.compile(r"^(spill|storm)-\d{8}T\d{4}Z$")
_CLASSES = {"incident", "storm_runoff", "flag_for_review", "no_incident"}
_DECISIONS = {"confirmed", "storm_runoff", "false_alarm"}
_PARENS = re.compile(r"\s*\([^)]*\)")


def default_client_factory(api_key: str) -> Any:
    from backboard import BackboardClient

    return BackboardClient(api_key=api_key, timeout=CLIENT_TIMEOUT_S)


class MemoryResult:
    """Outcome of one memory operation: mode, reason (never containing the key) and value."""

    def __init__(self, mode: MemoryMode, reason: str | None = None, value: Any = None):
        self.mode, self.reason, self.value = mode, reason, value


def combine(*results: MemoryResult) -> tuple[MemoryMode, str | None]:
    """failed if any call failed, skipped if all were skipped, otherwise backboard."""
    for r in results:
        if r.mode == "failed":
            return "failed", r.reason
    if results and all(r.mode == "skipped" for r in results):
        return "skipped", results[0].reason
    return "backboard", None


def _error_reason(e: BaseException) -> str:
    # Type and status only: SDK messages are not echoed, so nothing from a request can leak.
    status = getattr(e, "status_code", None)
    return f"{type(e).__name__}" + (f" (status {status})" if status else "")


def _run(coro_fn: Callable[[], Awaitable[Any]]) -> Any:
    """Run an async SDK call from sync code, also when the calling thread has a running loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro_fn())
    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(asyncio.run, coro_fn()).result()


def _get(obj: Any, key: str) -> Any:
    return obj.get(key) if isinstance(obj, dict) else getattr(obj, key, None)


class IncidentMemory:
    """Backboard-backed incident memory. Every public method returns a MemoryResult and never raises."""

    def __init__(
        self,
        api_key: str | None,
        cache_dir: str | Path,
        client_factory: Callable[[str], Any] | None = None,
        assistant_name: str = ASSISTANT_NAME,
    ):
        self._key = api_key or None
        self._cache_file = Path(cache_dir) / ASSISTANT_CACHE_FILE
        self._factory = client_factory  # None: default_client_factory, looked up at call time
        self.assistant_name = assistant_name

    @classmethod
    def from_settings(cls, settings: Any, client_factory: Callable[[str], Any] | None = None) -> "IncidentMemory":
        key = getattr(settings, "backboard_api_key", None)
        return cls(key.get_secret_value() if key else None, settings.cache_dir, client_factory)

    @property
    def configured(self) -> bool:
        return self._key is not None

    # ---- plumbing ---------------------------------------------------------------------------
    def _call(self, what: str, op: Callable[[Any, str], Awaitable[Any]]) -> MemoryResult:
        """op(client, assistant_id) on a fresh client; any failure becomes mode 'failed'."""
        if not self.configured:
            return MemoryResult("skipped", "BACKBOARD_API_KEY not set")

        async def go() -> Any:
            client = (self._factory or default_client_factory)(self._key)  # type: ignore[arg-type]
            try:
                assistant_id = await self._assistant_id(client)
                try:
                    return await op(client, assistant_id)
                except Exception as e:  # noqa: BLE001
                    # A cached ID for a deleted assistant (or another account): forget it, retry once.
                    if getattr(e, "status_code", None) != 404 and type(e).__name__ != "BackboardNotFoundError":
                        raise
                    self._cache_file.unlink(missing_ok=True)
                    return await op(client, await self._assistant_id(client))
            finally:
                close = getattr(client, "aclose", None)
                if close is not None:
                    try:
                        await close()
                    except Exception:  # noqa: BLE001 - closing must not turn success into failure
                        pass

        try:
            return MemoryResult("backboard", None, _run(go))
        except Exception as e:  # noqa: BLE001 - memory is optional and must never fail the run
            reason = _error_reason(e)
            log.warning("Backboard %s skipped: %s", what, reason)
            return MemoryResult("failed", reason)

    async def _assistant_id(self, client: Any) -> str:
        """The cached assistant ID; otherwise the existing assistant with our name, or a new one."""
        try:
            cached = json.loads(self._cache_file.read_text(encoding="utf-8"))
            if cached.get("name") == self.assistant_name and cached.get("assistant_id"):
                return str(cached["assistant_id"])
        except (OSError, ValueError):
            pass
        existing = await client.list_assistants(name=self.assistant_name)
        match = next((a for a in existing or [] if _get(a, "name") == self.assistant_name), None)
        if match is None:
            match = await client.create_assistant(
                name=self.assistant_name,
                description="Memory of SilentSpill demo runs and human review decisions (simulated data).",
            )
        assistant_id = str(_get(match, "assistant_id"))
        self._cache_file.parent.mkdir(parents=True, exist_ok=True)
        self._cache_file.write_text(
            json.dumps({"name": self.assistant_name, "assistant_id": assistant_id}) + "\n", encoding="utf-8"
        )
        return assistant_id

    # ---- operations -------------------------------------------------------------------------
    def recall(self, reach_id: int, query: str, limit: int = RECALL_LIMIT) -> MemoryResult:
        """Up to `limit` memories ranked by relevance to query, kept only if on the same reach."""

        async def op(client: Any, aid: str) -> list[dict[str, Any]]:
            found = await client.search_memories(aid, query=query, limit=limit)
            items = _get(found, "memories") or []
            out = []
            for m in items:
                meta = _get(m, "metadata")
                if meta is None and _get(m, "id"):
                    # Search hits carry no metadata (checked live): read each hit in full.
                    meta = _get(await client.get_memory(aid, _get(m, "id")), "metadata")
                meta = meta or {}
                if str(meta.get("reach_id")) == str(reach_id):
                    out.append({"id": _get(m, "id"), "content": _get(m, "content") or "", "metadata": meta})
            return out[:limit]

        return self._call("recall", op)

    def save(self, content: str, metadata: dict[str, Any]) -> MemoryResult:
        """Add one memory; value is the new memory ID when the API returns one."""

        async def op(client: Any, aid: str) -> str | None:
            res = await client.add_memory(aid, content=content, metadata=metadata)
            mid = _get(res, "memory_id") or _get(res, "id")
            return None if mid is None else str(mid)

        return self._call("save", op)


# ---- content builders -----------------------------------------------------------------------
def recall_query(reach_id: int, classification: str) -> str:
    # Short and close to the saved wording: longer queries scored lower in Backboard's search.
    return f"Past SilentSpill runs and human reviews on reach {reach_id}: {classification}"


def run_memory(
    incident_id: str, reach_id: int, scenario_date: date, classification: str, confidence: float,
    evidence: str, saved_at: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    """(content, metadata) for a completed run. simulated is always true."""
    saved_at = saved_at or datetime.now(timezone.utc)
    content = (
        f"SilentSpill run {incident_id} on reach {reach_id}, scenario date {scenario_date.isoformat()}: "
        f"classified {classification} with confidence {confidence}. Fused evidence: {evidence} "
        "Simulated: true."
    )
    # Metadata values are strings or booleans: Backboard answers 500 to numeric metadata values.
    metadata = {
        "type": "run", "incident_id": incident_id, "reach_id": str(reach_id),
        "date": scenario_date.isoformat(), "classification": classification, "confidence": str(confidence),
        "evidence_summary": evidence, "simulated": True,
        "saved_at": saved_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    return content, metadata


def review_memory(
    incident_id: str, reach_id: int, scenario_date: date, decision: ReviewDecision, note: str,
    saved_at: datetime | None = None,
) -> tuple[str, dict[str, Any]]:
    """(content, metadata) for a human review decision, linked to its incident by incident_id."""
    saved_at = saved_at or datetime.now(timezone.utc)
    content = (
        f"Human review of SilentSpill run {incident_id} on reach {reach_id}, scenario date "
        f"{scenario_date.isoformat()}: decision {decision}."
        + (f" Reviewer note: {note.strip()}" if note and note.strip() else "")
        + " Simulated: true."
    )
    metadata = {
        "type": "review", "incident_id": incident_id, "reach_id": str(reach_id),
        "date": scenario_date.isoformat(), "decision": decision, "simulated": True,
        "saved_at": saved_at.isoformat(timespec="seconds").replace("+00:00", "Z"),
    }
    return content, metadata


def evidence_summary(fusion: Any) -> str:
    """One-line fused evidence summary: sources plus the fusion reasons."""
    sources = ", ".join(fusion.sources) or "none"
    return f"sources {sources}. " + " ".join(fusion.reasons)


# ---- recall sanitizing ----------------------------------------------------------------------
def _parse_date(v: Any) -> date | None:
    try:
        return date.fromisoformat(str(v)[:10])
    except (TypeError, ValueError):
        return None


def _parse_dt(v: Any) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def sanitize_memories(memories: list[dict[str, Any]], ledger: ToolLedger) -> tuple[list[RelatedPastEvent], list[str]]:
    """Recalled memories -> RelatedPastEvent, through the guardrails. Returns (events, notes).

    The text keeps only sentences whose facilities and numbers appear in this run's tool output
    (no extra numbers are allowed), accusatory wording is rewritten, and structured fields are
    kept only when they match the known formats. A memory with no text left is dropped.
    """
    events: list[RelatedPastEvent] = []
    notes: list[str] = []
    for i, m in enumerate(memories, start=1):
        meta = m.get("metadata") or {}
        text, n = clean_text(str(m.get("content") or ""), ledger)
        # Notes drop any quoted values, so a rejected number never reaches the response either.
        notes.extend(f"memory {i}: {_PARENS.sub('', x)}" for x in n)
        if not text.strip():
            notes.append(f"memory {i}: dropped; nothing left after the guardrails")
            continue
        iid = str(meta.get("incident_id") or "")
        cls = meta.get("classification")
        decision = meta.get("decision")
        events.append(RelatedPastEvent(
            memory_type="review" if meta.get("type") == "review" else "run",
            incident_id=iid if _INCIDENT_ID.match(iid) else None,
            scenario_date=_parse_date(meta.get("date")),
            saved_at=_parse_dt(meta.get("saved_at")),
            classification=cls if cls in _CLASSES else None,
            review_decision=decision if decision in _DECISIONS else None,
            text=text.strip(),
        ))
    return events, notes


def describe(events: list[RelatedPastEvent]) -> str:
    """Plain text for the report's Related past events section; "none" when empty."""
    if not events:
        return "none"
    lines = []
    for e in events:
        when = f" (saved {e.saved_at:%Y-%m-%d %H:%M} UTC)" if e.saved_at else ""
        if e.memory_type == "review":
            head = f"Human review of {e.incident_id or 'a past run'}: {e.review_decision or 'decision not recorded'}"
        else:
            head = f"Past run {e.incident_id or ''}".rstrip() + f": {e.classification or 'classification not recorded'}"
        lines.append(head + when + ".")
    return " ".join(lines)
