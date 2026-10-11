# RootMind backend

Python service that indexes local documents into SQLite FTS5 and answers questions with an
agentic LLM tool loop: the model decides what to search, list and read. The same read-only
tools are exposed over a localhost REST API (used by the desktop app) and over MCP (stdio,
and HTTP with a bearer token).

## Quickstart

```bash
cd backend
uv sync                                   # Python 3.12+, creates .venv
cp config.example.yaml config.yaml        # indexes the demo corpus in evals/corpus
uv run rootmind index                     # build the full-text index (no LLM needed)
uv run rootmind api                       # REST API on http://127.0.0.1:8000, docs at /docs
```

To index your own files, add folders under `connectors` in `config.yaml` (use absolute
paths). Search works without an API key. `POST /query` needs a key for the configured
provider: set `ANTHROPIC_API_KEY`, `OPENAI_API_KEY` or `XAI_API_KEY` in the environment or in
`.env` (see `.env.example`), or save it through the desktop app (stored in `config.yaml`).

## MCP

```bash
uv run rootmind-mcp          # stdio transport, e.g. for Claude Desktop or Claude Code
uv run rootmind-mcp-http     # HTTP transport on 127.0.0.1:3000/mcp, bearer token required
```

Both only serve; keep the index current with `rootmind api` or `rootmind index`
(`rootmind-mcp-http --index` indexes in that process, if nothing else does). Restart them
after changing folders.

Client configuration examples are in the
[root README](../README.md#3-use-the-tools-from-an-mcp-client).

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q                     # offline; tests never construct a real LLM client
uv run python scripts/run_evals.py   # retrieval regression over evals/corpus (offline, see evals/README.md)
```

## More

- [ARCHITECTURE.md](../ARCHITECTURE.md): agent loop, tools, retrieval, configuration
  reference and environment variables
- [SECURITY.md](../SECURITY.md): threat model
- [evals/README.md](evals/README.md): eval corpus, cases and modes
- [deploy/windows/README.md](deploy/windows/README.md): running the backend as a Windows
  service (experimental)
