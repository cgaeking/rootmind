"""POST /query/stream: the agent's steps as server-sent events, the result, errors, cancelling."""

from __future__ import annotations

import asyncio
import json

import pytest
from fakes import FakeAnthropic, anthropic_response, text, tool_use
from helpers import make_engine, sync_app_state

from rootmind.api import routes
from rootmind.api.app import app_state
from rootmind.rlm_engine.engine import DEFAULT_MAX_ITERATIONS, LLMTimeoutError
from rootmind.tracing.progress import ProgressTracer


def _parse(body: str) -> list[tuple[str, dict]]:
    """``(event, data)`` pairs of an SSE body; comment lines (keepalives) are skipped."""
    events = []
    for block in body.split("\n\n"):
        lines = [line for line in block.splitlines() if line and not line.startswith(":")]
        if lines:
            events.append(
                (lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: ")))
            )
    return events


def _steps(events):
    return [data for name, data in events if name == "step"]


SEARCH_THEN_ANSWER = [
    anthropic_response(tool_use("t1", "search_documents", {"query": "paid time off"})),
    anthropic_response(text("21 days per year (handbook.txt).")),
]


@pytest.fixture
def use_fake(api, monkeypatch):
    """Install an engine with a scripted fake client; returns a factory taking the script."""

    def install(script, sleep: float = 0.0):
        fake = FakeAnthropic(script, sleep=sleep)
        engine = make_engine(app_state.config, app_state.db, fake)
        monkeypatch.setattr(app_state, "rlm_engine", engine)
        return fake

    return install


async def test_progress_reports_each_call_as_it_starts_and_ends(app_config, indexed_repo):
    steps: list[dict] = []
    engine = make_engine(app_config, indexed_repo, FakeAnthropic(SEARCH_THEN_ANSWER))

    result = await engine.query("How much paid time off?", tracer=ProgressTracer(steps.append))

    assert result["answer"] == "21 days per year (handbook.txt)."
    assert [(s["type"], s["step"]) for s in steps] == [
        ("model_start", 1),
        ("model_end", 1),
        ("tool_start", 2),
        ("tool_end", 2),
        ("model_start", 3),
        ("model_end", 3),
    ]
    assert steps[0] == {"type": "model_start", "step": 1, "purpose": "loop", "iteration": 1}
    assert steps[1]["tool_requests"] == 1
    assert steps[1]["stop_reason"] == "tool_use"
    assert steps[2]["name"] == "search_documents"
    assert steps[2]["arguments"] == {"query": "paid time off"}
    search = steps[3]
    assert search["is_error"] is False
    assert search["hits"] >= 1
    assert "handbook.txt" in search["files"]
    assert search["duration_ms"] >= 0
    # Steps are small: no document text, only counts and names.
    assert all("result" not in s for s in steps)
    assert steps[5]["tool_requests"] == 0


def test_stream_sends_start_steps_and_the_result(api, use_fake):
    sync_app_state()
    fake = use_fake(SEARCH_THEN_ANSWER)

    response = api.post("/query/stream", json={"question": "How much paid time off?"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    events = _parse(response.text)
    assert events[0] == (
        "start",
        {
            "max_iterations": DEFAULT_MAX_ITERATIONS,
            "provider": app_state.config.llm.provider,
            "model": app_state.config.llm.model,
        },
    )
    assert [s["type"] for s in _steps(events)] == [
        "model_start",
        "model_end",
        "tool_start",
        "tool_end",
        "model_start",
        "model_end",
    ]
    name, result = events[-1]
    assert name == "result"
    # The same body POST /query returns.
    assert result["answer"] == "21 days per year (handbook.txt)."
    assert [s["file_name"] for s in result["sources"]] == ["handbook.txt"]
    assert result["tool_calls"] == [
        {"tool": "search_documents", "input": {"query": "paid time off"}}
    ]
    assert result["stop_reasons"] == ["tool_use", "end_turn"]
    assert len(fake.calls) == 2


def test_stream_reports_an_error_as_its_last_event(api, use_fake):
    use_fake([LLMTimeoutError("LLM request timed out after 120s")])

    events = _parse(api.post("/query/stream", json={"question": "Anything?"}).text)

    assert events[-1] == ("error", {"status": 504, "detail": "LLM request timed out"})
    assert [s["type"] for s in _steps(events)] == ["model_start", "model_end"]
    assert "error" in _steps(events)[1]


def test_stream_validates_the_request_like_query(api):
    assert api.post("/query/stream", json={"question": ""}).status_code == 422


async def test_a_long_step_sends_keepalives():
    async def run(emit):
        emit({"type": "tool_start", "step": 1})
        await asyncio.sleep(0.25)
        return {"answer": "done", "sources": [], "tokens_used": 0}

    chunks = [chunk async for chunk in routes.query_events(run, {}, heartbeat=0.05)]

    assert chunks.count(": keepalive\n\n") >= 2
    assert _parse("".join(chunks))[-1][0] == "result"


async def test_closing_the_stream_cancels_the_question(app_config, indexed_repo):
    """The Stop button closes the stream: no model or tool call starts after that."""
    fake = FakeAnthropic(SEARCH_THEN_ANSWER, sleep=0.2)
    engine = make_engine(app_config, indexed_repo, fake)

    def run(emit):
        return engine.query("How much paid time off?", tracer=ProgressTracer(emit))

    stream = routes.query_events(run, {}, heartbeat=0.05)
    first = [await anext(stream), await anext(stream)]
    assert "model_start" in first[1]  # the first model call is under way
    await stream.aclose()

    await asyncio.sleep(0.6)  # long enough for the in-flight call and a next one
    assert len(fake.calls) == 1
