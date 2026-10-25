# LangGraph baseline

RootMind's tools and system prompt in LangChain's stock agent (`langchain.agents.create_agent`,
a LangGraph graph), run through the same eval cases and scored by the same code as RootMind's own
loop. It answers "why no framework?" with a measurement instead of an opinion
([#48](https://github.com/cgaeking/rootmind/issues/48)). Results:
[`backend/evals/README.md`](../../README.md#rootminds-loop-against-a-langgraph-agent).

The baseline is not part of the product. It has its own environment (`pyproject.toml` and
`uv.lock` in this folder); `backend/pyproject.toml`, `backend/uv.lock`, the packaged backend
and the backend's CI do not depend on LangChain.

## Run

From `backend/`, with Ollama running and the model pulled:

```bash
uv run --project evals/baselines/langgraph python scripts/run_evals.py --mode live \
    --agent langgraph --provider ollama --model granite4.1:8b --runs 3 --trace evals/results/traces
uv run python scripts/run_evals.py --mode live \
    --provider ollama --model granite4.1:8b --runs 3 --trace evals/results/traces
uv run python scripts/run_evals.py --compare evals/results/<rootmind>.json evals/results/<langgraph>.json
```

The first command creates the environment in `evals/baselines/langgraph/.venv` (gitignored) and
runs the baseline; the second runs RootMind's loop; the third prints both side by side. A hosted
model works the same way with `--provider anthropic|openai|xai` and the key in
`ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `XAI_API_KEY` (it costs money).

The smoke test runs with a fake chat model, no network:

```bash
uv run --project evals/baselines/langgraph pytest tests/test_baseline_langgraph.py
```

The backend's own test run skips it (no LangChain there).

## What is the same, what is the framework's own

Same as RootMind's loop, so that the difference is the loop and the framework's message handling,
not the model or the tools:

- system prompt: `rootmind.rlm_engine.engine.SYSTEM_PROMPT`, unchanged;
- tools: all five tools of the registry (`rootmind.tools.TOOLS`) with the registry's names,
  descriptions and JSON schemas, executed by `rootmind.tools.execute` on the same index. The
  issue speaks of three tools (search, read, listing); the loop offers five and the prompt names
  five, so the baseline gets the same five;
- model endpoint and call parameters: Ollama through its OpenAI-compatible `/v1` API with
  `langchain-openai` (the endpoint RootMind's loop calls, not `langchain-ollama`'s native API),
  the same output token cap (16,000), timeout and retry count; temperature is not set by either;
- sources of an answer (the same rules, from the same tool context) and the scoring
  (`run_evals.py`);
- the corpus, the index and every package both environments share: `pyproject.toml` pins them to
  the versions in `backend/uv.lock`, and the smoke test fails when the two drift.

Left as the framework does it, because that is what is compared:

- the loop ends when the model answers without a tool call;
- `tool_choice` stays `auto`: no forced first search (RootMind forces it for hosted providers)
  and no retry when the first answer did not search (RootMind's Ollama nudge);
- the step limit is LangGraph's `recursion_limit`, set to 20 steps, which allows 10 model calls
  like RootMind's iteration cap. A run that reaches it ends without an answer; RootMind asks once
  more, with tools off, for an answer from what was found;
- no per-turn or per-question tool budget (RootMind: 8 calls per turn, 200,000 result characters
  per question);
- a follow-up question (c16) gets the earlier turns as chat messages; RootMind condenses it into
  a standalone question in a separate call first;
- LangChain builds the messages, parses tool calls (malformed arguments end in
  `invalid_tool_calls` and stop the run, RootMind returns an error result to the model) and sends
  `max_completion_tokens` instead of `max_tokens`.

LangSmith: `langchain-core` brings the LangSmith client, but nothing is sent unless
`LANGSMITH_TRACING=true` and a key are set in the environment. The baseline does not set them.
`--trace` writes the same local trace files as for RootMind's loop.

## Update the pins

After a change of `backend/uv.lock`, copy the shared versions into `constraint-dependencies` in
`pyproject.toml` and run `uv lock --project evals/baselines/langgraph`. To move to newer LangChain
or LangGraph releases, change the exact pins under `dependencies`, lock, run the smoke test and
measure again.
