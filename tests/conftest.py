"""Shared test setup: no test may reach the live Backboard API."""
import pytest


class LiveBackboardBlocked(RuntimeError):
    pass


@pytest.fixture(scope="session", autouse=True)
def _no_live_backboard():
    """The real SDK client can never be built in tests. Session-scoped so it is in place before any
    module fixture runs; a test that forgets to inject a fake gets memory_mode "failed" with no
    network call instead of writing to the real assistant."""
    from backend.app.agents import memory

    def blocked(api_key: str):
        raise LiveBackboardBlocked("live Backboard calls are not allowed in tests")

    original = memory.default_client_factory
    memory.default_client_factory = blocked
    yield
    memory.default_client_factory = original
