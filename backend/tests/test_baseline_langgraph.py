"""Smoke test of the LangGraph baseline (evals/baselines/langgraph, #48), with a fake model.

Runs only in the baseline's own environment (``uv run --project evals/baselines/langgraph
pytest tests/test_baseline_langgraph.py``); the backend environment has no LangChain,
so the normal test run skips this module. No network: the chat model is a fake.
"""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

import pytest

pytest.importorskip("langgraph")
pytest.importorskip("langchain")

from langchain_core.language_models.fake_chat_models import (
    GenericFakeChatModel,
)
from langchain_core.messages import AIMessage, HumanMessage

from evals.baselines.langgraph import agent as baseline
from rootmind import tools
from scripts import run_evals

BACKEND = Path(__file__).resolve().parents[1]


class FakeToolModel(GenericFakeChatModel):
    """Replays scripted replies; accepts tools like a real chat model and records prompts."""

    seen: list = []

    def bind_tools(self, tools, **kwargs):
        self.seen.append(("tools", [t.name for t in tools]))
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):
        self.seen.append(("messages", list(messages)))
        return super()._generate(messages, stop=stop, run_manager=run_manager, **kwargs)


def _usage(total: int) -> dict:
    return {"input_tokens": total - 10, "output_tokens": 10, "total_tokens": total}


def _search(query: str, call_id: str = "call_1") -> AIMessage:
    return AIMessage(
        content="",
        tool_calls=[{"name": "search_documents", "args": {"query": query}, "id": call_id}],
        usage_metadata=_usage(1000),
        response_metadata={"finish_reason": "tool_calls"},
    )


def _answer(text: str) -> AIMessage:
    return AIMessage(
        content=text, usage_metadata=_usage(1200), response_metadata={"finish_reason": "stop"}
    )


def test_eval_run_with_the_baseline_uses_the_same_tools_and_scoring(tmp_path, monkeypatch):
    model = FakeToolModel(
        messages=iter(
            [
                _search("INV-2026-0142"),
                _answer("The total due is EUR 1,179.36 (invoice_INV-2026-0142.pdf)."),
            ]
        )
    )
    model.seen = []
    monkeypatch.setattr(baseline, "build_chat_model", lambda config: model)
    monkeypatch.setattr(run_evals, "ollama_version", lambda base_url: None)
    out, traces = tmp_path / "out", tmp_path / "traces"

    code = run_evals.main(
        [
            "--mode",
            "live",
            "--agent",
            "langgraph",
            "--provider",
            "ollama",
            "--model",
            "fake",
            "--cases",
            "c01",
            "--runs",
            "1",
            "--out",
            str(out),
            "--trace",
            str(traces),
        ]
    )

    assert code == 0
    [path] = out.glob("*-live-langgraph-fake.json")
    data = json.loads(path.read_text(encoding="utf-8"))
    assert data["agent"] == "langgraph"
    assert data["versions"]["langgraph"] and data["versions"]["langchain"]
    [run] = data["cases"][0]["runs"]
    assert run["passed"] is True
    assert run["tool_path"] == "search_documents(INV-2026-0142)"
    assert run["sources"] == ["invoice_INV-2026-0142.pdf"]
    assert run["tokens"] == 2200
    assert run["stop_reasons"] == ["tool_calls", "stop"]
    assert run["invalid_tool_calls"] == 0

    # The model saw RootMind's whole tool registry and system prompt, and the tool
    # result is exactly what rootmind.tools returns for the same call.
    assert ("tools", list(tools.TOOL_NAMES)) in model.seen
    prompts = [m for kind, m in model.seen if kind == "messages"]
    assert prompts[0][0].content == baseline.SYSTEM_PROMPT
    assert isinstance(prompts[0][1], HumanMessage)
    tool_message = prompts[1][-1]
    assert "invoice_INV-2026-0142.pdf" in tool_message.content

    # The local trace has the model calls and the tool call of the run.
    records = [
        json.loads(line)
        for line in next(traces.glob("*.jsonl")).read_text(encoding="utf-8").splitlines()
    ]
    kinds = [r["type"] for r in records]
    assert kinds.count("model_call") == 2 and kinds.count("tool_call") == 1
    assert run["trace_id"] in {r["trace_id"] for r in records if r["type"] == "answer"}


@pytest.fixture
def index(tmp_path):
    config, repo = run_evals.build_index(run_evals.DEFAULT_CORPUS, tmp_path)
    yield config, repo
    repo.engine.dispose()


async def test_recursion_limit_ends_the_run_without_an_answer(index):
    config, repo = index
    replies = iter([_search(f"query {i}", f"call_{i}") for i in range(50)])
    agent = baseline.LangGraphAgent(repo, config, model=FakeToolModel(messages=replies))

    result = await agent.query("What is the stock ticker?")

    assert result["answer"] == baseline.RECURSION_LIMIT_ANSWER
    assert len(result["tool_calls"]) == baseline.MAX_MODEL_CALLS
    assert result["stop_reasons"][-1] == "recursion_limit"
    assert len(result["stop_reasons"]) == baseline.MAX_MODEL_CALLS + 1


async def test_history_goes_into_the_conversation(index):
    config, repo = index
    model = FakeToolModel(messages=iter([_search("Trailhead warranty"), _answer("5 years")]))
    model.seen = []
    agent = baseline.LangGraphAgent(repo, config, model=model)
    history = [
        {"role": "user", "text": "How much does the Trailhead 2P tent weigh?"},
        {"role": "assistant", "text": "2.1 kg."},
    ]

    result = await agent.query("And its warranty?", history=history)

    first_prompt = next(m for kind, m in model.seen if kind == "messages")
    assert [type(m).__name__ for m in first_prompt] == [
        "SystemMessage",
        "HumanMessage",
        "AIMessage",
        "HumanMessage",
    ]
    assert result["answer"] == "5 years"


def test_baseline_pins_the_backend_lock_versions():
    """The baseline's environment uses the backend's versions of every shared package."""

    def versions(lock: Path) -> dict[str, str]:
        data = tomllib.loads(lock.read_text(encoding="utf-8"))
        return {p["name"]: p["version"] for p in data["package"]}

    backend = versions(BACKEND / "uv.lock")
    base = versions(BACKEND / "evals" / "baselines" / "langgraph" / "uv.lock")
    drift = {
        name: (backend[name], base[name])
        for name in set(backend) & set(base)
        if backend[name] != base[name]
    }
    assert not drift, f"regenerate the baseline's constraint-dependencies: {drift}"
