"""The Anthropic agent loop with a scripted fake client: stop handling, guardrails, sources."""

from __future__ import annotations

import asyncio
import re

import httpx
import pytest
from fakes import FakeAnthropic, anthropic_response, text, tool_use
from helpers import SDK_TEST_KEY, doc_id, make_engine

from rootmind import tools
from rootmind.rlm_engine import engine as engine_module
from rootmind.rlm_engine.engine import (
    EMPTY_ANSWER_FALLBACK,
    EMPTY_ANSWER_PROMPT,
    GENERIC_FAILURE_ANSWER,
    ITERATION_LIMIT_PROMPT,
    MAX_TOOL_CALLS_PER_TURN,
    SYSTEM_PROMPT,
    TRUNCATED_NOTE,
    LLMConfigError,
    LLMProviderError,
    LLMTimeoutError,
)


def _all_messages(fake):
    return [m for call in fake.calls for m in call["messages"]]


def _assert_no_empty_user_turn(fake):
    for message in _all_messages(fake):
        assert message["content"] not in ("", [], None), message


def _assert_roles_alternate(messages):
    roles = [m["role"] for m in messages]
    assert all(a != b for a, b in zip(roles, roles[1:], strict=False)), roles


async def test_search_read_answer(app_config, indexed_repo):
    invoice = doc_id(indexed_repo, "invoice.pdf")
    page2 = indexed_repo.get_document_content(invoice)["content"].index("[Page 2]")
    fake = FakeAnthropic(
        [
            anthropic_response(tool_use("t1", "search_documents", {"query": "total due"})),
            anthropic_response(
                tool_use("t2", "read_document", {"doc_id": invoice, "start_char": page2})
            ),
            anthropic_response(
                text("The total is 1,179.36 "), text("(invoice.pdf, Page 2)."), output_tokens=7
            ),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query("What is the total due?")

    assert result["answer"] == "The total is 1,179.36 (invoice.pdf, Page 2)."
    assert result["sources"] == [
        {
            "id": invoice,
            "file_name": "invoice.pdf",
            "file_path": "invoice.pdf",
            "page": "Page 2",
        }
    ]
    assert result["tokens_used"] == 15 + 15 + 17
    assert [c["tool"] for c in result["tool_calls"]] == ["search_documents", "read_document"]
    assert result["stop_reasons"] == ["tool_use", "tool_use", "end_turn"]

    first = fake.calls[0]
    assert first["system"] == SYSTEM_PROMPT
    assert first["tools"] == tools.TOOLS
    assert first["messages"] == [{"role": "user", "content": "What is the total due?"}]
    # The assistant turn goes back verbatim, followed by the matching tool_result.
    third = fake.calls[2]["messages"]
    _assert_roles_alternate(third)
    assert third[1]["content"][0].id == "t1"
    tool_result = third[2]["content"][0]
    assert tool_result["type"] == "tool_result"
    assert tool_result["tool_use_id"] == "t1"
    assert "is_error" not in tool_result
    assert "<snippet>" in tool_result["content"]
    assert f'<document id="{invoice}"' in third[4]["content"][0]["content"]


async def test_source_cites_the_hit_inside_the_suggested_read_window(app_config, indexed_repo):
    """Regression: following the suggested window (it starts before the hit) lost the page."""
    invoice = doc_id(indexed_repo, "invoice.pdf")

    def read_suggested_window(kwargs):
        hit_list = kwargs["messages"][-1]["content"][0]["content"]
        start, end = (int(x) for x in re.findall(r"start_char=(\d+), end_char=(\d+)", hit_list)[0])
        assert start == 0  # the hit is less than 3,000 chars into the document
        args = {"doc_id": invoice, "start_char": start, "end_char": end}
        return anthropic_response(tool_use("t2", "read_document", args))

    fake = FakeAnthropic(
        [
            anthropic_response(tool_use("t1", "search_documents", {"query": "Total due"})),
            read_suggested_window,
            anthropic_response(text("1,179.36")),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query("Total due?")

    assert result["sources"][0]["page"] == "Page 2"


async def test_multi_word_query_cites_the_page_of_the_match(app_config, indexed_repo):
    """Regression (#55): a query that is not a literal phrase lost the page of the source."""
    invoice = doc_id(indexed_repo, "invoice.pdf")

    def read_suggested_window(kwargs):
        hit_list = kwargs["messages"][-1]["content"][0]["content"]
        assert "Location: Page 2" in hit_list
        start, end = (int(x) for x in re.findall(r"start_char=(\d+), end_char=(\d+)", hit_list)[0])
        args = {"doc_id": invoice, "start_char": start, "end_char": end}
        return anthropic_response(tool_use("t2", "read_document", args))

    fake = FakeAnthropic(
        [
            anthropic_response(
                tool_use("t1", "search_documents", {"query": "invoice INV-7 due date"})
            ),
            read_suggested_window,
            anthropic_response(text("2026-04-30")),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query("When is INV-7 due?")

    assert result["sources"][0]["page"] == "Page 2"


async def test_read_from_the_start_without_a_hit_has_no_location(app_config, indexed_repo):
    invoice = doc_id(indexed_repo, "invoice.pdf")
    fake = FakeAnthropic(
        [
            anthropic_response(tool_use("t", "read_document", {"doc_id": invoice})),
            anthropic_response(text("It is an invoice.")),
        ]
    )
    result = await make_engine(app_config, indexed_repo, fake).query("What is this?")
    assert result["sources"] == [
        {"id": invoice, "file_name": "invoice.pdf", "file_path": "invoice.pdf"}
    ]


async def test_several_tool_calls_in_one_turn(app_config, indexed_repo):
    fake = FakeAnthropic(
        [
            anthropic_response(
                tool_use("a", "get_statistics", {}),
                tool_use("b", "list_documents", {"file_type": "pdf"}),
            ),
            anthropic_response(text("One PDF.")),
        ]
    )
    result = await make_engine(app_config, indexed_repo, fake).query("How many PDFs?")

    results = fake.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in results] == ["a", "b"]
    assert [s["file_name"] for s in result["sources"]] == ["invoice.pdf"]


async def test_iteration_cap_then_fallback(app_config, indexed_repo):
    looping = [anthropic_response(tool_use(f"t{i}", "get_statistics", {})) for i in range(3)]
    fake = FakeAnthropic([*looping, anthropic_response(text("Best answer so far."))])

    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=3)

    assert len(fake.calls) == 4
    final = fake.calls[-1]
    assert final["tool_choice"] == {"type": "none"}
    assert final["tools"] == tools.TOOLS  # history holds tool blocks, so tools stay declared
    assert final["max_tokens"] == engine_module.FALLBACK_MAX_TOKENS
    # The limit prompt is merged into the trailing tool-result turn, not a second user turn.
    _assert_roles_alternate(final["messages"])
    assert final["messages"][-1]["content"][-1] == {"type": "text", "text": ITERATION_LIMIT_PROMPT}
    # First call forces a tool (search-first guarantee); the loop's own
    # middle iterations stay free; the trailing fallback call forces none.
    assert fake.calls[0]["tool_choice"] == {"type": "any"}
    assert all("tool_choice" not in call for call in fake.calls[1:-1])
    assert result["answer"] == "Best answer so far."
    assert len(result["tool_calls"]) == 3
    _assert_no_empty_user_turn(fake)


async def test_fallback_failure_returns_generic_message(app_config, indexed_repo):
    looping = [anthropic_response(tool_use(f"t{i}", "get_statistics", {})) for i in range(2)]
    fake = FakeAnthropic([*looping, RuntimeError("provider exploded")])

    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=2)

    assert result["answer"] == GENERIC_FAILURE_ANSWER


async def test_fallback_without_text_returns_generic_message(app_config, indexed_repo):
    fake = FakeAnthropic(
        [anthropic_response(tool_use("t", "get_statistics", {})), anthropic_response()]
    )
    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=1)
    assert result["answer"] == GENERIC_FAILURE_ANSWER


@pytest.mark.parametrize(
    ("call", "error_text"),
    [
        (tool_use("t1", "drop_database", {}), "unknown tool 'drop_database'"),
        (tool_use("t1", "read_document", {}), "missing required argument 'doc_id'"),
        (tool_use("t1", "search_documents", {"query": "x", "limit": "many"}), "must be an integer"),
    ],
)
async def test_tool_errors_are_fed_back_and_the_loop_continues(
    app_config, indexed_repo, call, error_text
):
    fake = FakeAnthropic([anthropic_response(call), anthropic_response(text("Recovered."))])

    result = await make_engine(app_config, indexed_repo, fake).query("q")

    tool_result = fake.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert error_text in tool_result["content"]
    assert result["answer"] == "Recovered."


async def test_repository_failure_inside_a_tool_does_not_crash(
    app_config, indexed_repo, monkeypatch
):
    def broken(*args, **kwargs):
        raise RuntimeError("disk I/O error")

    monkeypatch.setattr(indexed_repo, "search_fulltext", broken)
    fake = FakeAnthropic(
        [
            anthropic_response(tool_use("t1", "search_documents", {"query": "tent"})),
            anthropic_response(text("Search is unavailable.")),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query("q")

    tool_result = fake.calls[1]["messages"][-1]["content"][0]
    assert tool_result["is_error"] is True
    assert "disk I/O error" in tool_result["content"]
    assert result["answer"] == "Search is unavailable."


@pytest.mark.parametrize(
    ("stop_reason", "answer"),
    [
        ("max_tokens", "partial" + TRUNCATED_NOTE),  # says that it was cut off
        ("stop_sequence", "partial"),
        ("refusal", "partial"),
        ("pause_turn", "partial"),
    ],
)
async def test_non_tool_stop_is_final(app_config, indexed_repo, stop_reason, answer):
    fake = FakeAnthropic([anthropic_response(text("partial"), stop_reason=stop_reason)])

    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=5)

    assert len(fake.calls) == 1
    assert result["answer"] == answer
    assert result["stop_reasons"] == [stop_reason]
    _assert_no_empty_user_turn(fake)


async def test_max_tokens_stop_is_final_even_with_a_tool_block(app_config, indexed_repo):
    """A truncated turn may contain an incomplete tool_use block; it must not be executed."""
    truncated = anthropic_response(
        text("Let me search"), tool_use("t1", "search_documents", {}), stop_reason="max_tokens"
    )
    fake = FakeAnthropic([truncated])

    result = await make_engine(app_config, indexed_repo, fake).query("q")

    assert len(fake.calls) == 1
    assert result["answer"] == "Let me search" + TRUNCATED_NOTE
    assert result["tool_calls"] == []


async def test_stop_without_text_reports_the_stop_reason(app_config, indexed_repo):
    fake = FakeAnthropic([anthropic_response(stop_reason="refusal")])
    result = await make_engine(app_config, indexed_repo, fake).query("q")
    assert result["answer"] == (
        "The model stopped without producing an answer (stop reason: refusal)."
    )


def _search():
    return anthropic_response(tool_use("t1", "search_documents", {"query": "total due"}))


@pytest.mark.parametrize("empty", [(), (text(""),), (text("  \n"),)])
async def test_empty_answer_after_a_search_is_retried_once(app_config, indexed_repo, empty):
    """Regression (#47, case c04): an empty end_turn reply was returned as the answer."""
    fake = FakeAnthropic(
        [_search(), anthropic_response(*empty), anthropic_response(text("1,179.36 (invoice.pdf)."))]
    )

    result = await make_engine(app_config, indexed_repo, fake).query("Total due?")

    assert result["answer"] == "1,179.36 (invoice.pdf)."
    assert len(fake.calls) == 3
    assert result["stop_reasons"] == ["tool_use", "end_turn", "end_turn"]
    assert result["tokens_used"] == 3 * 15
    retry = fake.calls[2]
    assert "tool_choice" not in retry  # an ordinary iteration: tools stay available
    # The empty turn is not sent back; the follow-up joins the tool-result turn.
    assert len(retry["messages"]) == len(fake.calls[1]["messages"])
    assert retry["messages"][-1]["content"][-1] == {"type": "text", "text": EMPTY_ANSWER_PROMPT}
    _assert_roles_alternate(retry["messages"])
    _assert_no_empty_user_turn(fake)


async def test_second_empty_answer_returns_the_fallback(app_config, indexed_repo):
    fake = FakeAnthropic([_search(), anthropic_response(), anthropic_response(text(" "))])

    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=5)

    assert result["answer"] == EMPTY_ANSWER_FALLBACK
    assert len(fake.calls) == 3
    assert result["stop_reasons"] == ["tool_use", "end_turn", "end_turn"]


async def test_empty_answer_is_retried_only_once_per_question(app_config, indexed_repo):
    """The retry may search again; a later empty reply still gets the fallback."""
    fake = FakeAnthropic(
        [
            _search(),
            anthropic_response(),
            anthropic_response(tool_use("t2", "get_statistics", {})),
            anthropic_response(),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=5)

    assert result["answer"] == EMPTY_ANSWER_FALLBACK
    assert len(fake.calls) == 4
    assert [c["tool"] for c in result["tool_calls"]] == ["search_documents", "get_statistics"]


async def test_empty_answer_retry_counts_against_the_iteration_cap(app_config, indexed_repo):
    fake = FakeAnthropic(
        [
            _search(),
            anthropic_response(),
            anthropic_response(tool_use("t2", "get_statistics", {})),  # the retry: 3rd iteration
            anthropic_response(text("Best answer so far.")),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=3)

    assert len(fake.calls) == 4
    final = fake.calls[-1]
    assert final["tool_choice"] == {"type": "none"}
    assert final["messages"][-1]["content"][-1] == {"type": "text", "text": ITERATION_LIMIT_PROMPT}
    assert result["answer"] == "Best answer so far."


async def test_empty_answer_at_the_iteration_cap_uses_the_final_call(app_config, indexed_repo):
    fake = FakeAnthropic([_search(), anthropic_response(), anthropic_response(text("Found it."))])

    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=2)

    assert result["answer"] == "Found it."
    assert len(fake.calls) == 3
    final = fake.calls[-1]
    assert final["tool_choice"] == {"type": "none"}
    # Only the limit prompt asks for the answer here, not both prompts.
    assert final["messages"][-1]["content"][-1] == {"type": "text", "text": ITERATION_LIMIT_PROMPT}
    assert EMPTY_ANSWER_PROMPT not in str(final["messages"])
    _assert_roles_alternate(final["messages"])


async def test_empty_answer_at_the_cap_and_after_it_returns_the_fallback(app_config, indexed_repo):
    fake = FakeAnthropic([_search(), anthropic_response(), anthropic_response()])

    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=2)

    assert result["answer"] == EMPTY_ANSWER_FALLBACK
    assert len(fake.calls) == 3


async def test_non_empty_answer_is_not_retried(app_config, indexed_repo):
    fake = FakeAnthropic([_search(), anthropic_response(text("1,179.36."))])

    result = await make_engine(app_config, indexed_repo, fake).query("q", max_iterations=5)

    assert result["answer"] == "1,179.36."
    assert len(fake.calls) == 2
    assert EMPTY_ANSWER_PROMPT not in str(fake.calls)


async def test_timeout_raises_llm_timeout(app_config, indexed_repo):
    import anthropic

    error = anthropic.APITimeoutError(request=httpx.Request("POST", "http://127.0.0.1:9"))
    fake = FakeAnthropic([error])

    with pytest.raises(LLMTimeoutError, match="timed out after 120s"):
        await make_engine(app_config, indexed_repo, fake).query("q")


async def test_stdlib_timeout_raises_llm_timeout(app_config, indexed_repo):
    fake = FakeAnthropic(
        [anthropic_response(tool_use("t", "get_statistics", {})), TimeoutError("read timed out")]
    )
    with pytest.raises(LLMTimeoutError):
        await make_engine(app_config, indexed_repo, fake).query("q")


async def test_other_provider_errors_propagate(app_config, indexed_repo):
    fake = FakeAnthropic([ValueError("invalid request")])
    with pytest.raises(ValueError, match="invalid request"):
        await make_engine(app_config, indexed_repo, fake).query("q")


async def test_sdk_errors_become_provider_errors(app_config, indexed_repo):
    import anthropic

    request = httpx.Request("POST", "http://127.0.0.1:9/v1/messages")
    overloaded = anthropic.InternalServerError(
        "overloaded", response=httpx.Response(529, request=request), body=None
    )
    fake = FakeAnthropic([overloaded])

    with pytest.raises(LLMProviderError, match=r"returned HTTP 529$"):
        await make_engine(app_config, indexed_repo, fake).query("q")


async def test_missing_api_key_fails_before_any_work(app_config, indexed_repo):
    app_config.llm.api_key = None
    engine = engine_module.KnowledgeBaseEngine(indexed_repo, app_config)

    with pytest.raises(LLMConfigError, match="ANTHROPIC_API_KEY"):
        await engine.query("q", history=[{"role": "user", "text": "earlier"}])
    with pytest.raises(LLMConfigError):
        engine.generate_title("q")


async def test_tool_calls_per_turn_are_capped(app_config, indexed_repo):
    calls = [tool_use(f"t{i}", "get_statistics", {}) for i in range(MAX_TOOL_CALLS_PER_TURN + 3)]
    fake = FakeAnthropic([anthropic_response(*calls), anthropic_response(text("done"))])

    result = await make_engine(app_config, indexed_repo, fake).query("q")

    tool_results = fake.calls[1]["messages"][-1]["content"]
    assert [r["tool_use_id"] for r in tool_results] == [c.id for c in calls]  # one per call
    ran, refused = tool_results[:MAX_TOOL_CALLS_PER_TURN], tool_results[MAX_TOOL_CALLS_PER_TURN:]
    assert all("is_error" not in r for r in ran)
    assert all(r["is_error"] and "not run" in r["content"] for r in refused)
    assert len(result["tool_calls"]) == MAX_TOOL_CALLS_PER_TURN


async def test_tool_results_per_question_are_capped(app_config, indexed_repo, monkeypatch):
    monkeypatch.setattr(engine_module, "MAX_TOOL_RESULT_CHARS", 1_000)
    handbook = doc_id(indexed_repo, "handbook.txt")
    reads = [
        anthropic_response(tool_use(f"r{i}", "read_document", {"doc_id": handbook}))
        for i in range(3)
    ]
    big = "x" * 1_200
    monkeypatch.setattr(
        engine_module.tools,
        "execute",
        lambda name, args, repo, ctx: engine_module.tools.ToolResult(big),
    )
    fake = FakeAnthropic([*reads, anthropic_response(text("done"))])

    await make_engine(app_config, indexed_repo, fake).query("q")

    first, second = (fake.calls[i]["messages"][-1]["content"][0] for i in (1, 2))
    assert first["content"] == big
    assert second["is_error"] is True
    assert "reached the limit of 1,000 characters" in second["content"]


async def test_condense_uses_clipped_history_and_ignores_a_cut_off_rewrite(
    app_config, indexed_repo
):
    long_answer = "A" * (engine_module.MAX_HISTORY_TURN_CHARS + 500)
    fake = FakeAnthropic(
        [
            anthropic_response(text("What is the half-written"), stop_reason="max_tokens"),
            anthropic_response(text("ok")),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query(
        "And wholesale?", history=[{"role": "assistant", "text": long_answer}]
    )

    prompt = fake.calls[0]["messages"][0]["content"]
    assert "A" * engine_module.MAX_HISTORY_TURN_CHARS + "…" in prompt
    assert "A" * (engine_module.MAX_HISTORY_TURN_CHARS + 1) not in prompt
    assert fake.calls[1]["messages"][0]["content"] == "And wholesale?"
    assert result["answer"] == "ok"


def test_timeout_passed_to_sdk(app_config, indexed_repo, monkeypatch):
    import anthropic

    constructed = []

    class RecordingAnthropic:
        def __init__(self, **kwargs):
            constructed.append(kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", RecordingAnthropic)
    app_config.llm.api_key = SDK_TEST_KEY
    app_config.llm.timeout_seconds = 7.5

    engine = engine_module.KnowledgeBaseEngine(indexed_repo, app_config)
    client = engine._get_client()

    assert isinstance(client, RecordingAnthropic)
    assert constructed == [{"api_key": SDK_TEST_KEY, "timeout": 7.5, "max_retries": 1}]
    assert engine._get_client() is client  # created once, then cached


async def test_concurrent_queries_keep_sources_separate(app_config, indexed_repo):
    ids = {
        "invoice": doc_id(indexed_repo, "invoice.pdf"),
        "handbook": doc_id(indexed_repo, "handbook.txt"),
    }

    def route(kwargs):
        question = kwargs["messages"][0]["content"]
        target = ids["invoice"] if "invoice" in question else ids["handbook"]
        if len(kwargs["messages"]) == 1:
            return anthropic_response(tool_use("t", "read_document", {"doc_id": target}))
        return anthropic_response(text("done"))

    # The sleep inside the fake (a worker thread) forces the two loops to interleave.
    fake = FakeAnthropic([route] * 4, sleep=0.05)
    engine = make_engine(app_config, indexed_repo, fake)

    first, second = await asyncio.gather(
        engine.query("about the invoice"), engine.query("about the handbook")
    )

    assert [s["file_name"] for s in first["sources"]] == ["invoice.pdf"]
    assert [s["file_name"] for s in second["sources"]] == ["handbook.txt"]
    assert len(first["tool_calls"]) == len(second["tool_calls"]) == 1


async def test_condense_with_history(app_config, indexed_repo):
    history = [
        {"role": "user", "text": "How much does the Trailhead tent cost?"},
        {"role": "assistant", "text": "249.00 at retail."},
    ]
    fake = FakeAnthropic(
        [
            anthropic_response(text("What is the wholesale price of the Trailhead tent?")),
            anthropic_response(text("174.30")),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query(
        "And wholesale?", history=history
    )

    condense, loop = fake.calls
    assert "tools" not in condense
    prompt = condense["messages"][0]["content"]
    assert "User: How much does the Trailhead tent cost?" in prompt
    assert "Assistant: 249.00 at retail." in prompt
    assert "New user message: And wholesale?" in prompt
    # Only the standalone question enters the tool loop, never the history itself.
    assert loop["messages"] == [
        {"role": "user", "content": "What is the wholesale price of the Trailhead tent?"}
    ]
    assert result["answer"] == "174.30"
    assert result["tokens_used"] == 30


async def test_condense_failure_falls_back_to_the_original_question(app_config, indexed_repo):
    fake = FakeAnthropic([RuntimeError("overloaded"), anthropic_response(text("ok"))])

    result = await make_engine(app_config, indexed_repo, fake).query(
        "And wholesale?", history=[{"role": "user", "text": "tent price?"}]
    )

    assert fake.calls[1]["messages"][0]["content"] == "And wholesale?"
    assert result["answer"] == "ok"


async def test_sources_fall_back_to_search_hits_mentioned_in_the_answer(app_config, indexed_repo):
    fake = FakeAnthropic(
        [
            anthropic_response(tool_use("t", "search_documents", {"query": "Bluebird"})),
            anthropic_response(text("Bluebird Trail Club appears in orders.csv.")),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query("Who is Bluebird?")

    # Both invoice.pdf and orders.csv matched; only the one named in the answer is kept.
    assert [s["file_name"] for s in result["sources"]] == ["orders.csv"]


async def test_sources_fall_back_to_search_hits_named_by_id(app_config, indexed_repo):
    """Regression (#47): an answer that cited a hit by its id fell back to every search hit."""
    orders = doc_id(indexed_repo, "orders.csv")
    fake = FakeAnthropic(
        [
            anthropic_response(tool_use("t", "search_documents", {"query": "Bluebird"})),
            anthropic_response(
                text(f"Bluebird Trail Club placed order A-1 (document ID `{orders}`).")
            ),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).query("Who is Bluebird?")

    assert [s["file_name"] for s in result["sources"]] == ["orders.csv"]


async def test_engine_tools_only_see_configured_connectors(app_config, indexed_repo):
    app_config.connectors = []  # folder removed from the config, index not pruned yet
    fake = FakeAnthropic(
        [
            anthropic_response(tool_use("t", "search_documents", {"query": "lantern"})),
            anthropic_response(text("Nothing found.")),
        ]
    )

    await make_engine(app_config, indexed_repo, fake).query("Where is the lantern?")

    assert fake.calls[1]["messages"][-1]["content"][0]["content"] == "No results for 'lantern'."


def test_generate_title(app_config, indexed_repo):
    fake = FakeAnthropic([anthropic_response(text('  "Invoice   total\n question"  '))])

    title = make_engine(app_config, indexed_repo, fake).generate_title("What is the total?", "42")

    assert title == "Invoice total question"
    prompt = fake.calls[0]["messages"][0]["content"]
    assert "Question: What is the total?" in prompt
    assert "Answer: 42" in prompt
