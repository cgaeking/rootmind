"""Optional export of agent traces to Langfuse (self-hosted or Langfuse Cloud).

Needs the extra ``rootmind-backend[tracing]`` (the ``langfuse`` package, v5)
and is off unless turned on explicitly: ``tracing.langfuse: true`` in
``config.yaml``, or ``scripts/run_evals.py --langfuse``. The ``langfuse``
module is imported only here, and this module only when Langfuse export is on.

Configuration comes from the SDK's own environment variables, never from
``config.yaml``: ``LANGFUSE_PUBLIC_KEY``, ``LANGFUSE_SECRET_KEY`` and
``LANGFUSE_BASE_URL`` (default: Langfuse Cloud, https://cloud.langfuse.com).
With a self-hosted Langfuse on your own network, traces stay there; Langfuse
Cloud is a third party. Traces contain the question, the tool arguments and
the document text the model read (see SECURITY.md).

One trace per question: a root ``agent`` observation, a ``generation`` per
LLM call (model, token usage, stop reason, text and tool requests) and a
``tool`` observation per tool call (arguments, result text up to
``max_result_chars``, hits and windows read).
"""

from __future__ import annotations

import logging
import os
from typing import Any

from .base import QuestionTrace, Span, Tracer, never_raises, new_trace_id, scrub

logger = logging.getLogger(__name__)

REQUIRED_ENV = ("LANGFUSE_PUBLIC_KEY", "LANGFUSE_SECRET_KEY")
DEFAULT_MAX_RESULT_CHARS = 20_000


class TracingConfigError(RuntimeError):
    """Langfuse export was requested but cannot run (package or keys missing)."""


def _usage(fields: dict[str, Any]) -> dict[str, int] | None:
    usage = {
        key: fields[name]
        for key, name in (
            ("input", "input_tokens"),
            ("output", "output_tokens"),
            ("total", "total_tokens"),
        )
        if isinstance(fields.get(name), int)
    }
    return usage or None


class _LangfuseSpan(Span):
    def __init__(self, kind: str, fields: dict[str, Any], observation: Any, max_chars: int):
        super().__init__(kind, fields)
        self._observation = observation
        self._max_chars = max_chars

    @never_raises
    def _finish(self) -> None:
        fields = scrub(self.fields)
        error = fields.get("error")
        level = {"level": "ERROR", "status_message": error} if error else {}
        if self.kind == "model_call":
            self._observation.update(
                output={"text": fields.get("text"), "tool_requests": fields.get("tool_requests")},
                usage_details=_usage(fields),
                metadata={
                    "stop_reason": fields.get("stop_reason"),
                    "duration_ms": fields.get("duration_ms"),
                },
                **level,
            )
        else:
            result = fields.get("result")
            if isinstance(result, str) and len(result) > self._max_chars:
                result = result[: self._max_chars] + f"… [{len(result) - self._max_chars:,} more]"
            if fields.get("is_error") and not error:
                level = {"level": "ERROR", "status_message": "tool returned an error"}
            self._observation.update(
                output=result if not fields.get("skipped") else f"not run: {fields['skipped']}",
                metadata={
                    "result_chars": fields.get("result_chars"),
                    "hits": fields.get("hits"),
                    "reads": fields.get("reads"),
                    "duration_ms": fields.get("duration_ms"),
                },
                **level,
            )
        self._observation.end()


class _NullObservation:
    def update(self, **kwargs: Any) -> None:
        pass

    def end(self) -> None:
        pass


class _LangfuseQuestion(QuestionTrace):
    def __init__(self, tracer: LangfuseTracer, trace_id: str, fields: dict[str, Any]):
        super().__init__(trace_id, fields)
        self._tracer = tracer
        self._root = tracer.client.start_observation(
            name="rootmind.question",
            as_type="agent",
            input=scrub(fields.get("question")),
            metadata=scrub(
                {
                    "rootmind_trace_id": trace_id,
                    "provider": fields.get("provider"),
                    "model": fields.get("model"),
                    "history_turns": fields.get("history_turns"),
                    **{f"tag_{k}": v for k, v in (fields.get("tags") or {}).items()},
                }
            ),
        )

    def _child(self, **kwargs: Any) -> Any:
        try:
            return self._root.start_observation(**kwargs)
        except Exception:
            logger.warning("Langfuse: could not start an observation")
            return _NullObservation()

    def model_call(self, **fields: Any) -> Span:
        purpose = fields.get("purpose", "loop")
        observation = self._child(
            name=f"llm.{purpose}",
            as_type="generation",
            model=fields.get("model"),
            metadata=scrub(
                {
                    "iteration": fields.get("iteration"),
                    "tool_choice": fields.get("tool_choice"),
                    "messages": fields.get("messages"),
                }
            ),
        )
        return _LangfuseSpan("model_call", fields, observation, self._tracer.max_result_chars)

    def tool_call(self, **fields: Any) -> Span:
        observation = self._child(
            name=f"tool.{fields.get('name')}",
            as_type="tool",
            input=scrub(fields.get("arguments")),
            metadata={"iteration": fields.get("iteration")},
        )
        return _LangfuseSpan("tool_call", fields, observation, self._tracer.max_result_chars)

    @never_raises
    def event(self, kind: str, **fields: Any) -> None:
        observation = self._child(name=f"event.{kind}", as_type="span", metadata=scrub(fields))
        observation.end()

    @never_raises
    def end(self, **fields: Any) -> None:
        fields = scrub(fields)
        error = fields.get("error")
        self._root.update(
            output={"answer": fields.get("answer"), "sources": fields.get("sources")},
            metadata={
                "tokens_used": fields.get("tokens_used"),
                "stop_reasons": fields.get("stop_reasons"),
                "tool_calls": fields.get("tool_calls"),
                "standalone_question": fields.get("standalone_question"),
            },
            **({"level": "ERROR", "status_message": error} if error else {}),
        )
        self._root.end()


class LangfuseTracer(Tracer):
    """Exports traces through the Langfuse SDK.

    Args:
        client: A Langfuse client, or a fake in tests. When omitted, the
            ``langfuse`` package is imported and ``Langfuse()`` is created from
            the ``LANGFUSE_*`` environment variables.
        max_result_chars: Cap for the tool result text sent per tool call.

    Raises:
        TracingConfigError: the package is not installed or a key is missing.
    """

    def __init__(
        self, client: Any | None = None, *, max_result_chars: int = DEFAULT_MAX_RESULT_CHARS
    ):
        if client is None:
            missing = [name for name in REQUIRED_ENV if not os.environ.get(name, "").strip()]
            if missing:
                raise TracingConfigError(
                    f"Langfuse export needs {' and '.join(missing)} in the environment"
                )
            try:
                from langfuse import Langfuse
            except ImportError as e:
                raise TracingConfigError(
                    "Langfuse export needs the optional dependency: "
                    "uv sync --extra tracing (or pip install 'rootmind-backend[tracing]')"
                ) from e
            client = Langfuse()
        self.client = client
        self.max_result_chars = max_result_chars

    @never_raises
    def question(self, *, trace_id: str | None = None, **fields: Any) -> QuestionTrace:
        return _LangfuseQuestion(self, trace_id or new_trace_id(), fields)

    @never_raises
    def close(self) -> None:
        self.client.flush()
