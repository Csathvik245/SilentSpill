"""Groq JSON client and the run-or-fall-back helper every agent uses.

The client only ever returns a validated pydantic model or raises AgentError; error messages never
contain the API key.
"""
from __future__ import annotations

import email.utils
import json
import time
from collections.abc import Callable
from typing import Any, TypeVar

from pydantic import BaseModel, ValidationError

from ..schemas import AgentMode

T = TypeVar("T")
S = TypeVar("S", bound=BaseModel)

NOT_CONFIGURED = "GROQ_API_KEY or GROQ_MODEL not set"
MAX_TEMPERATURE = 0.2
MAX_BACKOFF_S = 60.0
_ERROR_TEXT_LIMIT = 1500


class AgentError(RuntimeError):
    """An LLM call failed. The message is safe to show: it never contains the key."""


def _content(response: Any) -> str | None:
    """choices[0].message.content from an SDK response or a plain dict."""
    try:
        choice = response["choices"][0] if isinstance(response, dict) else response.choices[0]
        msg = choice["message"] if isinstance(choice, dict) else choice.message
        return msg["content"] if isinstance(msg, dict) else msg.content
    except (AttributeError, IndexError, KeyError, TypeError):
        return None


def _status_code(err: BaseException) -> int | None:
    code = getattr(err, "status_code", None)
    if code is None:
        code = getattr(getattr(err, "response", None), "status_code", None)
    return code if isinstance(code, int) else None


def _retry_after(err: BaseException) -> float | None:
    """Seconds from retry-after-ms / retry-after headers on the error's response, if any."""
    headers = getattr(getattr(err, "response", None), "headers", None)
    if not headers:
        return None
    try:
        ms = headers.get("retry-after-ms")
        if ms is not None:
            return float(ms) / 1000.0
    except (TypeError, ValueError, AttributeError):
        pass
    try:
        value = headers.get("retry-after")
    except AttributeError:
        return None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        parsed = email.utils.parsedate_tz(str(value))
        return None if parsed is None else float(email.utils.mktime_tz(parsed) - time.time())


