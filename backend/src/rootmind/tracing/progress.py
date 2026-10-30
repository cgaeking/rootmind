"""Live progress of a question, for the desktop chat (``POST /query/stream``).

A tracer whose spans report when they start and when they end, as small
JSON-safe dicts passed to a callback. Unlike :mod:`.local` and
:mod:`.langfuse_export` it keeps no document text: a step names the tool and
its arguments and says how many hits a search had and which windows a read
returned, so the chat can show what the agent is doing while it does it.

The engine creates and ends spans on the event loop (the blocking calls run in
worker threads in between), so the callback is called from the event loop too.
"""

from __future__ import annotations

import itertools
import json
from collections.abc import Callable
from typing import Any

from .base import QuestionTrace, Span, Tracer, never_raises, new_trace_id, scrub

# Upper bounds for what one step carries, so a step stays small whatever the
# model sends or a tool returns.
_MAX_ARGUMENT_CHARS = 300
_MAX_FILES = 5
_MAX_ERROR_CHARS = 200

Emit = Callable[[dict[str, Any]], None]


def _arguments(raw: Any) -> Any:
    """Tool arguments as sent: a dict when they are JSON, else a shortened string."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except ValueError:
            return raw[:_MAX_ARGUMENT_CHARS]
    if isinstance(raw, dict):
        return {
            str(key): value[:_MAX_ARGUMENT_CHARS] if isinstance(value, str) else value
            for key, value in raw.items()
            if isinstance(value, str | int | float | bool) or value is None
        }
    return str(raw)[:_MAX_ARGUMENT_CHARS]


def _model_summary(fields: dict[str, Any]) -> dict[str, Any]:
    tokens = fields.get("total_tokens")
    if tokens is None:
        tokens = (fields.get("input_tokens") or 0) + (fields.get("output_tokens") or 0)
    summary: dict[str, Any] = {
        "stop_reason": fields.get("stop_reason"),
        "tokens": tokens,
        "tool_requests": len(fields.get("tool_requests") or []),
    }
    if fields.get("error"):
        summary["error"] = str(fields["error"])[:_MAX_ERROR_CHARS]
    return summary


def _tool_summary(fields: dict[str, Any]) -> dict[str, Any]:
    hits = fields.get("hits") or []
    files = list(dict.fromkeys(h["file_name"] for h in hits if h.get("file_name")))
    summary: dict[str, Any] = {
        "is_error": bool(fields.get("is_error")),
        "hits": len(hits),
        "files": files[:_MAX_FILES],
        "reads": [
            {
                "file_name": r.get("file_name"),
                "location": r.get("location"),
                "start": r.get("start"),
                "end": r.get("end"),
            }
            for r in fields.get("reads") or []
        ],
        "result_chars": fields.get("result_chars", 0),
    }
    if fields.get("skipped"):
        summary["skipped"] = fields["skipped"]
    if summary["is_error"] and fields.get("result"):
        summary["error"] = str(fields["result"])[:_MAX_ERROR_CHARS]
    return summary


class _ProgressSpan(Span):
    def __init__(self, kind: str, fields: dict[str, Any], step: int, emit: Emit):
        super().__init__(kind, fields)
        self._step = step
        self._emit = emit

    @never_raises
    def _finish(self) -> None:
        summary = _model_summary if self.kind == "model_call" else _tool_summary
        self._emit(
            scrub(
                {
                    "type": "model_end" if self.kind == "model_call" else "tool_end",
                    "step": self._step,
                    "duration_ms": self.fields.get("duration_ms"),
                    **summary(self.fields),
                }
            )
        )


class _ProgressQuestion(QuestionTrace):
    def __init__(self, trace_id: str, fields: dict[str, Any], emit: Emit):
        super().__init__(trace_id, fields)
        self._emit = emit
        self._steps = itertools.count(1)

    @never_raises
    def model_call(self, **fields: Any) -> Span:
        step = next(self._steps)
        self._emit(
            {
                "type": "model_start",
                "step": step,
                "purpose": fields.get("purpose"),
                "iteration": fields.get("iteration"),
            }
        )
        return _ProgressSpan("model_call", fields, step, self._emit)

    @never_raises
    def tool_call(self, **fields: Any) -> Span:
        step = next(self._steps)
        self._emit(
            scrub(
                {
                    "type": "tool_start",
                    "step": step,
                    "iteration": fields.get("iteration"),
                    "name": fields.get("name"),
                    "arguments": _arguments(fields.get("arguments")),
                }
            )
        )
        return _ProgressSpan("tool_call", fields, step, self._emit)

    @never_raises
    def event(self, kind: str, **fields: Any) -> None:
        self._emit({"type": "note", "kind": kind})


class ProgressTracer(Tracer):
    """Reports every model and tool call of a question to ``emit`` as it starts and ends."""

    def __init__(self, emit: Emit):
        self._emit = emit

    def question(self, *, trace_id: str | None = None, **fields: Any) -> QuestionTrace:
        return _ProgressQuestion(trace_id or new_trace_id(), fields, self._emit)
