"""In-memory stand-in for backboard.BackboardClient (the async methods SilentSpill uses)."""
from __future__ import annotations

from types import SimpleNamespace


class FakeAPIError(Exception):
    def __init__(self, status_code: int):
        super().__init__(f"fake error {status_code}")
        self.status_code = status_code


class FakeStore:
    """Shared state across fake clients: assistants, memories, calls, and injected failures."""

    def __init__(self):
        self.assistants: list[SimpleNamespace] = []
        self.memories: list[dict] = []
        self.calls: list[tuple[str, dict]] = []
        self.fail: dict[str, int] = {}  # method name -> status code to raise
        self.keys_seen: list[str] = []

    def factory(self, api_key: str) -> "FakeBackboard":
        self.keys_seen.append(api_key)
        return FakeBackboard(self)

    def names(self, method: str) -> list[dict]:
        return [kw for m, kw in self.calls if m == method]


class FakeBackboard:
    def __init__(self, store: FakeStore):
        self.s = store

    def _record(self, method: str, **kw) -> None:
        self.s.calls.append((method, kw))
        if method in self.s.fail:
            raise FakeAPIError(self.s.fail[method])

    def _assistant(self, assistant_id: str) -> None:
        if not any(a.assistant_id == assistant_id for a in self.s.assistants):
            raise FakeAPIError(404)

    async def list_assistants(self, skip: int = 0, limit: int = 100, name: str | None = None):
        self._record("list_assistants", name=name)
        return [a for a in self.s.assistants if name is None or a.name == name]

    async def create_assistant(self, name: str, description: str | None = None, **kw):
        self._record("create_assistant", name=name, description=description)
        a = SimpleNamespace(assistant_id=f"asst-{len(self.s.assistants) + 1}", name=name)
        self.s.assistants.append(a)
        return a

    async def add_memory(self, assistant_id, content: str, metadata: dict | None = None):
        self._record("add_memory", assistant_id=assistant_id, content=content, metadata=metadata)
        self._assistant(assistant_id)
        if any(not isinstance(v, (str, bool)) for v in (metadata or {}).values()):
            raise FakeAPIError(500)  # the live API rejects numeric metadata values the same way
        mid = f"mem-{len(self.s.memories) + 1}"
        self.s.memories.append({"id": mid, "assistant_id": assistant_id, "content": content,
                                "metadata": metadata or {}})
        return {"memory_id": mid, "status": "ok"}

    async def search_memories(self, assistant_id, query: str, limit: int = 5):
        self._record("search_memories", assistant_id=assistant_id, query=query, limit=limit)
        self._assistant(assistant_id)
        mine = [m for m in self.s.memories if m["assistant_id"] == assistant_id]
        # Like the live API: search hits have id, content and score but no metadata.
        hits = [{"id": m["id"], "content": m["content"], "score": 0.9} for m in reversed(mine)][:limit]
        return {"memories": hits, "total_count": len(hits)}

    async def get_memory(self, assistant_id, memory_id: str):
        self._record("get_memory", assistant_id=assistant_id, memory_id=memory_id)
        self._assistant(assistant_id)
        m = next((m for m in self.s.memories if m["id"] == memory_id), None)
        if m is None:
            raise FakeAPIError(404)
        return SimpleNamespace(id=m["id"], content=m["content"], metadata=m["metadata"])

    async def aclose(self):
        self.s.calls.append(("aclose", {}))
