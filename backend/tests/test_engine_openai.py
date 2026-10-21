"""The OpenAI-compatible loop (OpenAI and xAI): tool messages, quirk retries, fallback."""

from __future__ import annotations

import json

import httpx
import pytest
from fakes import FakeOpenAI, openai_response, openai_tool_call
from helpers import SDK_TEST_KEY, doc_id, make_engine

from rootmind import tools
from rootmind.rlm_engine import engine as engine_module
from rootmind.rlm_engine.engine import (
    GENERIC_FAILURE_ANSWER,
    ITERATION_LIMIT_PROMPT,
    SEARCH_FIRST_PROMPT,
    SYSTEM_PROMPT,
    TRUNCATED_NOTE,
    XAI_BASE_URL,
    LLMProviderError,
    LLMTimeoutError,
    provider_error,
)


class ProviderError(Exception):
    """Stands in for an SDK BadRequestError; only its message matters to the engine."""


@pytest.fixture
def openai_config(app_config):
    app_config.llm.provider = "openai"
    app_config.llm.model = "gpt-test"
    return app_config


async def test_tool_calls_then_final_answer(openai_config, indexed_repo):
    handbook = doc_id(indexed_repo, "handbook.txt")
    fake = FakeOpenAI(
        [
            openai_response(
                content="Let me look.",
                tool_calls=[openai_tool_call("c1", "search_documents", {"query": "time off"})],
            ),
            openai_response(
                tool_calls=[openai_tool_call("c2", "read_document", {"doc_id": handbook})]
            ),
            openai_response(content="21 days per year (handbook.txt).", total_tokens=40),
        ]
    )

    result = await make_engine(openai_config, indexed_repo, fake).query("How much PTO?")

    assert result["answer"] == "21 days per year (handbook.txt)."
    assert [s["file_name"] for s in result["sources"]] == ["handbook.txt"]
    assert result["tokens_used"] == 15 + 15 + 40

    first = fake.calls[0]
    assert first["model"] == "gpt-test"
    assert first["tools"] == tools.openai_tools()
    assert first["messages"] == [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "How much PTO?"},
    ]
    messages = fake.calls[2]["messages"]
    assert messages[2] == {
        "role": "assistant",
        "content": "Let me look.",
        "tool_calls": [
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "search_documents", "arguments": '{"query": "time off"}'},
            }
        ],
    }
    assert messages[3]["role"] == "tool"
    assert messages[3]["tool_call_id"] == "c1"
    assert "handbook.txt" in messages[3]["content"]
    assert messages[5]["tool_call_id"] == "c2"
    assert "<document" in messages[5]["content"]


async def test_bad_json_arguments_become_an_error_tool_message(openai_config, indexed_repo):
    fake = FakeOpenAI(
        [
            openai_response(tool_calls=[openai_tool_call("c1", "search_documents", "{bad json")]),
            openai_response(content="Sorry, retrying failed."),
        ]
    )

    result = await make_engine(openai_config, indexed_repo, fake).query("q")

    tool_message = fake.calls[1]["messages"][-1]
    assert tool_message["role"] == "tool"
    assert tool_message["content"].startswith("Error: invalid arguments for 'search_documents'")
    assert result["answer"] == "Sorry, retrying failed."
    assert result["tool_calls"] == [{"tool": "search_documents", "input": "{bad json"}]


async def test_max_completion_tokens_quirk_is_retried(openai_config, indexed_repo):
    quirk = ProviderError(
        "Unsupported parameter: 'max_tokens' is not supported with this model. "
        "Use 'max_completion_tokens' instead."
    )
    fake = FakeOpenAI([quirk, openai_response(content="ok")])

    result = await make_engine(openai_config, indexed_repo, fake).query("q")

    first, retry = fake.calls
    assert first["max_tokens"] == engine_module.MAX_OUTPUT_TOKENS
    assert "max_tokens" not in retry
    assert retry["max_completion_tokens"] == engine_module.MAX_OUTPUT_TOKENS
    assert result["answer"] == "ok"


async def test_reasoning_effort_quirk_is_retried(openai_config, indexed_repo):
    quirk = ProviderError("Function tools with reasoning_effort are not supported for this model")
    fake = FakeOpenAI([quirk, openai_response(content="ok")])

    await make_engine(openai_config, indexed_repo, fake).query("q")

    assert "reasoning_effort" not in fake.calls[0]
    assert fake.calls[1]["reasoning_effort"] == "none"


