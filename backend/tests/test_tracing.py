"""Optional tracing of the agent loop: a no-op when off, a readable local trace, Langfuse fakes."""

from __future__ import annotations

import ast
import json
import os
import socket
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import anthropic
import httpx
import pytest
from fakes import (
    FakeAnthropic,
    FakeOpenAI,
    anthropic_response,
    openai_response,
    openai_tool_call,
    text,
    tool_use,
)
from helpers import TEST_API_KEY, doc_id, make_engine

from rootmind.config import TracingConfig
from rootmind.rlm_engine import engine as engine_module
from rootmind.rlm_engine.engine import KnowledgeBaseEngine, LLMProviderError

TRACING_MODULES = ("rootmind.tracing", "langfuse", "langsmith")


def _script(invoice: str) -> list:
    return [
        anthropic_response(tool_use("t1", "search_documents", {"query": "total due"})),
        anthropic_response(
            tool_use("t2", "read_document", {"doc_id": invoice, "start_char": 0, "end_char": 400})
        ),
        anthropic_response(text("1,179.36 (invoice.pdf, Page 2).")),
    ]


def _records(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]


def _files_under(*roots: Path) -> set[Path]:
    return {Path(d, f) for root in roots for d, _, files in os.walk(root) for f in files}


def _local_tracer(directory: Path, **kwargs):
    from rootmind.tracing.local import LocalTracer

    return LocalTracer(directory, name="run", **kwargs)


# --- tracing off -----------------------------------------------------------------------------


async def test_without_a_tracer_nothing_is_imported_written_or_sent(
    app_config, indexed_repo, tmp_path
):
    """The acceptance check of #48: with tracing off, behaviour, imports, files and network
    stay as they were."""
    invoice = doc_id(indexed_repo, "invoice.pdf")
    watched = (Path.cwd(), Path(os.environ["RLM_HOME"]).parent)
    before = _files_under(*watched)

    def no_network(*args, **kwargs):
        raise AssertionError("network access while tracing is off")

    plain = FakeAnthropic(_script(invoice))
    with pytest.MonkeyPatch.context() as mp:
        for name in list(sys.modules):
            if name.startswith(TRACING_MODULES):
                mp.delitem(sys.modules, name)
        mp.setattr(socket.socket, "connect", no_network)
        mp.setattr(socket, "create_connection", no_network)

        result = await make_engine(app_config, indexed_repo, plain).query("Total due?")

        assert not [m for m in sys.modules if m.startswith(TRACING_MODULES)]
    assert _files_under(*watched) == before
    assert "trace_id" not in result

    # The same question with a tracer: the model sees exactly the same calls and
    # the caller gets the same result, plus the trace id.
    traced = FakeAnthropic(_script(invoice))
    engine = KnowledgeBaseEngine(
        indexed_repo, app_config, client=traced, tracer=_local_tracer(tmp_path / "traces")
    )
    traced_result = await engine.query("Total due?")

    assert traced.calls == plain.calls
    assert traced_result.pop("trace_id")
    assert traced_result == result


def test_the_engine_module_does_not_import_tracing():
    """Only a TYPE_CHECKING block may name the tracing package."""
    tree = ast.parse(Path(engine_module.__file__).read_text(encoding="utf-8"))
    runtime_imports = [node for node in tree.body if isinstance(node, ast.Import | ast.ImportFrom)]
    names = [
        (node.module or "") if isinstance(node, ast.ImportFrom) else node.names[0].name
        for node in runtime_imports
    ]
    assert not [n for n in names if "tracing" in n or n in ("langfuse", "langsmith")]


def test_backend_without_tracing_config_builds_no_tracer(monkeypatch):
    from rootmind.api import app as app_module
    from rootmind.config import AppConfig

    for name in list(sys.modules):
        if name.startswith("rootmind.tracing"):
            monkeypatch.delitem(sys.modules, name)

    assert app_module._tracer(AppConfig()) is None
    assert "rootmind.tracing" not in sys.modules


# --- the local trace -------------------------------------------------------------------------


