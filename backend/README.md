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

## Commands

`rootmind` (from `uv run`, `uv tool install` or pip) and the packaged `rootmind-backend.exe`
share one command line. Global options `--config FILE`, `--debug` and `--version` come before
the command; without a command, `api` runs.

| Command | What it does |
|---|---|
| `api [--host H] [--port P] [--require-token] [--exit-with-parent] [--import-config FILE]` | REST API and background indexer |
| `index [--full] [--connector NAME]` | One incremental (or full) sync, then exit |
| `rebuild-trigrams` | Rebuild the trigram table for fuzzy search |
| `mcp [--config FILE]` | MCP server over stdio (same as `rootmind-mcp`) |
| `mcp-http [--host H] [--port P] [--config FILE] [--index]` | MCP server over HTTP with a bearer token (same as `rootmind-mcp-http`) |
| `service install \| uninstall \| status` | Windows background service; see below |

`api` options:

- `--host`, `--port`: the bind address. Without them, `RLM_HOST`/`RLM_PORT`, then `api.host`/
  `api.port` in `config.yaml`, then `127.0.0.1:8000`. `--port 0` picks a free port.
- `--require-token`: require `Authorization: Bearer <token>` on every route except `/` and
  `/health`. The token comes from `RLM_API_TOKEN`, else from `data/rest_token.txt` under
  `RLM_HOME` (created if missing). Setting `RLM_API_TOKEN` alone also turns it on. While a
  token is required, `/docs` and `/openapi.json` are off.
- `--exit-with-parent`: stop when stdin closes or receives the line `shutdown`. The desktop
  app uses it, together with `--port 0`; the backend then prints one line
  `ROOTMIND_LISTENING {"port":N}` on stdout when it accepts connections.
- `--import-config FILE`: replace `config.yaml` with the folders and settings from `FILE`
  before starting (the database path is reset, the old file kept as `config.yaml.bak`). If
  the file cannot be imported, the current config is kept.

## MCP

```bash
uv run rootmind-mcp          # stdio transport, e.g. for Claude Desktop or Claude Code
uv run rootmind-mcp-http     # HTTP transport on 127.0.0.1:3000/mcp, bearer token required
```

Both only serve; keep the index current with `rootmind api` or `rootmind index`
(`rootmind-mcp-http --index` indexes in that process, if nothing else does). The HTTP server
picks up folder changes in `config.yaml` on its own; restart the stdio server after changing
folders.

Client configuration examples are in the
[root README](../README.md#3-use-the-tools-from-an-mcp-client).

## Windows background service

`service` manages the Windows services `RootMind-Backend` (REST API and indexer) and, with
`--mcp-http`, `RootMind-MCP-HTTP`. `install` and `uninstall` need an elevated prompt;
`status` does not.

```powershell
rootmind-backend.exe service install [--port N|auto] [--mcp-http | --no-mcp-http] [--mcp-port N|auto]
                                     [--import-config FILE] [--mcp-token-from FILE] [--user-sid SID] [--no-start]
rootmind-backend.exe service uninstall [--purge]
rootmind-backend.exe service status [--json]
```

`install` only runs from the release build (`rootmind-backend-windows-x64.zip`, or the copy
the desktop app installs). From a uv or pip install it stops and prints the download address,
because the service may only run a program that administrators alone can change; `status`
and `uninstall` work there too. Running `install` again updates the service and keeps its
ports, tokens and data. The exit codes, the folders and the security model are described in
[ARCHITECTURE.md](../ARCHITECTURE.md#background-service) and
[SECURITY.md](../SECURITY.md#background-service).

## Development

```bash
uv run ruff check .
uv run ruff format --check .
uv run pytest -q                     # offline; tests never construct a real LLM client
uv run python scripts/run_evals.py   # retrieval regression over evals/corpus (offline, see evals/README.md)
```

The packaged executable is built with PyInstaller from `packaging/rootmind-backend.spec` and
checked with `packaging/smoke-test.ps1`; see [CONTRIBUTING.md](../CONTRIBUTING.md).

## More

- [ARCHITECTURE.md](../ARCHITECTURE.md): agent loop, tools, retrieval, deployment modes,
  configuration reference and environment variables
- [SECURITY.md](../SECURITY.md): threat model
- [evals/README.md](evals/README.md): eval corpus, cases and modes
- [deploy/windows/README.md](deploy/windows/README.md): `run-backend.bat`, and moving from an
  older NSSM-based service setup