async def test_output_limit_quirk_lowers_the_cap(openai_config, indexed_repo):
    quirk = ProviderError(
        "max_tokens is too large: 16000. This model supports at most 4096 completion "
        "tokens, whereas you provided 16000."
    )
    fake = FakeOpenAI([quirk, openai_response(content="ok")])

    result = await make_engine(openai_config, indexed_repo, fake).query("q")

    assert fake.calls[0]["max_tokens"] == engine_module.MAX_OUTPUT_TOKENS
    assert fake.calls[1]["max_tokens"] == 4096
    assert result["answer"] == "ok"


async def test_repeated_quirk_is_not_retried_forever(openai_config, indexed_repo):
    quirk = ProviderError("reasoning_effort is not supported")
    fake = FakeOpenAI([quirk, quirk])

    with pytest.raises(ProviderError):
        await make_engine(openai_config, indexed_repo, fake).query("q")
    assert len(fake.calls) == 2


async def test_unrecognized_error_is_raised(openai_config, indexed_repo):
    fake = FakeOpenAI([ProviderError("invalid_api_key")])

    with pytest.raises(ProviderError, match="invalid_api_key"):
        await make_engine(openai_config, indexed_repo, fake).query("q")
    assert len(fake.calls) == 1


async def test_iteration_cap_then_fallback(openai_config, indexed_repo):
    looping = [
        openai_response(tool_calls=[openai_tool_call(f"c{i}", "get_statistics", {})])
        for i in range(2)
    ]
    fake = FakeOpenAI([*looping, openai_response(content="Answer from what was found.")])

    result = await make_engine(openai_config, indexed_repo, fake).query("q", max_iterations=2)

    final = fake.calls[-1]
    assert len(fake.calls) == 3
    assert final["tool_choice"] == "none"
    assert final["tools"] == tools.openai_tools()
    assert final["messages"][-1] == {"role": "user", "content": ITERATION_LIMIT_PROMPT}
    assert result["answer"] == "Answer from what was found."


async def test_fallback_failure_returns_generic_message(openai_config, indexed_repo):
    looping = [openai_response(tool_calls=[openai_tool_call("c", "get_statistics", {})])]
    fake = FakeOpenAI([*looping, ProviderError("server error")])

    result = await make_engine(openai_config, indexed_repo, fake).query("q", max_iterations=1)

    assert result["answer"] == GENERIC_FAILURE_ANSWER


async def test_timeout_raises_llm_timeout(openai_config, indexed_repo):
    import openai

    error = openai.APITimeoutError(request=httpx.Request("POST", "http://127.0.0.1:9"))
    fake = FakeOpenAI([error])

    with pytest.raises(LLMTimeoutError):
        await make_engine(openai_config, indexed_repo, fake).query("q")


async def test_empty_content_reports_the_finish_reason(openai_config, indexed_repo):
    """Regression: an empty final message was returned as an empty answer."""
    fake = FakeOpenAI([openai_response(content=None, finish_reason="content_filter")])
    result = await make_engine(openai_config, indexed_repo, fake).query("q")
    assert result["answer"] == (
        "The model stopped without producing an answer (stop reason: content_filter)."
    )
    assert result["stop_reasons"] == ["content_filter"]


async def test_length_finish_reason_marks_the_answer_as_cut_off(openai_config, indexed_repo):
    fake = FakeOpenAI([openai_response(content="partial", finish_reason="length")])
    result = await make_engine(openai_config, indexed_repo, fake).query("q")
    assert result["answer"] == "partial" + TRUNCATED_NOTE


async def test_condense_uses_chat_completions(openai_config, indexed_repo):
    fake = FakeOpenAI(
        [
            openai_response(content="What is the warranty of the Trailhead tent?"),
            openai_response(content="5 years"),
        ]
    )

    result = await make_engine(openai_config, indexed_repo, fake).query(
        "And its warranty?", history=[{"role": "user", "text": "Trailhead tent weight?"}]
    )

    assert "tools" not in fake.calls[0]
    assert fake.calls[1]["messages"][1]["content"] == "What is the warranty of the Trailhead tent?"
    assert result["answer"] == "5 years"


def test_generate_title(openai_config, indexed_repo):
    fake = FakeOpenAI([openai_response(content="Paid time off")])
    assert make_engine(openai_config, indexed_repo, fake).generate_title("PTO?") == "Paid time off"