async def test_local_trace_makes_a_run_readable(app_config, indexed_repo, tmp_path):
    invoice = doc_id(indexed_repo, "invoice.pdf")
    tracer = _local_tracer(tmp_path / "traces")
    engine = KnowledgeBaseEngine(
        indexed_repo, app_config, client=FakeAnthropic(_script(invoice)), tracer=tracer
    )

    result = await engine.query("Total due?", trace_tags={"case": "c01", "run": 2})

    records = _records(tracer.jsonl_path)
    assert [r["type"] for r in records] == [
        "question",
        "model_call",
        "tool_call",
        "model_call",
        "tool_call",
        "model_call",
        "answer",
    ]
    assert {r["trace_id"] for r in records} == {result["trace_id"]}
    question, first, search, _, read, last, answer = records
    assert question["question"] == "Total due?"
    assert question["tags"] == {"case": "c01", "run": 2}
    assert first["purpose"] == "loop" and first["iteration"] == 1
    assert first["tool_choice"] == {"type": "any"}
    assert first["stop_reason"] == "tool_use"
    assert first["input_tokens"] == 10 and first["output_tokens"] == 5
    assert first["tool_requests"] == [
        {"name": "search_documents", "arguments": {"query": "total due"}}
    ]
    assert search["name"] == "search_documents" and search["arguments"] == {"query": "total due"}
    assert {"doc_id": invoice, "file_name": "invoice.pdf", "page": "Page 2"} in search["hits"]
    assert search["result_chars"] == len(search["result"]) > 0
    assert search["duration_ms"] >= 0
    end = min(400, indexed_repo.get_document_meta(invoice)["content_length"])
    assert read["reads"] == [
        {"doc_id": invoice, "file_name": "invoice.pdf", "start": 0, "end": end, "location": None}
    ]
    assert "<document" in read["result"]
    assert last["text"] == "1,179.36 (invoice.pdf, Page 2)."
    assert answer["answer"] == result["answer"]
    assert answer["sources"][0]["file_name"] == "invoice.pdf"
    assert answer["stop_reasons"] == ["tool_use", "tool_use", "end_turn"]
    assert answer["tool_calls"] == 2

    summary = tracer.summary_path.read_text(encoding="utf-8")
    assert "## case c01 run 2" in summary
    assert "**Question:** Total due?" in summary
    assert "search_documents" in summary and "invoice.pdf (Page 2)" in summary
    assert f"read invoice.pdf chars 0-{end}" in summary
    assert "**Answer:** 1,179.36 (invoice.pdf, Page 2)." in summary


async def test_result_text_is_capped(app_config, indexed_repo, tmp_path):
    invoice = doc_id(indexed_repo, "invoice.pdf")
    tracer = _local_tracer(tmp_path, max_result_chars=50)
    engine = KnowledgeBaseEngine(
        indexed_repo, app_config, client=FakeAnthropic(_script(invoice)), tracer=tracer
    )
    await engine.query("Total due?")

    search = next(r for r in _records(tracer.jsonl_path) if r["type"] == "tool_call")
    assert search["result"].startswith(search["result"][:50])
    assert "more chars]" in search["result"]
    assert search["result_chars"] > 50  # the size is the real one


async def test_openai_loop_records_nudge_limits_and_fallback(app_config, indexed_repo, tmp_path):
    app_config.llm.provider = "ollama"
    app_config.llm.model = "granite-test"
    many = [
        openai_tool_call(f"c{i}", "get_statistics", {})
        for i in range(engine_module.MAX_TOOL_CALLS_PER_TURN + 1)
    ]
    fake = FakeOpenAI(
        [
            openai_response(content="From memory."),
            openai_response(tool_calls=many),
            openai_response(content="Best effort.", total_tokens=40),
        ]
    )
    tracer = _local_tracer(tmp_path)

    result = await KnowledgeBaseEngine(indexed_repo, app_config, client=fake, tracer=tracer).query(
        "q", max_iterations=2
    )

    records = _records(tracer.jsonl_path)
    events = [r["event"] for r in records if r["type"] == "event"]
    assert events == ["search_nudge", "iteration_limit"]
    tools_ = [r for r in records if r["type"] == "tool_call"]
    assert [t.get("skipped") for t in tools_][-1] == "per-turn limit"
    assert all(t["iteration"] == 2 for t in tools_)
    fallback = [r for r in records if r["type"] == "model_call"][-1]
    assert fallback["purpose"] == "fallback" and fallback["tool_choice"] == "none"
    assert fallback["total_tokens"] == 40
    assert result["answer"] == "Best effort."
    assert "not run (per-turn limit)" in tracer.summary_path.read_text(encoding="utf-8")


async def test_condense_step_is_a_model_span(app_config, indexed_repo, tmp_path):
    fake = FakeAnthropic(
        [anthropic_response(text("What is the warranty?")), anthropic_response(text("5 years"))]
    )
    tracer = _local_tracer(tmp_path)
    await KnowledgeBaseEngine(indexed_repo, app_config, client=fake, tracer=tracer).query(
        "And its warranty?", history=[{"role": "user", "text": "Tent weight?"}]
    )
    records = _records(tracer.jsonl_path)
    assert [r.get("purpose") for r in records if r["type"] == "model_call"] == [
        "condense",
        "loop",
    ]
    assert records[-1]["standalone_question"] == "What is the warranty?"


