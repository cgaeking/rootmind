"""A local trace file: one JSON line per event, plus a readable Markdown summary.

Nothing leaves the computer. Used by ``scripts/run_evals.py --trace <dir>`` and,
when ``tracing.local`` is on in ``config.yaml``, by the backend (under
``<home>/logs/traces/``).

``<name>.jsonl`` has one record per line, each with ``type`` and ``trace_id``:

- ``question``: the question, provider, model, history turns and tags;
- ``model_call``: purpose (condense, loop, fallback), iteration, tool choice,
  stop reason, token counts, the model's text and the tools it requested,
  start time and duration, or the error;
- ``tool_call``: name, arguments, result size, the result text (capped at
  ``max_result_chars``), the documents a search returned (``hits``) and the
  windows a read returned (``reads``), or why it was not run (``skipped``);
- ``event``: loop decisions (``search_nudge``, ``iteration_limit``);
- ``answer``: the answer, sources, tokens, stop reasons and tool-call count,
  or the error.

``<name>.md`` shows the same per question as a numbered list of steps. Both
files contain questions and document text; see SECURITY.md.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .base import QuestionTrace, Span, Tracer, never_raises, now_iso, scrub

logger = logging.getLogger(__name__)

DEFAULT_MAX_RESULT_CHARS = 20_000
SUMMARY_TEXT_CHARS = 300


def _clip(text: Any, limit: int) -> str:
    text = "" if text is None else str(text)
    return text if len(text) <= limit else text[:limit] + f"… [{len(text) - limit:,} more chars]"


def _one_line(text: Any, limit: int = SUMMARY_TEXT_CHARS) -> str:
    return _clip(" ".join(str(text or "").split()), limit)


def _args(arguments: Any) -> str:
    if isinstance(arguments, str):
        return _one_line(arguments, 200)
    return _one_line(json.dumps(arguments, ensure_ascii=False, default=str), 200)


class _LocalSpan(Span):
    def __init__(self, kind: str, fields: dict[str, Any], owner: _LocalQuestion):
        super().__init__(kind, fields)
        self._owner = owner

    @never_raises
    def _finish(self) -> None:
        self._owner.span_done(self)


class _LocalQuestion(QuestionTrace):
    def __init__(self, tracer: LocalTracer, trace_id: str, fields: dict[str, Any]):
        super().__init__(trace_id, fields)
        self._tracer = tracer
        self._seq = 0
        self._steps: list[str] = []
        tracer.write({"type": "question", "trace_id": trace_id, "time": self.start_time, **fields})

    def _next(self) -> int:
        self._seq += 1
        return self._seq

    def model_call(self, **fields: Any) -> Span:
        return _LocalSpan("model_call", fields, self)

    def tool_call(self, **fields: Any) -> Span:
        return _LocalSpan("tool_call", fields, self)

    def span_done(self, span: Span) -> None:
        fields = dict(span.fields)
        if span.kind == "tool_call" and "result" in fields:
            fields["result"] = _clip(fields["result"], self._tracer.max_result_chars)
        record = {
            "type": span.kind,
            "trace_id": self.trace_id,
            "seq": self._next(),
            "start": span.start_time,
            **fields,
        }
        self._tracer.write(record)
        self._steps.append(_summary_step(span.kind, fields))

    @never_raises
    def event(self, kind: str, **fields: Any) -> None:
        self._tracer.write(
            {
                "type": "event",
                "trace_id": self.trace_id,
                "seq": self._next(),
                "time": now_iso(),
                "event": kind,
                **fields,
            }
        )
        details = ", ".join(f"{k}={v}" for k, v in fields.items())
        self._steps.append(f"event: {kind}" + (f" ({details})" if details else ""))

    @never_raises
    def end(self, **fields: Any) -> None:
        duration = self.elapsed_ms()
        self._tracer.write(
            {
                "type": "answer",
                "trace_id": self.trace_id,
                "time": now_iso(),
                "duration_ms": duration,
                **fields,
            }
        )
        self._tracer.write_summary(_summary_block(self, fields, duration))


class LocalTracer(Tracer):
    """Writes ``<directory>/<name>.jsonl`` and, with ``summary``, ``<name>.md``.

    Safe for concurrent questions: every write appends one complete line (or
    one complete summary block) under a lock. The files are opened per write,
    so no handle stays open between questions.
    """

    def __init__(
        self,
        directory: str | Path,
        *,
        name: str | None = None,
        summary: bool = True,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
    ):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        stem = name or f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{os.getpid()}"
        self.jsonl_path = self.directory / f"{stem}.jsonl"
        self.summary_path = self.directory / f"{stem}.md" if summary else None
        self.max_result_chars = max_result_chars
        self._lock = threading.Lock()

    @never_raises
    def question(self, *, trace_id: str | None = None, **fields: Any) -> QuestionTrace:
        from .base import new_trace_id

        return _LocalQuestion(self, trace_id or new_trace_id(), fields)

    def write(self, record: dict[str, Any]) -> None:
        line = json.dumps(scrub(record), ensure_ascii=False, default=str)
        with self._lock, open(self.jsonl_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(line + "\n")

    def write_summary(self, block: str) -> None:
        if self.summary_path is None:
            return
        with self._lock, open(self.summary_path, "a", encoding="utf-8", newline="\n") as f:
            f.write(scrub(block) + "\n")


# --- the readable summary -------------------------------------------------------------------


def _tokens(fields: dict[str, Any]) -> str:
    total = fields.get("total_tokens")
    if total is None and (fields.get("input_tokens") or fields.get("output_tokens")):
        total = (fields.get("input_tokens") or 0) + (fields.get("output_tokens") or 0)
    if total is None:
        return ""
    prompt = fields.get("input_tokens")
    return f", {total:,} tok" + (f" ({prompt:,} in)" if prompt is not None else "")


def _summary_step(kind: str, fields: dict[str, Any]) -> str:
    seconds = (fields.get("duration_ms") or 0) / 1000
    if kind == "model_call":
        head = f"model {fields.get('purpose', '?')}"
        if fields.get("iteration") is not None:
            head += f" #{fields['iteration']}"
        if fields.get("error"):
            return f"{head} ({seconds:.2f} s): ERROR {fields['error']}"
        line = f"{head} ({seconds:.2f} s{_tokens(fields)}, stop: {fields.get('stop_reason')})"
        requests = fields.get("tool_requests") or []
        if requests:
            line += " → " + ", ".join(r.get("name", "?") for r in requests)
        if fields.get("text"):
            line += f': "{_one_line(fields["text"])}"'
        return line
    name = fields.get("name", "?")
    line = f"  - {name} {_args(fields.get('arguments'))}"
    if fields.get("skipped"):
        return line + f" → not run ({fields['skipped']})"
    hits = fields.get("hits") or []
    reads = fields.get("reads") or []
    if fields.get("is_error"):
        outcome = f"ERROR: {_one_line(fields.get('result'), 200)}"
    elif hits:
        shown = ", ".join(
            (h.get("file_name") or h.get("doc_id", "?"))
            + (f" ({h['page']})" if h.get("page") else "")
            for h in hits[:5]
        )
        outcome = f"{len(hits)} hit(s): {shown}" + (" …" if len(hits) > 5 else "")
    elif reads:
        outcome = "read " + ", ".join(
            f"{r.get('file_name') or r.get('doc_id')} chars {r.get('start', 0):,}-{r.get('end', 0):,}"
            + (f" (from {r['location']})" if r.get("location") else "")
            for r in reads
        )
    elif name in ("search_documents", "search_fuzzy"):
        outcome = "no hits"
    else:
        outcome = "ok"
    return line + f" → {outcome} [{fields.get('result_chars', 0):,} chars, {seconds:.2f} s]"


def _summary_block(trace: QuestionTrace, end: dict[str, Any], duration_ms: float) -> str:
    tags = trace.fields.get("tags") or {}
    label = " ".join(f"{k} {v}" for k, v in tags.items())
    title = f"## {label + ' · ' if label else ''}trace {trace.trace_id[:8]}"
    title += f" · {duration_ms / 1000:.1f} s"
    if end.get("tokens_used") is not None:
        title += f" · {end['tokens_used']:,} tokens"
    lines = [title, "", f"**Question:** {_one_line(trace.fields.get('question'), 500)}"]
    if end.get("standalone_question") and end["standalone_question"] != trace.fields.get(
        "question"
    ):
        lines.append(f"**Condensed:** {_one_line(end['standalone_question'], 500)}")
    lines.append(
        f"**Model:** {trace.fields.get('provider')} / {trace.fields.get('model')}",
    )
    lines.append("")
    number = 0
    steps = getattr(trace, "_steps", [])
    for step in steps:
        if step.startswith("  - "):
            lines.append("   " + step[2:])
        else:
            number += 1
            lines.append(f"{number}. {step}")
    lines.append("")
    if end.get("error"):
        lines.append(f"**Error:** {end['error']}")
    else:
        lines.append(f"**Answer:** {_one_line(end.get('answer'), 2_000)}")
        sources = end.get("sources") or []
        shown = ", ".join(
            s.get("file_name", "?") + (f" ({s['page']})" if s.get("page") else "") for s in sources
        )
        lines.append(f"**Sources:** {shown or '-'}")
    lines.append(f"**Stop reasons:** {', '.join(end.get('stop_reasons') or []) or '-'}")
    lines.append("")
    return "\n".join(lines)
