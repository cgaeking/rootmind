# RootMind backend

Python service that indexes local documents into SQLite FTS5 and answers questions with an
agentic LLM tool loop: the model decides what to search, list and read. The same read-only
tools are exposed over a localhost REST API and over MCP (stdio, and HTTP with a bearer token).
The desktop app uses the REST API of the RootMind Windows service, which runs this backend.

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
| `api [--host H] [--port P] [--require-token]` | REST API and background indexer |
| `index [--full] [--connector NAME]` | One incremental (or full) sync, then exit |
| `rebuild-trigrams` | Rebuild the trigram table for fuzzy search |
| `mcp [--config FILE]` | MCP server over stdio (same as `rootmind-mcp`) |
| `mcp-http [--host H] [--port P] [--config FILE] [--index]` | MCP server over HTTP with a bearer token (same as `rootmind-mcp-http`) |
| `service install \| uninstall \| status` | Windows background service; see below |

`api` options:

- `--host`, `--port`: the bind address. Without them, `RLM_HOST`/`RLM_PORT`, then `api.host`/
  `api.port` in `config.yaml`, then `127.0.0.1:8000`. `--port 0` picks a free port, which the
  log names.
- `--require-token`: require `Authorization: Bearer <token>` on every route except `/` and
  `/health`. The token comes from `RLM_API_TOKEN`, else from `data/rest_token.txt` under
  `RLM_HOME` (created if missing). Setting `RLM_API_TOKEN` alone also turns it on. While a
  token is required, `/docs` and `/openapi.json` are off.

The desktop app does not start `api`; it connects to the Windows service (see below). `api` is
for development and for setups without the service.

## MCP

```bash
uv run rootmind-mcp          # stdio transport, e.g. for Claude Desktop or Claude Code
uv run rootmind-mcp-http     # HTTP transport on 127.0.0.1:3000/mcp, bearer token required
```

Both only serve; keep the index current with `rootmind api` or `rootmind index`
(`rootmind-mcp-http --index` indexes in that process, if nothing else does). The HTTP server
picks up folder changes in `config.yaml` on its own; restart the stdio server after changing
folders.

The stdio server suits a source checkout or a uv install with its own `config.yaml`. It does
not work with the installed app: `rootmind-backend.exe mcp` from the app's folder cannot open
the service's index in `%ProgramData%\RootMind`. Connect MCP clients to the service's MCP over
HTTP instead (`RootMind-MCP-HTTP`, see
[Connect MCP clients](../README.md#connect-mcp-clients)).

Client configuration examples for a source or uv setup are in the
[root README](../README.md#3-use-the-tools-from-an-mcp-client).

## Windows background service

`service` manages the Windows services `RootMind-Backend` (REST API and indexer) and
`RootMind-MCP-HTTP` (MCP over HTTP on 127.0.0.1, bearer token). `install` and `uninstall`
need an elevated prompt; `status` does not.

```powershell
rootmind-backend.exe service install [--port N|auto] [--mcp-http | --no-mcp-http] [--mcp-port N|auto]
                                     [--mcp-token-from FILE] [--user-sid SID] [--no-start]
rootmind-backend.exe service uninstall [--purge]
rootmind-backend.exe service status [--json]
```

The RootMind installer runs `service install --port auto --mcp-port auto` for the account
signed in to Windows, and its uninstaller runs `service uninstall`. The desktop app runs
`service install` again, elevated: for **Repair** and **Update service** (`--port auto
--mcp-port auto`) with its own backend in `%ProgramFiles%\RootMind\backend`, and to switch MCP
over HTTP on or off with the service's registered copy under `%ProgramFiles%\RootMind Service\`.

`install` only runs from the release build (`rootmind-backend-windows-x64.zip`, or the
installed app's `%ProgramFiles%\RootMind\backend` folder). From a uv or pip install it stops
and prints the download address, because the service may only run a program that
administrators alone can change; `status` and `uninstall` work there too. For the same reason
it refuses with exit code 10 to copy the program from a folder that accounts other than
administrators can change, such as a download folder: unzip the release build into a folder
under `%ProgramFiles%` first. A new install
serves MCP over HTTP unless `--no-mcp-http` is given. Without a `config.yaml` in
`%ProgramData%\RootMind`, `install` writes one without folders; add folders in the app or
with `POST /config` and the REST token. Running `install` again updates the service and keeps
its ports, MCP setting, tokens and data. The exit codes, the folders and the security model
are described in
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
- [deploy/windows/README.md](deploy/windows/README.md): `run-backend.bat`, the REST API from
  source in a console window