async def test_traces_never_contain_api_keys(app_config, indexed_repo, tmp_path):
    """Provider errors can echo the key; tool arguments and documents can contain key-like text."""
    app_config.llm.api_key = TEST_API_KEY
    request = httpx.Request("POST", "http://127.0.0.1:9/v1/messages")
    leaky = anthropic.AuthenticationError(
        f"invalid x-api-key {TEST_API_KEY}",
        response=httpx.Response(401, request=request),
        body=None,
    )
    fake = FakeAnthropic(
        [
            anthropic_response(tool_use("t1", "search_documents", {"query": TEST_API_KEY})),
            leaky,
        ]
    )
    tracer = _local_tracer(tmp_path)

    with pytest.raises(LLMProviderError):
        await KnowledgeBaseEngine(indexed_repo, app_config, client=fake, tracer=tracer).query("q")

    written = tracer.jsonl_path.read_text(encoding="utf-8") + tracer.summary_path.read_text(
        encoding="utf-8"
    )
    assert TEST_API_KEY not in written
    assert "[redacted]" in written
    answer = _records(tracer.jsonl_path)[-1]
    assert answer["type"] == "answer"
    assert answer["error"].startswith("LLMProviderError: ")


def test_redact_patterns():
    from rootmind.tracing.base import redact

    for secret in (
        "sk-ant-api03-abcdefghijklmnop",
        "sk-proj-abcdefghijklmnop",
        "xai-abcdefghijklmnopqrs",
        "pk-lf-1234567890abcdef",
        "Bearer abcdefgh12345678",
    ):
        assert redact(f"key {secret} end") == "key [redacted] end"
    assert redact("risk-free task-list sk-short") == "risk-free task-list sk-short"


async def test_a_failing_tracer_does_not_break_the_question(
    app_config, indexed_repo, tmp_path, monkeypatch
):
    from rootmind.tracing.local import LocalTracer

    tracer = _local_tracer(tmp_path)

    def disk_full(self, record):
        raise OSError("No space left on device")

    monkeypatch.setattr(LocalTracer, "write", disk_full)
    invoice = doc_id(indexed_repo, "invoice.pdf")
    engine = KnowledgeBaseEngine(
        indexed_repo, app_config, client=FakeAnthropic(_script(invoice)), tracer=tracer
    )

    result = await engine.query("Total due?")

    assert result["answer"] == "1,179.36 (invoice.pdf, Page 2)."
    assert "trace_id" not in result  # the question trace could not start


async def test_multi_tracer_shares_one_trace_id(app_config, indexed_repo, tmp_path):
    from rootmind.tracing import MultiTracer

    first, second = _local_tracer(tmp_path / "a"), _local_tracer(tmp_path / "b")
    invoice = doc_id(indexed_repo, "invoice.pdf")
    engine = KnowledgeBaseEngine(
        indexed_repo,
        app_config,
        client=FakeAnthropic(_script(invoice)),
        tracer=MultiTracer([first, second]),
    )
    result = await engine.query("Total due?")

    a, b = _records(first.jsonl_path), _records(second.jsonl_path)
    assert [r["type"] for r in a] == [r["type"] for r in b]
    assert {r["trace_id"] for r in a} == {r["trace_id"] for r in b} == {result["trace_id"]}


# --- backend switch --------------------------------------------------------------------------


def test_backend_local_tracing_writes_under_logs_traces(tmp_path):
    from rootmind.api import app as app_module
    from rootmind.config import AppConfig, app_home

    config = AppConfig(tracing=TracingConfig(local=True))
    tracer = app_module._tracer(config)

    assert tracer.jsonl_path.parent == app_home() / "logs" / "traces"
    assert app_module._tracer(config) is tracer  # kept across config reloads


def test_backend_langfuse_without_keys_logs_and_continues(monkeypatch, caplog):
    from rootmind.tracing import tracer_from_config

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.delenv("LANGFUSE_SECRET_KEY", raising=False)

    assert tracer_from_config(TracingConfig(langfuse=True), Path("unused")) is None
    assert "LANGFUSE_PUBLIC_KEY" in caplog.text


# --- Langfuse exporter (fake client, no network) ---------------------------------------------


class FakeObservation:
    def __init__(self, log: list, **kwargs):
        self.log = log
        self.kwargs = kwargs
        self.updates: list[dict] = []
        self.children: list[FakeObservation] = []
        self.ended = False
        log.append(self)

    def start_observation(self, **kwargs):
        child = FakeObservation(self.log, **kwargs)
        self.children.append(child)
        return child

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def end(self):
        self.ended = True


class FakeLangfuse:
    def __init__(self, **kwargs):
        self.kwargs = kwargs
        self.log: list[FakeObservation] = []
        self.flushed = False

    def start_observation(self, **kwargs):
        return FakeObservation(self.log, **kwargs)

    def flush(self):
        self.flushed = True


