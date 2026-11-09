"""Checking a model (POST /llm/check) and remembering its quirks across restarts."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fakes import (
    FakeAnthropic,
    FakeOpenAI,
    anthropic_response,
    openai_response,
    text,
    tool_use,
)
from helpers import make_engine

from rootmind.api import routes
from rootmind.rlm_engine import engine as engine_module
from rootmind.rlm_engine.engine import (
    CHECK_QUESTION,
    MAX_OUTPUT_TOKENS,
    KnowledgeBaseEngine,
    LLMProviderError,
    LLMTimeoutError,
    effort_for,
    load_model_quirks,
    model_quirks,
)

REFUSED = ValueError('tool_choice: type "tool" and "any" are not supported for this model.')


# --- the check ---------------------------------------------------------------------


def _with_models(fake, capabilities):
    """Give a fake Anthropic client a Models API that reports ``capabilities``."""
    fake.models = SimpleNamespace(retrieve=lambda model: SimpleNamespace(capabilities=capabilities))
    return fake


EFFORT_CAPS = {
    "effort": {
        "supported": True,
        "low": {"supported": True},
        "medium": {"supported": True},
        "high": {"supported": True},
        "xhigh": {"supported": False},
        "max": {"supported": True},
    }
}


async def test_check_makes_one_request_like_a_first_step(app_config, indexed_repo):
    fake = _with_models(
        FakeAnthropic([anthropic_response(tool_use("t1", "search_documents", {"query": "x"}))]),
        EFFORT_CAPS,
    )

    result = await make_engine(app_config, indexed_repo, fake).check_model()

    (call,) = fake.calls
    assert call["tool_choice"] == {"type": "any"}
    assert call["max_tokens"] == MAX_OUTPUT_TOKENS
    assert call["messages"] == [{"role": "user", "content": CHECK_QUESTION}]
    assert result["checked_now"] is True
    assert result["efforts"] == ["low", "medium", "high", "max"]
    assert result["quirks"]["checked"] is True


async def test_check_learns_what_the_model_refuses(app_config, indexed_repo):
    fake = FakeAnthropic([REFUSED, anthropic_response()])

    result = await make_engine(app_config, indexed_repo, fake).check_model()

    assert "tool_choice" not in fake.calls[1]
    assert result["quirks"]["no_forced_tool"] is True
    assert result["efforts"] is None  # no Models API on this fake: not known


async def test_a_known_or_checked_model_is_not_checked_again(app_config, indexed_repo):
    app_config.llm.model = "claude-opus-5-5"  # in known_models
    fake = FakeAnthropic([])

    result = await make_engine(app_config, indexed_repo, fake).check_model()

    assert fake.calls == []
    assert result["checked_now"] is False
    assert result["quirks"]["no_forced_tool"] is True
    assert result["efforts"] == ["low", "medium", "high", "xhigh", "max"]


async def test_force_checks_a_known_model_again(app_config, indexed_repo):
    app_config.llm.model = "claude-opus-5-5"
    fake = FakeAnthropic([anthropic_response()])

    result = await make_engine(app_config, indexed_repo, fake).check_model(force=True)

    assert len(fake.calls) == 1
    assert "tool_choice" not in fake.calls[0]  # known: no forced call to begin with
    assert result["checked_now"] is True


async def test_check_of_an_openai_model(app_config, indexed_repo):
    app_config.llm.provider = "openai"
    app_config.llm.model = "gpt-test"
    no_none = ValueError(
        "'reasoning_effort' does not support 'none' with this model. "
        "Supported values are: 'low', 'medium'."
    )
    effort_values = ValueError(
        "Invalid value: 'rootmind-check'. Supported values are: 'low', 'medium', and 'high'."
    )
    fake = FakeOpenAI(
        [ValueError("reasoning_effort required"), no_none, openai_response("ok"), effort_values]
    )

    result = await make_engine(app_config, indexed_repo, fake).check_model()

    assert fake.calls[0]["tool_choice"] == "required"
    assert fake.calls[3]["reasoning_effort"] == "rootmind-check"
    assert result["quirks"]["reasoning_effort"] == "low"
    assert result["efforts"] == ["low", "medium", "high"]


TOOLS_NEED_NONE = ValueError(
    "Function tools with reasoning_effort are not supported for gpt-sol in "
    "/v1/chat/completions. To use function tools, use /v1/responses or set "
    "reasoning_effort to 'none'."
)


async def test_a_model_that_takes_tools_only_without_reasoning(app_config, indexed_repo):
    app_config.llm.provider = "openai"
    app_config.llm.model = "gpt-sol"
    effort_values = ValueError(
        "Invalid value: 'rootmind-check'. Supported values are: 'none', 'low' and 'high'."
    )
    fake = FakeOpenAI([TOOLS_NEED_NONE, openai_response("ok"), effort_values])

    result = await make_engine(app_config, indexed_repo, fake).check_model()

    assert fake.calls[1]["reasoning_effort"] == "none"
    assert result["efforts"] == ["none"]  # the only level a question can use


async def test_the_suggested_none_is_tried_after_another_level(app_config, indexed_repo):
    # Learned earlier (e.g. without tools): "low". With tools the model wants "none".
    app_config.llm.provider = "openai"
    app_config.llm.model = "gpt-sol"
    model_quirks("openai", "gpt-sol").update({"reasoning_effort": "low"})
    fake = FakeOpenAI([TOOLS_NEED_NONE, openai_response(content="ok")])

    await make_engine(app_config, indexed_repo, fake).query("q")

    assert fake.calls[0]["reasoning_effort"] == "low"
    assert fake.calls[1]["reasoning_effort"] == "none"
    assert model_quirks("openai", "gpt-sol")["reasoning_effort"] == "none"


async def test_an_openai_model_without_reasoning_effort(app_config, indexed_repo):
    app_config.llm.provider = "openai"
    app_config.llm.model = "gpt-plain"
    fake = FakeOpenAI(
        [
            openai_response("ok"),
            ValueError("Unrecognized request argument supplied: reasoning_effort"),
        ]
    )

    result = await make_engine(app_config, indexed_repo, fake).check_model()

    assert result["efforts"] == []


# --- the reasoning effort setting --------------------------------------------------


@pytest.mark.parametrize(
    ("efforts", "setting", "expected"),
    [
        (None, "high", None),
        ([], "high", None),
        (["low", "medium", "high"], "high", "high"),
        (["low", "medium", "high"], "auto", None),  # the model's default
        (["low", "medium", "high"], "max", "high"),  # not supported: the next below
        (["low", "medium", "high"], "minimal", "low"),  # none below: the lowest
        (["high", "none", "low"], "medium", "low"),
    ],
)
def test_effort_for(efforts, setting, expected):
    quirks = {} if efforts is None else {"efforts": efforts}
    assert effort_for(quirks, setting) == expected


async def test_questions_send_the_chosen_effort(app_config, indexed_repo):
    app_config.llm.model = "claude-opus-5-5"
    app_config.llm.reasoning_effort = "high"
    # No forced tool call for this model: an answer without a search is nudged once.
    fake = FakeAnthropic([anthropic_response(text("ok")), anthropic_response(text("ok"))])

    await make_engine(app_config, indexed_repo, fake).query("q")

    assert all(call["output_config"] == {"effort": "high"} for call in fake.calls)


async def test_auto_effort_sends_none(app_config, indexed_repo):
    app_config.llm.provider = "openai"
    app_config.llm.model = "gpt-reasoner"
    model_quirks("openai", "gpt-reasoner").update({"efforts": ["low", "medium", "high"]})
    fake = FakeOpenAI([openai_response(content="ok")])

    await make_engine(app_config, indexed_repo, fake).query("q")

    assert "reasoning_effort" not in fake.calls[0]


async def test_no_effort_for_a_model_without_levels(app_config, indexed_repo):
    fake = FakeAnthropic([anthropic_response(text("ok"))])

    await make_engine(app_config, indexed_repo, fake).query("q")

    assert "output_config" not in fake.calls[0]


# --- remembering across restarts ---------------------------------------------------


async def test_learned_quirks_are_written_and_read_back(app_config, indexed_repo, tmp_path):
    path = tmp_path / "data" / "model_quirks.json"
    load_model_quirks(path)  # no file yet: starts empty

    fake = FakeAnthropic([REFUSED, anthropic_response()])
    await make_engine(app_config, indexed_repo, fake).check_model()

    saved = json.loads(path.read_text(encoding="utf-8"))
    assert saved == {
        "version": 1,
        "models": {"anthropic": {app_config.llm.model: {"checked": True, "no_forced_tool": True}}},
    }

    # A restart: the memory is empty, the file brings the model back as checked.
    engine_module._MODEL_QUIRKS.clear()
    load_model_quirks(path)
    fake = FakeAnthropic([])
    result = await make_engine(app_config, indexed_repo, fake).check_model()
    assert fake.calls == [] and result["checked_now"] is False


@pytest.mark.parametrize(
    "content",
    [
        "not json",
        "[]",
        '{"models": []}',
        '{"models": {"anthropic": {"m": {"no_forced_tool": "yes", "unknown": 1, "max_cap": -5}}}}',
    ],
)
def test_a_damaged_quirks_file_is_ignored(tmp_path, content):
    path = tmp_path / "model_quirks.json"
    path.write_text(content, encoding="utf-8")

    load_model_quirks(path)

    assert model_quirks("anthropic", "m") == {}


def test_valid_quirks_are_loaded(tmp_path):
    path = tmp_path / "model_quirks.json"
    path.write_text(
        json.dumps(
            {
                "version": 1,
                "models": {
                    "openai": {"gpt-x": {"reasoning_effort": "low", "max_cap": 4096}},
                    "anthropic": {"claude-x": {"no_forced_tool": True}},
                },
            }
        ),
        encoding="utf-8",
    )

    load_model_quirks(path)

    assert model_quirks("openai", "gpt-x") == {"reasoning_effort": "low", "max_cap": 4096}
    assert model_quirks("anthropic", "claude-x") == {"no_forced_tool": True}


# --- POST /llm/check -----------------------------------------------------------------


@pytest.fixture
def checked(monkeypatch):
    """Replace the model check with a scripted outcome; records the config it ran with."""
    seen: list[tuple[str, str, str | None]] = []
    outcome: dict = {"result": {"quirks": {}}}

    async def fake_check(self, force=False):
        seen.append((self.config.llm.provider, self.config.llm.model, self.config.llm.api_key))
        if isinstance(outcome["result"], Exception):
            raise outcome["result"]
        return {
            "provider": self.config.llm.provider,
            "model": self.config.llm.model,
            "checked_now": True,
            "efforts": None,
            **outcome["result"],
        }

    monkeypatch.setattr(KnowledgeBaseEngine, "check_model", fake_check)
    return seen, outcome


def _status_error(status: int) -> LLMProviderError:
    error = LLMProviderError(f"The LLM provider (anthropic) returned HTTP {status}: why")
    cause = Exception("sdk")
    cause.status_code = status  # type: ignore[attr-defined]
    error.__cause__ = cause
    return error


def test_check_endpoint_reports_a_usable_model(api, checked):
    seen, outcome = checked
    outcome["result"] = {"quirks": {"no_forced_tool": True}}

    res = api.post("/llm/check", json={"provider": "anthropic", "model": "claude-x"})

    assert res.status_code == 200
    assert res.json() == {
        "provider": "anthropic",
        "model": "claude-x",
        "ok": True,
        "reason": None,
        "checked_now": True,
        "efforts": None,
        "quirks": {"no_forced_tool": True},
    }
    assert seen[0][:2] == ("anthropic", "claude-x")
    assert seen[0][2]  # the saved key of the provider


@pytest.mark.parametrize(
    ("error", "ok"),
    [
        (_status_error(400), False),
        (_status_error(404), False),
        (_status_error(401), False),
        (_status_error(429), None),
        (_status_error(529), None),
        (LLMProviderError("Could not reach the LLM provider (anthropic)"), None),
        (LLMTimeoutError("LLM request timed out after 60s"), None),
    ],
)
def test_check_endpoint_separates_refusals_from_failed_checks(api, checked, error, ok):
    _, outcome = checked
    outcome["result"] = error

    res = api.post("/llm/check", json={"model": "claude-x"})

    assert res.status_code == 200
    assert res.json()["ok"] is ok
    assert res.json()["reason"] == str(error)


def test_check_endpoint_needs_a_key(api, checked):
    res = api.post("/llm/check", json={"provider": "openai", "model": "gpt-x"})

    assert res.status_code == 400
    assert "No API key" in res.json()["detail"]
    assert checked[0] == []


def test_a_refused_model_is_left_out_of_the_model_list(api, checked, monkeypatch):
    _, outcome = checked
    outcome["result"] = _status_error(404)
    monkeypatch.setattr(
        routes, "_list_chat_models", lambda provider, key, base_url=None: ["claude-x", "claude-y"]
    )

    assert api.post("/llm/check", json={"model": "claude-x"}).json()["ok"] is False
    assert model_quirks("anthropic", "claude-x")["unusable"]

    res = api.post("/llm/models", json={"provider": "anthropic"})
    assert res.json()["models"] == ["claude-y"]


def test_a_rejected_key_does_not_make_the_model_unusable(api, checked):
    _, outcome = checked
    outcome["result"] = _status_error(401)

    assert api.post("/llm/check", json={"model": "claude-x"}).json()["ok"] is False
    assert "unusable" not in model_quirks("anthropic", "claude-x")
