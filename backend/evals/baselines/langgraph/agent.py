"""The LangGraph baseline: RootMind's tools and system prompt in LangChain's stock agent (#48).

What is the same as RootMind's own loop (``rootmind.rlm_engine.engine``):

- the system prompt (:data:`~rootmind.rlm_engine.engine.SYSTEM_PROMPT`);
- the tools: every tool of the registry (:data:`rootmind.tools.TOOLS`, the same five the
  loop offers), with the registry's names, descriptions and JSON schemas, executed by
  :func:`rootmind.tools.execute` on the same index, so each call returns exactly the text
  RootMind's loop would get;
- the model endpoint: Ollama, OpenAI and xAI through their OpenAI-compatible Chat
  Completions API, Anthropic through the Messages API, with the same output token cap,
  timeout and retry count;
- the sources of an answer, from the same tool context and the same rules
  (:meth:`KnowledgeBaseEngine._extract_sources`), and the scoring in ``scripts/run_evals.py``.

What is the framework's own (deliberately left stock, since that is what is compared):

- the loop: :func:`langchain.agents.create_agent`, a LangGraph graph of a model node and a
  tool node, ending when the model answers without a tool call;
- ``tool_choice`` stays ``auto`` on every call: no forced first search (RootMind forces it
  for hosted providers) and no "search first" retry (RootMind's Ollama nudge);
- the step limit: LangGraph's ``recursion_limit``, set so the model gets at most
  :data:`MAX_MODEL_CALLS` calls like RootMind's iteration cap; when it is hit, the run
  ends without an answer (RootMind asks once more, with tools off, for a final answer);
- no per-turn or per-question tool budget (RootMind runs at most 8 calls per turn and
  200,000 result characters per question);
- follow-up questions: the history goes into the conversation as earlier messages (the
  usual chat-agent pattern) instead of RootMind's separate condense step;
- how messages are built, how tool calls are parsed (malformed arguments end up in
  ``invalid_tool_calls`` instead of an error result) and the request parameters
  (LangChain sends ``max_completion_tokens`` instead of ``max_tokens``).

The agent has the same ``query`` interface as :class:`KnowledgeBaseEngine`, so the eval
harness runs it with ``run_evals.py --agent langgraph`` and scores it with the same code.
"""

from __future__ import annotations

import asyncio
from importlib import metadata
from typing import Any
from uuid import UUID

from langchain.agents import create_agent
from langchain_core.callbacks import BaseCallbackHandler
from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage
from langchain_core.outputs import LLMResult
from langchain_core.tools import BaseTool, StructuredTool
from langgraph.errors import GraphRecursionError

from rootmind import tools
from rootmind.config import OLLAMA_DEFAULT_BASE_URL, AppConfig
from rootmind.database.repository import DocumentRepository
from rootmind.rlm_engine.engine import (
    DEFAULT_MAX_ITERATIONS,
    LLM_MAX_RETRIES,
    MAX_OUTPUT_TOKENS,
    SYSTEM_PROMPT,
    XAI_BASE_URL,
    KnowledgeBaseEngine,
    _describe_tool,
)

# The model calls one question may make, as RootMind's iteration cap.
MAX_MODEL_CALLS = DEFAULT_MAX_ITERATIONS
# LangGraph counts every node run as a step: a model call and its tool node are
# two. 2 * MAX_MODEL_CALLS steps allow MAX_MODEL_CALLS model calls; the next
# model call raises GraphRecursionError.
RECURSION_LIMIT = 2 * MAX_MODEL_CALLS
RECURSION_LIMIT_ANSWER = "(no answer: the agent reached LangGraph's recursion limit)"

# Packages whose versions are recorded with every baseline result.
VERSIONED_PACKAGES = (
    "langchain",
    "langchain-core",
    "langgraph",
    "langgraph-prebuilt",
    "langchain-openai",
    "langchain-anthropic",
    "openai",
    "anthropic",
)