async def test_langfuse_export_with_a_fake_client(app_config, indexed_repo):
    from rootmind.tracing.langfuse_export import LangfuseTracer

    client = FakeLangfuse()
    tracer = LangfuseTracer(client)
    invoice = doc_id(indexed_repo, "invoice.pdf")
    engine = KnowledgeBaseEngine(
        indexed_repo, app_config, client=FakeAnthropic(_script(invoice)), tracer=tracer
    )

    result = await engine.query("Total due?", trace_tags={"case": "c01"})
    tracer.close()

    root = client.log[0]
    assert root.kwargs["as_type"] == "agent" and root.kwargs["input"] == "Total due?"
    assert root.kwargs["metadata"]["rootmind_trace_id"] == result["trace_id"]
    assert root.kwargs["metadata"]["tag_case"] == "c01"
    assert [(c.kwargs["as_type"], c.kwargs["name"]) for c in root.children] == [
        ("generation", "llm.loop"),
        ("tool", "tool.search_documents"),
        ("generation", "llm.loop"),
        ("tool", "tool.read_document"),
        ("generation", "llm.loop"),
    ]
    assert all(o.ended for o in client.log)
    generation = root.children[0]
    assert generation.kwargs["model"] == app_config.llm.model
    assert generation.updates[0]["usage_details"] == {"input": 10, "output": 5}
    assert generation.updates[0]["metadata"]["stop_reason"] == "tool_use"
    search = root.children[1]
    assert search.kwargs["input"] == {"query": "total due"}
    assert "invoice.pdf" in search.updates[0]["output"]
    assert root.updates[-1]["output"]["answer"] == result["answer"]
    assert client.flushed


def test_langfuse_needs_keys(monkeypatch):
    from rootmind.tracing.langfuse_export import LangfuseTracer, TracingConfigError

    monkeypatch.delenv("LANGFUSE_PUBLIC_KEY", raising=False)
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")  # pragma: allowlist secret
    with pytest.raises(TracingConfigError, match="LANGFUSE_PUBLIC_KEY"):
        LangfuseTracer()


def test_langfuse_needs_the_extra(monkeypatch):
    from rootmind.tracing.langfuse_export import LangfuseTracer, TracingConfigError

    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")  # pragma: allowlist secret
    monkeypatch.setitem(sys.modules, "langfuse", None)  # import fails as if not installed
    with pytest.raises(TracingConfigError, match="extra tracing"):
        LangfuseTracer()


def test_langfuse_client_is_built_from_the_environment_only_when_enabled(monkeypatch):
    from rootmind.tracing.langfuse_export import LangfuseTracer

    module = ModuleType("langfuse")
    module.Langfuse = FakeLangfuse
    monkeypatch.setitem(sys.modules, "langfuse", module)
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-lf-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-lf-test")  # pragma: allowlist secret

    tracer = LangfuseTracer()

    assert isinstance(tracer.client, FakeLangfuse)
    assert tracer.client.kwargs == {}  # the SDK reads LANGFUSE_* itself; no key passes through us


async def test_langfuse_failures_do_not_break_the_question(app_config, indexed_repo):
    from rootmind.tracing.langfuse_export import LangfuseTracer

    broken = SimpleNamespace(
        start_observation=lambda **kwargs: (_ for _ in ()).throw(RuntimeError("down")),
        flush=lambda: None,
    )
    invoice = doc_id(indexed_repo, "invoice.pdf")
    engine = KnowledgeBaseEngine(
        indexed_repo,
        app_config,
        client=FakeAnthropic(_script(invoice)),
        tracer=LangfuseTracer(broken),
    )

    result = await engine.query("Total due?")

    assert result["answer"] == "1,179.36 (invoice.pdf, Page 2)."


async def test_langfuse_exporter_against_the_real_sdk_without_export(app_config, indexed_repo):
    """Runs only where the `tracing` extra is installed; the SDK exports nothing here."""
    langfuse = pytest.importorskip("langfuse")
    from rootmind.tracing.langfuse_export import LangfuseTracer

    client = langfuse.Langfuse(
        public_key="pk-lf-test",
        secret_key="sk-lf-test",  # pragma: allowlist secret
        base_url="http://127.0.0.1:9",
        tracing_enabled=False,
    )
    invoice = doc_id(indexed_repo, "invoice.pdf")
    engine = KnowledgeBaseEngine(
        indexed_repo,
        app_config,
        client=FakeAnthropic(_script(invoice)),
        tracer=LangfuseTracer(client),
    )

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(engine_module.logger, "warning", _fail_on_warning)
        mp.setattr("rootmind.tracing.base.logger.warning", _fail_on_warning)
        mp.setattr("rootmind.tracing.langfuse_export.logger.warning", _fail_on_warning)
        result = await engine.query("Total due?")

    assert result["answer"] == "1,179.36 (invoice.pdf, Page 2)."


def _fail_on_warning(message, *args, **kwargs):
    raise AssertionError(message % args if args else message)