class GroqJSONClient:
    """JSON-mode chat completions validated against a pydantic schema.

    Needs GROQ_API_KEY and a model (argument or GROQ_MODEL); otherwise available is False and
    complete_json is never called by run_agent. Tests inject a fake client with
    .chat.completions.create(**kw).
    """

    def __init__(
        self,
        settings: Any = None,
        client: Any = None,
        model: str | None = None,
        temperature: float = 0.1,
        timeout: float = 30.0,
        max_rate_limit_retries: int = 3,
        sleep: Callable[[float], Any] = time.sleep,
    ) -> None:
        self.temperature = min(max(float(temperature), 0.0), MAX_TEMPERATURE)
        self.timeout = timeout
        self.max_rate_limit_retries = max(0, int(max_rate_limit_retries))
        self._sleep = sleep
        self.unavailable_reason: str | None = None
        self._client = None

        if settings is None:
            from ..config import ConfigError, get_settings

            try:
                settings = get_settings()
            except ConfigError:
                settings = None
        key = getattr(settings, "groq_api_key", None)
        self.model = model or getattr(settings, "groq_model", None) or None
        has_key = bool(key.get_secret_value() if hasattr(key, "get_secret_value") else key)

        if not (has_key and self.model):
            self.unavailable_reason = NOT_CONFIGURED
        elif client is not None:
            self._client = client
        else:
            try:
                import groq

                secret = key.get_secret_value() if hasattr(key, "get_secret_value") else str(key)
                self._client = groq.Groq(api_key=secret, timeout=timeout, max_retries=0)
            except Exception as exc:  # never surface the key: report the type only
                self.unavailable_reason = f"Groq client could not be created ({type(exc).__name__})"
        self._secret = (key.get_secret_value() if hasattr(key, "get_secret_value") else key) if has_key else None

    @property
    def available(self) -> bool:
        return self._client is not None and self.unavailable_reason is None

    def _scrub(self, text: str) -> str:
        if self._secret and isinstance(self._secret, str):
            text = text.replace(self._secret, "***")
        return text

    def _create(self, messages: list[dict[str, str]]) -> Any:
        """One completion, retrying only on rate limits (429)."""
        attempt = 0
        while True:
            try:
                return self._client.chat.completions.create(
                    model=self.model,
                    messages=messages,
                    temperature=self.temperature,
                    response_format={"type": "json_object"},
                )
            except Exception as exc:
                if self._is_rate_limit(exc):
                    if attempt >= self.max_rate_limit_retries:
                        raise AgentError(
                            f"Groq rate limit (429) persisted after {self.max_rate_limit_retries} retries"
                        ) from None
                    wait = _retry_after(exc)
                    if wait is None or wait < 0:
                        wait = float(2**attempt)
                    self._sleep(min(wait, MAX_BACKOFF_S))
                    attempt += 1
                    continue
                raise AgentError(self._describe(exc)) from None

    @staticmethod
    def _is_rate_limit(exc: BaseException) -> bool:
        try:
            import groq

            if isinstance(exc, groq.RateLimitError):
                return True
        except ImportError:
            pass
        return _status_code(exc) == 429

    def _describe(self, exc: BaseException) -> str:
        """Short, key-free description of a failed request."""
        try:
            import groq

            if isinstance(exc, groq.APITimeoutError):
                return f"Groq request timed out after {self.timeout:g} s (APITimeoutError)"
            if isinstance(exc, groq.APIConnectionError):
                return "Groq connection error (APIConnectionError)"
        except ImportError:
            pass
        code = _status_code(exc)
        status = f", status {code}" if code is not None else ""
        return f"Groq request failed ({type(exc).__name__}{status})"

    def complete_json(self, system: str, user: str, schema: type[S]) -> S:
        """Ask for a JSON object matching schema; retry once on invalid JSON or schema errors."""
        if not self.available:
            raise AgentError(self.unavailable_reason or NOT_CONFIGURED)
        schema_json = json.dumps(schema.model_json_schema(), separators=(",", ":"))
        messages = [
            {
                "role": "system",
                "content": f"{system}\n\nRespond with one JSON object only, matching this JSON schema:\n{schema_json}",
            },
            {"role": "user", "content": user},
        ]
        last_error = ""
        for attempt in range(2):
            raw = _content(self._create(messages))
            try:
                if not isinstance(raw, str):
                    raise ValueError("response had no message content")
                return schema.model_validate(json.loads(raw))
            except (json.JSONDecodeError, ValidationError, ValueError, TypeError) as exc:
                # ValidationError subclasses ValueError, so test it first.
                kind = "schema validation failed" if isinstance(exc, ValidationError) else "invalid JSON"
                last_error = self._scrub(f"{kind}: {exc}")[:_ERROR_TEXT_LIMIT]
                if attempt == 0:
                    messages = messages + [
                        {"role": "assistant", "content": raw if isinstance(raw, str) else ""},
                        {
                            "role": "user",
                            "content": (
                                "Your previous reply was rejected: "
                                f"{last_error}\nReply again with one valid JSON object that matches the schema "
                                "exactly. No prose, no code fences."
                            ),
                        },
                    ]
        raise AgentError(f"model output rejected twice ({last_error.splitlines()[0][:300] if last_error else 'unknown'})")


def run_agent(
    client: GroqJSONClient | None,
    system: str,
    user: str,
    schema: type[S],
    fallback: Callable[[], T],
) -> tuple[S | T, AgentMode, str | None]:
    """(result, "llm", None) on success, else (fallback(), "fallback", reason)."""
    if client is None or not getattr(client, "available", False):
        reason = getattr(client, "unavailable_reason", None) or NOT_CONFIGURED
        return fallback(), "fallback", reason
    try:
        return client.complete_json(system, user, schema), "llm", None
    except AgentError as err:
        return fallback(), "fallback", str(err)
    except Exception as err:  # complete_json should not raise anything else; stay safe for the demo
        return fallback(), "fallback", f"{type(err).__name__}"