def package_versions() -> dict[str, str | None]:
    """Installed versions of the packages the baseline depends on."""
    versions: dict[str, str | None] = {}
    for name in VERSIONED_PACKAGES:
        try:
            versions[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            versions[name] = None
    return versions


def build_chat_model(config: AppConfig) -> BaseChatModel:
    """The LangChain chat model for ``config.llm``, with RootMind's call parameters.

    Imports the provider's integration only when it is needed.
    """
    llm = config.llm
    common = {
        "model": llm.model,
        "max_tokens": MAX_OUTPUT_TOKENS,
        "timeout": llm.timeout_seconds,
        "max_retries": LLM_MAX_RETRIES,
    }
    if llm.provider == "anthropic":
        from langchain_anthropic import ChatAnthropic

        return ChatAnthropic(api_key=llm.api_key, **common)
    from langchain_openai import ChatOpenAI

    if llm.provider == "ollama":
        base = (llm.base_url or OLLAMA_DEFAULT_BASE_URL).rstrip("/")
        # Ollama needs no key; the SDK only insists on a non-empty one.
        return ChatOpenAI(
            api_key="ollama",  # pragma: allowlist secret
            base_url=f"{base}/v1",
            use_responses_api=False,
            **common,
        )
    if llm.provider == "openai":
        return ChatOpenAI(api_key=llm.api_key, use_responses_api=False, **common)
    if llm.provider == "xai":
        return ChatOpenAI(
            api_key=llm.api_key, base_url=XAI_BASE_URL, use_responses_api=False, **common
        )
    raise ValueError(f"Unsupported LLM provider: {llm.provider}")


def history_messages(history: list[dict[str, str]] | None) -> list[BaseMessage]:
    """Earlier turns (``{"role", "text"}``) as chat messages."""
    messages: list[BaseMessage] = []
    for turn in history or []:
        if turn["role"] == "user":
            messages.append(HumanMessage(turn["text"]))
        else:
            messages.append(AIMessage(turn["text"]))
    return messages


def message_text(message: BaseMessage) -> str:
    """The text of a message whose content is a string or a list of content blocks."""
    content = message.content
    if isinstance(content, str):
        return content
    parts = []
    for block in content:
        if isinstance(block, str):
            parts.append(block)
        elif isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text", ""))
    return "".join(parts)


def _stop_reason(message: AIMessage) -> str:
    meta = message.response_metadata or {}
    return str(meta.get("finish_reason") or meta.get("stop_reason") or "unknown")


def _tokens(message: AIMessage) -> int:
    usage = message.usage_metadata or {}
    return int(usage.get("total_tokens") or 0)


class _Run:
    """One question: tool context, model call count and the trace, if any."""

    def __init__(self, ctx: tools.ToolContext, trace: Any | None):
        self.ctx = ctx
        self.trace = trace
        self.model_calls = 0


def make_tools(repo: DocumentRepository, run: _Run) -> list[BaseTool]:
    """The registry as LangChain tools that run :func:`rootmind.tools.execute` in ``run.ctx``."""

    def bind(name: str):
        def call(**kwargs: Any) -> str:
            ctx = run.ctx
            span = None
            if run.trace is not None:
                span = run.trace.tool_call(
                    iteration=run.model_calls, name=name, arguments=dict(kwargs)
                )
            refs_before, reads_before = len(ctx.referenced), len(ctx.read_windows)
            result = tools.execute(name, kwargs, repo, ctx)
            if span is not None:
                span.end(**_describe_tool(result, ctx, refs_before, reads_before))
            return result.text

        return call

    return [
        StructuredTool.from_function(
            func=bind(t["name"]),
            name=t["name"],
            description=t["description"],
            args_schema=t["input_schema"],
        )
        for t in tools.TOOLS
    ]


class _TraceCallback(BaseCallbackHandler):
    """Records every model call of a run as a model span of RootMind's tracer."""

    def __init__(self, run: _Run, model: str | None):
        self.run = run
        self.model = model
        self.spans: dict[UUID, Any] = {}

    def on_chat_model_start(
        self,
        serialized: dict[str, Any],
        messages: list[list[BaseMessage]],
        *,
        run_id: UUID,
        **kwargs: Any,
    ) -> None:
        self.run.model_calls += 1
        if self.run.trace is not None:
            self.spans[run_id] = self.run.trace.model_call(
                purpose="loop",
                iteration=self.run.model_calls,
                model=self.model,
                tool_choice=None,
                messages=len(messages[0]) if messages else 0,
            )

    def on_llm_end(self, response: LLMResult, *, run_id: UUID, **kwargs: Any) -> None:
        span = self.spans.pop(run_id, None)
        if span is None:
            return
        generation = response.generations[0][0] if response.generations else None
        message = getattr(generation, "message", None)
        if not isinstance(message, AIMessage):
            span.end(error="response not recorded")
            return
        usage = message.usage_metadata or {}
        span.end(
            stop_reason=_stop_reason(message),
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            total_tokens=usage.get("total_tokens"),
            text=message_text(message),
            tool_requests=[{"name": c["name"], "arguments": c["args"]} for c in message.tool_calls]
            + [
                {"name": c.get("name"), "arguments": c.get("args"), "invalid": True}
                for c in message.invalid_tool_calls
            ],
        )

    def on_llm_error(self, error: BaseException, *, run_id: UUID, **kwargs: Any) -> None:
        span = self.spans.pop(run_id, None)
        if span is not None:
            span.end(error=type(error).__name__)


class LangGraphAgent:
    """Answers questions with LangChain's stock agent over RootMind's tools.

    Has the ``query`` interface of :class:`KnowledgeBaseEngine`.
    """

    def __init__(
        self,
        repo: DocumentRepository,
        config: AppConfig,
        model: BaseChatModel | None = None,
        tracer: Any | None = None,
        recursion_limit: int = RECURSION_LIMIT,
    ):
        self.repo = repo
        self.config = config
        self.model = model if model is not None else build_chat_model(config)
        self.tracer = tracer
        self.recursion_limit = recursion_limit
        # Only for the source rules; this engine never calls a model.
        self._sources = KnowledgeBaseEngine(repo, config)

    def _context(self) -> tools.ToolContext:
        return tools.ToolContext(
            allowed_connectors=frozenset(c.name for c in self.config.connectors)
        )

    async def query(
        self,
        question: str,
        history: list[dict[str, str]] | None = None,
        trace_tags: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Answer ``question``; returns the keys of :meth:`KnowledgeBaseEngine.query`."""
        return await asyncio.to_thread(self._query, question, history, trace_tags)

    def _query(
        self,
        question: str,
        history: list[dict[str, str]] | None,
        trace_tags: dict[str, Any] | None,
    ) -> dict[str, Any]:
        trace = None
        if self.tracer is not None:
            trace = self.tracer.question(
                question=question,
                provider=self.config.llm.provider,
                model=self.config.llm.model,
                history_turns=len(history or []),
                tags={**(trace_tags or {}), "agent": "langgraph"},
            )
        run = _Run(self._context(), trace)
        agent = create_agent(
            self.model, tools=make_tools(self.repo, run), system_prompt=SYSTEM_PROMPT
        )
        inputs = {"messages": [*history_messages(history), HumanMessage(question)]}
        config = {
            "recursion_limit": self.recursion_limit,
            "callbacks": [_TraceCallback(run, self.config.llm.model)],
        }

        state: dict[str, Any] = inputs
        limit_reached = False
        try:
            # "values" yields the whole state after every step, so the messages
            # (tokens, tool calls) are still known when the recursion limit ends the run.
            for step in agent.stream(inputs, config=config, stream_mode="values"):
                state = step
        except GraphRecursionError:
            limit_reached = True
            if trace is not None:
                trace.event("iteration_limit", iterations=MAX_MODEL_CALLS)
        except Exception as e:
            if trace is not None:
                trace.end(error=type(e).__name__, tool_calls=len(run.ctx.tool_calls))
            raise

        new_messages = state["messages"][len(inputs["messages"]) :]
        replies = [m for m in new_messages if isinstance(m, AIMessage)]
        last = replies[-1] if replies else None
        if limit_reached or last is None or last.tool_calls:
            answer = RECURSION_LIMIT_ANSWER if limit_reached else ""
        else:
            answer = message_text(last)
        stop_reasons = [_stop_reason(m) for m in replies]
        if limit_reached:
            stop_reasons.append("recursion_limit")
        sources = self._sources._extract_sources(run.ctx, answer)
        result = {
            "answer": answer,
            "sources": sources,
            "tokens_used": sum(_tokens(m) for m in replies),
            "tool_calls": run.ctx.tool_calls,
            "stop_reasons": stop_reasons,
            "invalid_tool_calls": sum(len(m.invalid_tool_calls) for m in replies),
        }
        if trace is not None:
            trace.end(
                answer=answer,
                sources=[
                    {"file_name": s["file_name"], "page": s.get("page"), "id": s["id"]}
                    for s in sources
                ],
                tokens_used=result["tokens_used"],
                stop_reasons=stop_reasons,
                tool_calls=len(run.ctx.tool_calls),
            )
            result["trace_id"] = trace.trace_id
        return result
