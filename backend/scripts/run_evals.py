"""Eval harness for the agent loop, over the fictional demo corpus in ``evals/corpus``.

The harness indexes the corpus into a temporary database with the real indexer
(no LLM, no user config) and runs the real ``KnowledgeBaseEngine.query`` for
every case in ``evals/dataset.json``.

Modes:

``--mode dry`` (default; offline, deterministic, no key)
    A scripted policy stands in for the model: search for the case's
    ``dry_search`` terms, read the window the search result suggests around
    the top hit, answer with that excerpt. This measures retrieval and the
    tool layer, not answer quality: hit@1 (the document read is expected),
    hit@5 (an expected document is among the first five hits) and location
    (the cited page/slide/sheet). Exits 1 when hit@5 falls below ``--min-hit``.
    CI runs this mode.

``--mode live``
    Calls a real provider, so it costs money and is never run in CI. Needs
    ``--provider`` and ``--model``; the key is read only from the provider's
    environment variable (never from ``config.yaml``); ``--provider ollama``
    needs none (``OLLAMA_BASE_URL`` overrides ``http://localhost:11434``). Every case runs
    ``--runs`` times. Reports the pass rate (expected source cited, required
    strings present, forbidden strings absent), how much the tool path and
    the cited sources vary between runs, tool calls, tokens and latency.

Results go to ``evals/results/<utc-timestamp>-<mode>-<model>.json`` (gitignored)
and a Markdown summary table is printed.

``--trace DIR`` (both modes) writes a trace of every run: ``DIR/<same name>.jsonl``
(one record per model call, tool call and answer) and ``DIR/<same name>.md``
(the steps of each run, readable). Each run's result row gets its ``trace_id``.
``--langfuse`` also exports the traces to Langfuse (needs the ``tracing``
extra and ``LANGFUSE_*`` variables). Traces contain the questions and the
document text the model read.

Usage (from backend/)::

    uv run python scripts/run_evals.py
    uv run python scripts/run_evals.py --cases c01,c06 --out /tmp/evals
    uv run python scripts/run_evals.py --mode live --provider ollama --model granite4.1:8b \\
        --runs 3 --trace evals/results/traces
    ANTHROPIC_API_KEY=... uv run python scripts/run_evals.py --mode live \\
        --provider anthropic --model <model id> --runs 3
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import statistics
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
DEFAULT_CORPUS = BACKEND / "evals" / "corpus"
DEFAULT_DATASET = BACKEND / "evals" / "dataset.json"
DEFAULT_OUT = BACKEND / "evals" / "results"
CONNECTOR = "demo"
INCLUDE = ["*.pdf", "*.docx", "*.xlsx", "*.pptx", "*.md", "*.txt", "*.csv"]

# Dry-mode gate: the measured hit@5 on the committed corpus (1.00, with c09
# excluded as a known miss), rounded down to 0.05. Any retrieval regression on
# the 15 gated cases fails CI; lower it with --min-hit only for a reason.
DEFAULT_MIN_HIT = 1.0

KEY_ENV = {"anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "xai": "XAI_API_KEY"}
# Ollama needs no key; OLLAMA_BASE_URL points at a server other than localhost.
PROVIDERS = sorted([*KEY_ENV, "ollama"])
REQUIRED_FIELDS = (
    "id",
    "question",
    "history",
    "expected_sources",
    "must_contain",
    "must_contain_any",
    "must_not_contain",
    "tags",
)
DRY_NO_HITS_ANSWER = "I could not find this in the documents."


# --- dataset -----------------------------------------------------------------------------


def load_cases(path: Path, selected: list[str] | None = None) -> list[dict[str, Any]]:
    """Load and validate the dataset; optionally keep only the ``selected`` case ids."""
    cases = json.loads(Path(path).read_text(encoding="utf-8"))
    for case in cases:
        missing = [f for f in REQUIRED_FIELDS if f not in case]
        if missing:
            raise ValueError(f"case {case.get('id', '?')}: missing fields {missing}")
    if selected:
        by_id = {c["id"]: c for c in cases}
        unknown = [s for s in selected if s not in by_id]
        if unknown:
            raise ValueError(f"unknown case ids: {', '.join(unknown)}")
        cases = [by_id[s] for s in selected]
    return cases


# --- index ---------------------------------------------------------------------------------


def build_index(corpus: Path, workdir: Path):
    """Index ``corpus`` into ``workdir/index.db`` as connector ``demo``."""
    from rootmind.api.app import create_connectors
    from rootmind.config import AppConfig, ConnectorConfig
    from rootmind.database.repository import DocumentRepository
    from rootmind.indexer import Indexer, SyncManager

    config = AppConfig(connectors=[ConnectorConfig(name=CONNECTOR, type="local", path=str(corpus))])
    config.database.path = str(workdir / "index.db")
    config.indexer.include = list(INCLUDE)
    repo = DocumentRepository(config.database.path)
    connectors = create_connectors(config)
    manager = SyncManager(repo, connectors, config, Indexer(repo, connectors, config))
    result = manager.full_sync()
    counts = result["results"].get(CONNECTOR, {})
    if not counts.get("indexed"):
        raise RuntimeError(f"nothing indexed from {corpus}: {counts}")
    return config, repo


# --- dry policy ----------------------------------------------------------------------------

_HINT_RE = re.compile(r'read_document\(doc_id="([0-9a-f]+)", start_char=(\d+), end_char=(\d+)\)')
_ID_RE = re.compile(r"^ID: ([0-9a-f]+)$", re.MULTILINE)
_DOC_RE = re.compile(r"<document [^>]*>\n(.*)\n</document>", re.DOTALL)


def _response(*blocks: SimpleNamespace) -> SimpleNamespace:
    tool = any(b.type == "tool_use" for b in blocks)
    return SimpleNamespace(
        stop_reason="tool_use" if tool else "end_turn",
        content=list(blocks),
        usage=SimpleNamespace(input_tokens=0, output_tokens=0),
    )


def _text(value: str) -> SimpleNamespace:
    return SimpleNamespace(type="text", text=value)


def _tool(call_id: str, name: str, args: dict[str, Any]) -> SimpleNamespace:
    return SimpleNamespace(type="tool_use", id=call_id, name=name, input=args)


class DryPolicyClient:
    """A deterministic stand-in for the Anthropic client (``.messages.create``).

    search -> read the suggested window around the top hit -> answer with the
    excerpt. It only reads the conversation it is given, like a model would.
    """

    def __init__(self, case: dict[str, Any]):
        self.case = case
        self.messages = SimpleNamespace(create=self._create)

    def _create(self, **kwargs: Any) -> SimpleNamespace:
        if "tools" not in kwargs:  # the condense step for follow-up questions
            return _response(_text(self.case["question"]))
        messages = kwargs["messages"]
        if len(messages) == 1:
            query = self.case.get("dry_search") or self.case["question"]
            return _response(_tool("s1", "search_documents", {"query": query, "limit": 5}))

        last_call = messages[-2]["content"][-1]
        result = messages[-1]["content"][0]["content"]
        if last_call.name == "search_documents":
            hint = _HINT_RE.search(result)
            ids = _ID_RE.findall(result)
            if not ids:
                return _response(_text(DRY_NO_HITS_ANSWER))
            if hint and hint.group(1) == ids[0]:
                args = {
                    "doc_id": ids[0],
                    "start_char": int(hint.group(2)),
                    "end_char": int(hint.group(3)),
                }
            else:
                args = {"doc_id": ids[0], "start_char": 0, "end_char": 4000}
            return _response(_tool("r1", "read_document", args))

        excerpt = _DOC_RE.search(result)
        return _response(_text(excerpt.group(1)[:1500] if excerpt else result[:1500]))


def _search_hit_names(repo, tool_result: str) -> list[str]:
    names = []
    for doc_id in _ID_RE.findall(tool_result):
        meta = repo.get_document_meta(doc_id)
        names.append(meta["file_name"] if meta else doc_id)
    return names


async def run_dry_case(case, config, repo, tracer=None) -> dict[str, Any]:
    from rootmind import tools
    from rootmind.rlm_engine.engine import KnowledgeBaseEngine

    engine = KnowledgeBaseEngine(repo, config, client=DryPolicyClient(case), tracer=tracer)
    result = await engine.query(
        case["question"], history=case["history"] or None, trace_tags={"case": case["id"]}
    )

    calls = result["tool_calls"]
    search_args = next((c["input"] for c in calls if c["tool"] == "search_documents"), None)
    hits: list[str] = []
    if search_args is not None:
        # Re-run the same search to see the full hit list the policy saw.
        ctx = tools.ToolContext(allowed_connectors=frozenset({CONNECTOR}))
        hits = _search_hit_names(
            repo, tools.execute("search_documents", search_args, repo, ctx).text
        )
    read = next((c["input"]["doc_id"] for c in calls if c["tool"] == "read_document"), None)
    read_name = repo.get_document_meta(read)["file_name"] if read else None
    sources = [{"file_name": s["file_name"], "page": s.get("page")} for s in result["sources"]]

    expected = case["expected_sources"]
    location = case.get("expected_location")
    return {
        "id": case["id"],
        "question": case["question"],
        "tags": case["tags"],
        "expected_sources": expected,
        "expected_location": location,
        "dry_expected_miss": bool(case.get("dry_expected_miss")),
        "search_query": search_args["query"] if search_args else None,
        "search_hits": hits,
        "read": read_name,
        "sources": sources,
        "hit_at_1": (read_name in expected) if expected else None,
        "hit_at_5": any(name in expected for name in hits[:5]) if expected else None,
        "location_match": (
            any(s["file_name"] in expected and s["page"] == location for s in sources)
            if location
            else None
        ),
        "tool_path": tool_path(calls),
        "answer": result["answer"],
        **({"trace_id": result["trace_id"]} if "trace_id" in result else {}),
    }


def _rate(values: list[bool | None]) -> float | None:
    known = [v for v in values if v is not None]
    return round(sum(known) / len(known), 3) if known else None


def summarize_dry(rows: list[dict[str, Any]], min_hit: float) -> dict[str, Any]:
    gated = [r for r in rows if not r["dry_expected_miss"]]
    summary = {
        "cases": len(rows),
        "hit_at_1": _rate([r["hit_at_1"] for r in gated]),
        "hit_at_5": _rate([r["hit_at_5"] for r in gated]),
        "location": _rate([r["location_match"] for r in gated]),
        "known_misses": [r["id"] for r in rows if r["dry_expected_miss"]],
        "min_hit": min_hit,
    }
    hit5 = summary["hit_at_5"]
    summary["passed_gate"] = hit5 is None or hit5 >= min_hit
    return summary


# --- live mode -----------------------------------------------------------------------------


def tool_path(calls: list[dict[str, Any]]) -> str:
    """A compact signature of the tool calls, e.g. ``search_documents(total due) -> read_document``."""
    parts = []
    for call in calls:
        args = call["input"] if isinstance(call["input"], dict) else {}
        arg = args.get("query") or args.get("search_filename") or ""
        parts.append(f"{call['tool']}({arg})" if arg else call["tool"])
    return " -> ".join(parts) or "(no tools)"


# Typography that models put into otherwise correct answers (granite4.1 writes
# "1.1 kg" and "2026‑04‑30"), mapped to what the checks spell.
_CHECK_CHARS = str.maketrans(
    {
        **dict.fromkeys("        ", " "),
        **dict.fromkeys("‐‑‒–−", "-"),
        **dict.fromkeys("‘’‛′", "'"),
        **dict.fromkeys("“”‟″", '"'),
        **dict.fromkeys("*`", None),  # Markdown emphasis and code spans
    }
)
_WHITESPACE_RE = re.compile(r"\s+")


def normalize_for_check(text: str) -> str:
    """Lower case, with Unicode spaces, hyphens and quotes as ASCII and Markdown marks removed.

    The checks compare strings; an answer that says "1.1 kg" with a narrow
    no-break space, or bolds part of a date, is still the expected answer.
    """
    return _WHITESPACE_RE.sub(" ", (text or "").translate(_CHECK_CHARS)).lower()


def run_checks(case: dict[str, Any], answer: str, source_names: list[str]) -> dict[str, bool]:
    """Answer checks; checks that do not apply to a case are left out.

    Answer and expected strings are compared after :func:`normalize_for_check`.
    """
    text = normalize_for_check(answer)

    def found(expected: str) -> bool:
        return normalize_for_check(expected) in text

    checks: dict[str, bool] = {}
    if case["expected_sources"]:
        checks["source_hit"] = any(e in source_names for e in case["expected_sources"])
    if case["must_contain"]:
        checks["contains_all"] = all(found(m) for m in case["must_contain"])
    if case["must_contain_any"]:
        checks["contains_any"] = any(found(m) for m in case["must_contain_any"])
    if case["must_not_contain"]:
        checks["no_forbidden"] = not any(found(m) for m in case["must_not_contain"])
    return checks


async def run_live_case(case, engine, runs: int) -> dict[str, Any]:
    rows = []
    for run in range(1, runs + 1):
        started = time.perf_counter()
        try:
            result = await engine.query(
                case["question"],
                history=case["history"] or None,
                trace_tags={"case": case["id"], "run": run},
            )
        except Exception as e:  # keep going; a failed run counts as a fail
            rows.append({"run": run, "error": f"{type(e).__name__}: {e}", "passed": False})
            continue
        latency = time.perf_counter() - started
        names = [s["file_name"] for s in result["sources"]]
        checks = run_checks(case, result["answer"], names)
        location = case.get("expected_location")
        rows.append(
            {
                "run": run,
                "passed": all(checks.values()),
                "checks": checks,
                "location_match": (
                    any(s.get("page") == location for s in result["sources"]) if location else None
                ),
                "tool_path": tool_path(result["tool_calls"]),
                "tool_calls": len(result["tool_calls"]),
                "sources": sorted(set(names)),
                "tokens": result["tokens_used"],
                "stop_reasons": result.get("stop_reasons", []),
                "latency_s": round(latency, 2),
                "answer": result["answer"],
                **({"trace_id": result["trace_id"]} if "trace_id" in result else {}),
            }
        )
    ok = [r for r in rows if "error" not in r]
    return {
        "id": case["id"],
        "question": case["question"],
        "tags": case["tags"],
        "runs": rows,
        "pass_rate": round(sum(r["passed"] for r in rows) / len(rows), 3),
        "distinct_tool_paths": len(Counter(r["tool_path"] for r in ok)),
        "distinct_source_sets": len(Counter(tuple(r["sources"]) for r in ok)),
        "median_tool_calls": statistics.median(r["tool_calls"] for r in ok) if ok else None,
        "mean_tokens": round(statistics.mean(r["tokens"] for r in ok)) if ok else None,
        "p50_latency_s": round(statistics.median(r["latency_s"] for r in ok), 2) if ok else None,
        "errors": [r["error"] for r in rows if "error" in r],
    }


def summarize_live(rows: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "cases": len(rows),
        "pass_rate": round(statistics.mean(r["pass_rate"] for r in rows), 3) if rows else None,
        "cases_always_passing": sum(r["pass_rate"] == 1.0 for r in rows),
        "cases_with_varying_sources": sum(r["distinct_source_sets"] > 1 for r in rows),
    }


# --- reporting -----------------------------------------------------------------------------


def _mark(value: bool | None) -> str:
    return "n/a" if value is None else ("yes" if value else "NO")


def markdown_dry(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "| case | tags | hit@1 | hit@5 | location | document read | note |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        note = "known FTS limitation (agent must reformulate)" if r["dry_expected_miss"] else ""
        location = _mark(r["location_match"])
        if r["location_match"] is not None:
            got = next((s["page"] for s in r["sources"]), None)
            location += f" ({got or '-'})"
        lines.append(
            f"| {r['id']} | {', '.join(r['tags'])} | {_mark(r['hit_at_1'])} | "
            f"{_mark(r['hit_at_5'])} | {location} | {r['read'] or '-'} | {note} |"
        )
    lines.append("")
    lines.append(
        f"hit@1 {summary['hit_at_1']}, hit@5 {summary['hit_at_5']}, location "
        f"{summary['location']} (known misses excluded: {', '.join(summary['known_misses']) or '-'}); "
        f"gate hit@5 >= {summary['min_hit']}: {'passed' if summary['passed_gate'] else 'FAILED'}"
    )
    return "\n".join(lines)


def markdown_live(rows: list[dict[str, Any]], summary: dict[str, Any]) -> str:
    lines = [
        "| case | pass | tool paths | source sets | median tool calls | mean tokens | p50 latency (s) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        lines.append(
            f"| {r['id']} | {r['pass_rate']:.0%} | {r['distinct_tool_paths']} | "
            f"{r['distinct_source_sets']} | {r['median_tool_calls']} | {r['mean_tokens']} | "
            f"{r['p50_latency_s']} |"
        )
    lines.append("")
    lines.append(
        f"mean pass rate {summary['pass_rate']:.0%}; {summary['cases_always_passing']}/"
        f"{summary['cases']} cases passed every run; {summary['cases_with_varying_sources']} "
        "cases cited different sources between runs"
    )
    return "\n".join(lines)


def result_stem(mode: str, model: str, stamp: str | None = None) -> str:
    """``<utc-timestamp>-<mode>-<model>``, shared by the results and the trace files."""
    stamp = stamp or datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    safe_model = re.sub(r"[^A-Za-z0-9._-]+", "_", model)
    return f"{stamp}-{mode}-{safe_model}"


def write_results(
    out: Path, mode: str, model: str, payload: dict[str, Any], stem: str | None = None
) -> Path:
    out.mkdir(parents=True, exist_ok=True)
    path = out / f"{stem or result_stem(mode, model)}.json"
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    return path


# --- main ----------------------------------------------------------------------------------


@dataclass
class Args:
    mode: str
    provider: str | None
    model: str | None
    runs: int
    cases: list[str] | None
    out: Path
    corpus: Path
    dataset: Path
    min_hit: float
    verbose: bool
    trace: Path | None = None
    langfuse: bool = False


def parse_args(argv: list[str] | None) -> Args:
    parser = argparse.ArgumentParser(description="Run the RootMind eval dataset.")
    parser.add_argument("--mode", choices=["dry", "live"], default="dry")
    parser.add_argument("--provider", choices=PROVIDERS, help="live mode: LLM provider")
    parser.add_argument("--model", help="live mode: model id")
    parser.add_argument("--runs", type=int, default=3, help="live mode: runs per case")
    parser.add_argument("--cases", help="comma-separated case ids (default: all)")
    parser.add_argument("--out", type=Path, default=DEFAULT_OUT, help="results folder")
    parser.add_argument("--corpus", type=Path, default=DEFAULT_CORPUS)
    parser.add_argument("--dataset", type=Path, default=DEFAULT_DATASET)
    parser.add_argument(
        "--min-hit",
        type=float,
        default=DEFAULT_MIN_HIT,
        help=f"dry mode: minimum hit@5 (default {DEFAULT_MIN_HIT})",
    )
    parser.add_argument("--verbose", "-v", action="store_true", help="log indexing and tool calls")
    parser.add_argument(
        "--trace",
        type=Path,
        metavar="DIR",
        help="write a trace of every run (JSONL and a readable .md) to DIR",
    )
    parser.add_argument(
        "--langfuse",
        action="store_true",
        help="also export traces to Langfuse (tracing extra, LANGFUSE_* variables)",
    )
    ns = parser.parse_args(argv)
    if ns.mode == "live" and (not ns.provider or not ns.model):
        parser.error("--mode live needs --provider and --model")
    if ns.runs < 1:
        parser.error("--runs must be at least 1")
    cases = [c.strip() for c in ns.cases.split(",") if c.strip()] if ns.cases else None
    return Args(
        ns.mode,
        ns.provider,
        ns.model,
        ns.runs,
        cases,
        ns.out,
        ns.corpus,
        ns.dataset,
        ns.min_hit,
        ns.verbose,
        ns.trace,
        ns.langfuse,
    )


def make_tracer(args: Args, stem: str):
    """The tracer for ``--trace`` / ``--langfuse``, or None (nothing is imported then)."""
    if args.trace is None and not args.langfuse:
        return None
    from rootmind.tracing import MultiTracer

    tracers = []
    if args.trace is not None:
        from rootmind.tracing.local import LocalTracer

        tracers.append(LocalTracer(args.trace, name=stem))
    if args.langfuse:
        from rootmind.tracing.langfuse_export import LangfuseTracer, TracingConfigError

        try:
            tracers.append(LangfuseTracer())
        except TracingConfigError as e:
            raise RuntimeError(str(e)) from e
    return tracers[0] if len(tracers) == 1 else MultiTracer(tracers)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=logging.INFO if args.verbose else logging.WARNING,
        format="%(levelname)s %(name)s: %(message)s",
    )

    api_key = None
    if args.mode == "live" and args.provider != "ollama":
        api_key = os.environ.get(KEY_ENV[args.provider], "").strip()
        if not api_key:
            print(
                f"error: live mode needs {KEY_ENV[args.provider]} in the environment "
                "(the key is never read from config.yaml). Dry mode needs no key.",
                file=sys.stderr,
            )
            return 2

    try:
        cases = load_cases(args.dataset, args.cases)
    except ValueError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    model = "dry-policy" if args.mode == "dry" else args.model
    stem = result_stem(args.mode, model)
    try:
        tracer = make_tracer(args, stem)
    except RuntimeError as e:
        print(f"error: {e}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory(prefix="rootmind-evals-", ignore_cleanup_errors=True) as tmp:
        config, repo = build_index(args.corpus, Path(tmp))
        try:
            if args.mode == "dry":
                rows = [asyncio.run(run_dry_case(c, config, repo, tracer)) for c in cases]
                summary = summarize_dry(rows, args.min_hit)
                table = markdown_dry(rows, summary)
            else:
                from rootmind.rlm_engine.engine import KnowledgeBaseEngine

                config.llm.provider = args.provider
                config.llm.model = args.model
                config.llm.api_key = api_key
                if args.provider == "ollama" and os.environ.get("OLLAMA_BASE_URL"):
                    config.llm.base_url = os.environ["OLLAMA_BASE_URL"]
                engine = KnowledgeBaseEngine(repo, config, tracer=tracer)
                rows = [asyncio.run(run_live_case(c, engine, args.runs)) for c in cases]
                summary = summarize_live(rows)
                table = markdown_live(rows, summary)
        finally:
            repo.engine.dispose()
            if tracer is not None:
                tracer.close()

    payload = {
        "mode": args.mode,
        "provider": args.provider,
        "model": model,
        "created_utc": datetime.now(UTC).isoformat(timespec="seconds"),
        "corpus": args.corpus.name,
        "dataset": args.dataset.name,
        "summary": summary,
        "cases": rows,
    }
    path = write_results(args.out, args.mode, model, payload, stem)
    print(table)
    print(f"\nResults: {path}")
    if args.trace is not None:
        print(f"Traces: {args.trace / (stem + '.md')} (readable), {args.trace / (stem + '.jsonl')}")
    if args.mode == "dry" and not summary["passed_gate"]:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