@pytest.mark.parametrize(
    ("provider", "expected"),
    [
        ("openai", {"api_key": SDK_TEST_KEY, "timeout": 30.0, "max_retries": 1}),
        (
            "xai",
            {"api_key": SDK_TEST_KEY, "base_url": XAI_BASE_URL, "timeout": 30.0, "max_retries": 1},
        ),
    ],
)
def test_sdk_client_construction(app_config, indexed_repo, monkeypatch, provider, expected):
    import openai

    constructed = []

    class RecordingOpenAI:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    monkeypatch.setattr(openai, "OpenAI", RecordingOpenAI)
    app_config.llm.provider = provider
    app_config.llm.api_key = SDK_TEST_KEY
    app_config.llm.timeout_seconds = 30

    engine = engine_module.KnowledgeBaseEngine(indexed_repo, app_config)

    assert isinstance(engine._get_client(), RecordingOpenAI)
    assert constructed == [expected]


def test_tool_arguments_are_valid_json_schema_objects():
    for tool in tools.openai_tools():
        json.dumps(tool)  # serializable as sent over the wire
        assert tool["type"] == "function"
        assert tool["function"]["parameters"]["type"] == "object"


# --- Ollama ---------------------------------------------------------------------------------


@pytest.fixture
def ollama_config(app_config):
    app_config.llm.provider = "ollama"
    app_config.llm.model = "qwen3:8b"
    app_config.llm.api_key = None
    return app_config


def test_ollama_client_needs_no_key_and_uses_the_v1_endpoint(
    ollama_config, indexed_repo, monkeypatch
):
    import openai

    constructed = []

    class RecordingOpenAI:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    monkeypatch.setattr(openai, "OpenAI", RecordingOpenAI)
    ollama_config.llm.base_url = "http://nas:11434/"
    ollama_config.llm.timeout_seconds = 30

    engine = engine_module.KnowledgeBaseEngine(indexed_repo, ollama_config)

    assert isinstance(engine._get_client(), RecordingOpenAI)
    assert constructed == [
        {
            "api_key": "ollama",  # pragma: allowlist secret (placeholder, not a key)
            "base_url": "http://nas:11434/v1",
            "timeout": 30.0,
            "max_retries": 1,
        }
    ]


async def test_ollama_does_not_force_a_tool_call(ollama_config, indexed_repo):
    fake = FakeOpenAI(
        [
            openai_response(
                tool_calls=[openai_tool_call("c1", "search_documents", {"query": "time off"})]
            ),
            openai_response(content="21 days."),
        ]
    )

    result = await make_engine(ollama_config, indexed_repo, fake).query("How much PTO?")

    assert result["answer"] == "21 days."
    assert all("tool_choice" not in call for call in fake.calls)


async def test_ollama_answer_without_a_search_is_retried_once(ollama_config, indexed_repo):
    fake = FakeOpenAI(
        [
            openai_response(content="From memory: 20 days."),
            openai_response(
                tool_calls=[openai_tool_call("c1", "search_documents", {"query": "time off"})]
            ),
            openai_response(content="21 days (handbook.txt)."),
        ]
    )

    result = await make_engine(ollama_config, indexed_repo, fake).query("How much PTO?")

    assert result["answer"] == "21 days (handbook.txt)."
    retry = fake.calls[1]["messages"]
    assert retry[-2] == {"role": "assistant", "content": "From memory: 20 days."}
    assert retry[-1] == {"role": "user", "content": SEARCH_FIRST_PROMPT}


async def test_ollama_second_answer_without_a_search_is_accepted(ollama_config, indexed_repo):
    fake = FakeOpenAI([openai_response(content="Hi."), openai_response(content="Still no tool.")])

    result = await make_engine(ollama_config, indexed_repo, fake).query("Hello?")

    assert result["answer"] == "Still no tool."
    assert len(fake.calls) == 2


def test_openai_still_forces_the_first_tool_call(openai_config, indexed_repo):
    import asyncio

    fake = FakeOpenAI([openai_response(content="No tool.")])

    asyncio.run(make_engine(openai_config, indexed_repo, fake).query("Q?"))

    assert fake.calls[0]["tool_choice"] == "required"
    assert len(fake.calls) == 1


def _api_error(status, message, with_status=True):
    import openai

    request = httpx.Request("POST", "http://localhost:11434/v1/chat/completions")
    if not with_status:
        return openai.APIConnectionError(request=request)
    return openai.APIStatusError(
        message, response=httpx.Response(status, request=request), body=None
    )


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (_api_error(0, "", with_status=False), "Could not reach Ollama"),
        (_api_error(404, "model 'x' not found"), "ollama pull"),
        (_api_error(400, "registry.ollama.ai/library/x does not support tools"), "tool-capable"),
        (_api_error(500, "boom"), "Ollama returned HTTP 500"),
    ],
)
def test_ollama_failures_are_readable(error, expected):
    mapped = provider_error(error, "ollama")

    assert isinstance(mapped, LLMProviderError)
    assert expected in str(mapped)
