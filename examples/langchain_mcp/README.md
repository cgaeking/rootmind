# RootMind's MCP server in a LangChain agent

A small example that RootMind fits into an existing agent stack: RootMind's MCP server
(`rootmind-mcp`, over stdio) provides the tools, [langchain-mcp-adapters](https://github.com/langchain-ai/langchain-mcp-adapters)
turns them into LangChain tools, and LangChain's stock agent (`create_agent`, a LangGraph graph)
uses them with a local Ollama model. The agent gets the same five read-only tools as RootMind's own
loop (`search_documents`, `search_fuzzy`, `list_documents`, `read_document`, `get_statistics`); it
cannot change or delete files.

This folder has its own environment. It does not import RootMind: `langchain-mcp-adapters` 0.3.2
needs the MCP SDK 1.x (pinned here to 1.30.0), the backend uses 2.x, and the two only talk over
stdio, where the protocol versions are negotiated.

## Run

Needs [uv](https://docs.astral.sh/uv/), a backend checkout with its environment (`uv sync` in
`backend/`), an index (`uv run rootmind index` in `backend/`, with your `config.yaml`) and Ollama
with a tool-capable model.

```bash
cd examples/langchain_mcp
uv sync
uv run python rootmind_agent.py "What is the total due on invoice INV-2026-0142?"
uv run python rootmind_agent.py --config /path/to/config.yaml --model qwen3:8b "Who signed the lease?"
```

The script starts `uv --directory <backend> run rootmind-mcp [--config <file>]` as a subprocess,
keeps one MCP session open for the question (without it, langchain-mcp-adapters starts a new
server for every tool call), and prints the answer. Without `--config`, the server uses
`config.yaml` in `backend/` (or under `RLM_HOME`), like any other MCP client. Nothing leaves the
computer: the server reads the local index and the model runs in Ollama. With a hosted model
instead (replace `ollama_model()` with, for example, `ChatAnthropic`), the questions and the
document text the model reads go to that provider.

The installed RootMind service also serves MCP over HTTP with a bearer token (Settings → RootMind
service shows the endpoint and copies the token). langchain-mcp-adapters connects to it with:

```python
connections = {
    "rootmind": {
        "transport": "streamable_http",
        "url": "http://127.0.0.1:<port>/mcp",
        "headers": {"Authorization": f"Bearer {token}"},
    }
}
```

Keep the token out of the code and out of the shell history, for example in an environment
variable.

## Test

```bash
uv run pytest
```

Offline: it indexes one small Markdown file with RootMind's CLI into a temporary folder, starts the
real MCP server over stdio, loads its tools through langchain-mcp-adapters, and runs the agent with
a fake chat model that searches and answers. It skips when `uv` or the backend environment is
missing. The backend's CI does not run it.
