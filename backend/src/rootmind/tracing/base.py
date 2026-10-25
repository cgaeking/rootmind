"""The tracing hook: the interface the agent loop calls, with no-op defaults.

The engine (:mod:`rootmind.rlm_engine.engine`) never imports this package. It
receives an optional tracer object and, only when one is given, calls:

- :meth:`Tracer.question` once per question, which returns a
  :class:`QuestionTrace`;
- :meth:`QuestionTrace.model_call` before every LLM call and
  :meth:`QuestionTrace.tool_call` before every tool call; both return a
  :class:`Span` that the engine ends with what came back (tokens, stop
  reason, tool requests; result size, hits, windows read);
- :meth:`QuestionTrace.event` for loop decisions (the Ollama search nudge,
  the iteration limit);
- :meth:`QuestionTrace.end` with the answer, sources, tokens and stop reasons,
  or with the error that ended the question.

Implementations: :mod:`.local` (JSONL and a readable summary on disk) and
:mod:`.langfuse_export` (optional dependency). A tracer must never break a
question: implementations catch their own failures and log them.

Traces contain the question, the model's tool arguments and the document text
the model read. They must never contain API keys: :func:`scrub` removes
key-like strings, and the engine records only the type and RootMind's own
message of a provider error, never the provider's raw error text.
"""

from __future__ import annotations

import functools
import logging
import re
import time
import uuid
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

logger = logging.getLogger(__name__)

# Key- and token-like strings: provider API keys (Anthropic, OpenAI, xAI),
# Langfuse keys and bearer tokens. Documents rarely contain such strings; if
# one does, its trace shows "[redacted]" instead.
_SECRET_RE = re.compile(
    r"\b(?:sk|pk|xai)-[A-Za-z0-9][A-Za-z0-9_\-]{11,}|\bBearer\s+[A-Za-z0-9._~+/=\-]{8,}",
    re.IGNORECASE,
)
REDACTED = "[redacted]"


def redact(text: str) -> str:
    """Replace key- and token-like substrings of ``text``."""
    return _SECRET_RE.sub(REDACTED, text)


def scrub(value: Any) -> Any:
    """``value`` with :func:`redact` applied to every string inside it (dicts, lists, tuples)."""
    if isinstance(value, str):
        return redact(value)
    if isinstance(value, dict):
        return {str(k): scrub(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [scrub(v) for v in value]
    return value


def now_iso() -> str:
    """UTC time with milliseconds, e.g. ``2026-10-25T21:03:04.123+00:00``."""
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def new_trace_id() -> str:
    return uuid.uuid4().hex


def never_raises(method: Callable) -> Callable:
    """Decorator for tracer methods: log a failure instead of breaking the question."""

    @functools.wraps(method)
    def wrapper(*args: Any, **kwargs: Any) -> Any:
        try:
            return method(*args, **kwargs)
        except Exception:
            logger.warning("Tracing failed in %s; the question continues", method.__qualname__)
            logger.debug("Tracing failure", exc_info=True)
            return None

    return wrapper


class Span:
    """One model call or tool call. Created at the start, ended once with what came back.

    ``fields`` holds what was known at the start and, after :meth:`end`, the
    result fields plus ``duration_ms``. Subclasses export in :meth:`_finish`.
    """

    def __init__(self, kind: str, fields: dict[str, Any]):
        self.kind = kind
        self.fields = dict(fields)
        self.start_time = now_iso()
        self._t0 = time.perf_counter()
        self.ended = False

    def end(self, **fields: Any) -> None:
        if self.ended:
            return
        self.ended = True
        self.fields.update(fields)
        self.fields["duration_ms"] = round((time.perf_counter() - self._t0) * 1000, 1)
        self._finish()

    def _finish(self) -> None:
        """Export the finished span (no-op here)."""


class QuestionTrace:
    """The trace of one question. The default implementation records nothing."""

    def __init__(self, trace_id: str, fields: dict[str, Any]):
        self.trace_id = trace_id
        self.fields = dict(fields)
        self.start_time = now_iso()
        self._t0 = time.perf_counter()

    def elapsed_ms(self) -> float:
        return round((time.perf_counter() - self._t0) * 1000, 1)

    def model_call(self, **fields: Any) -> Span:
        """Before an LLM call: ``purpose`` (condense, loop, fallback), ``iteration``, ``model``,
        ``tool_choice``, ``messages`` (count). End the span with ``stop_reason``, token counts,
        ``text``, ``tool_requests`` or ``error``."""
        return Span("model_call", fields)

    def tool_call(self, **fields: Any) -> Span:
        """Before a tool call: ``iteration``, ``name``, ``arguments``. End the span with
        ``result``, ``result_chars``, ``is_error``, ``hits``, ``reads`` or ``skipped``."""
        return Span("tool_call", fields)

    def event(self, kind: str, **fields: Any) -> None:
        """A loop decision without a call of its own."""

    def end(self, **fields: Any) -> None:
        """After the question: ``answer``, ``sources``, ``tokens_used``, ``stop_reasons``,
        ``tool_calls`` (count), or ``error``."""


class Tracer:
    """Creates one :class:`QuestionTrace` per question. The default records nothing."""

    def question(self, *, trace_id: str | None = None, **fields: Any) -> QuestionTrace:
        """``question``, ``provider``, ``model``, ``history_turns`` and ``tags`` (for example
        the eval case and run). ``trace_id`` is set by :class:`MultiTracer`."""
        return QuestionTrace(trace_id or new_trace_id(), fields)

    def close(self) -> None:
        """Flush and release resources."""


# --- fan-out to several tracers ---------------------------------------------------------


class _MultiSpan(Span):
    def __init__(self, kind: str, fields: dict[str, Any], spans: list[Span]):
        super().__init__(kind, fields)
        self._spans = spans

    def _finish(self) -> None:
        result = {k: v for k, v in self.fields.items() if k != "duration_ms"}
        for span in self._spans:
            span.end(**result)


class _MultiQuestion(QuestionTrace):
    def __init__(self, traces: list[QuestionTrace], fields: dict[str, Any]):
        # One id for every backend, so a local trace and a Langfuse trace match.
        super().__init__(traces[0].trace_id, fields)
        self._traces = traces

    def model_call(self, **fields: Any) -> Span:
        return _MultiSpan("model_call", fields, [t.model_call(**fields) for t in self._traces])

    def tool_call(self, **fields: Any) -> Span:
        return _MultiSpan("tool_call", fields, [t.tool_call(**fields) for t in self._traces])

    def event(self, kind: str, **fields: Any) -> None:
        for trace in self._traces:
            trace.event(kind, **fields)

    def end(self, **fields: Any) -> None:
        for trace in self._traces:
            trace.end(**fields)


class MultiTracer(Tracer):
    """Sends every call to several tracers (for example a local file and Langfuse)."""

    def __init__(self, tracers: list[Tracer]):
        if not tracers:
            raise ValueError("MultiTracer needs at least one tracer")
        self.tracers = list(tracers)

    def question(self, *, trace_id: str | None = None, **fields: Any) -> QuestionTrace:
        trace_id = trace_id or new_trace_id()
        return _MultiQuestion(
            [t.question(trace_id=trace_id, **fields) for t in self.tracers], fields
        )

    def close(self) -> None:
        for tracer in self.tracers:
            tracer.close()
