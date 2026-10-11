# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's private
vulnerability reporting: open the repository's **Security** tab and choose
**Report a vulnerability**. Do not open a public issue for a security problem.

RootMind is a personal project without a support team. Reports are read and
handled on a best-effort basis, and fixes land on the `main` branch. There are
no maintained release branches.

## Intended use

RootMind is designed for **one person on their own computer**: the backend,
the desktop app and the indexed folders all belong to the same user. The
design does not protect against other local users of the same machine, against
local administrators, or against exposing the REST API to a network. These
are out of scope (see the end of this document).

## What leaves the machine

| Action | What is sent | Where |
|---|---|---|
| Indexing, search, listing, reading | Nothing | Local only; no LLM is involved |
| Asking a question (`POST /query`) | The question; for follow-ups, the last few chat turns (for the condense step); every tool result the model requests, which includes search snippets and document text of up to 50,000 characters per read | The LLM provider you configured (Anthropic, OpenAI or xAI) |
| Chat titles (`POST /chat/title`) | Up to 500 characters each of the first question and its answer | The configured provider |
| Model list (`POST /llm/models`) | The API key, to list models | The configured provider |
| MCP tool calls | Tool results go to the MCP client, and from there to whatever model that client uses | Decided by the MCP client |

RootMind has no telemetry and no update check. The desktop app's CSP only
allows network requests to `localhost` and `127.0.0.1`.

## Data at rest

| Data | Location | Notes |
|---|---|---|
| API keys saved through the desktop app | `config.yaml` and its backup `config.yaml.bak` in `RLM_HOME` | **Plain text.** Written atomically and owner-only (0600) on Linux and macOS; on Windows the folder's permissions apply. Prefer environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`) |
| Index | `data/index.db` (plus `-wal`/`-shm`) in `RLM_HOME` | Contains the **full extracted text** of every indexed file |
| MCP HTTP token | `data/api_token.txt` in `RLM_HOME`, unless `MCP_API_TOKEN` is set | Created with mode 0600 on Linux and macOS; on Windows it inherits the folder's permissions |
| Logs | Console, or `%ProgramData%\RootMind\logs` for the Windows service | Contain file paths, questions and tool arguments. RootMind never logs API keys or the MCP token; provider error messages are logged as the provider sends them |
| Chat history | `%APPDATA%\io.github.cgaeking.rootmind\conversations` | Questions, answers and source paths in plain JSON |

RootMind never indexes its own data: the `data` folder, the database's folder
and, when `RLM_HOME` is set, all of `RLM_HOME` are skipped even inside an
indexed folder, and a folder inside them is refused.

For the Windows service, `RLM_HOME` is `%ProgramData%\RootMind`. Installing
the service takes ownership of that folder, removes every other permission
and grants access to SYSTEM and Administrators only.

## Threat model

### REST API (no authentication)

The REST API is meant for the desktop app on the same machine and has no
authentication. The defenses are layered:

- It binds to `127.0.0.1` by default (`api.host`, `--host`). Binding to
  another address logs a warning, and requests from non-loopback addresses
  are refused with 403 regardless of the bind address.
- `TrustedHostMiddleware` rejects requests whose `Host` header is not
  `127.0.0.1`, `localhost` or `tauri.localhost`. This blocks DNS rebinding
  attacks from web pages.
- CORS allows only the desktop app's origins (and the Vite dev server when
  `RLM_DEV` is set), without credentials. Web pages cannot read responses.
- Endpoints that change state or call the LLM take JSON bodies. A web page
  cannot send JSON cross-origin without a CORS preflight, which fails, so
  "simple" form or `no-cors` requests to these endpoints get 422. Tests cover
  this for `POST /config`.
- `GET /config` never returns API keys, only an ellipsis plus at most the last
  four characters. A masked value sent back by a client keeps the stored key.

Remaining risks:

- **Any local process can use the API**: read the text of every indexed
  document, ask questions on your API key, and change the configuration,
  including which folders are indexed. This is the main reason for the
  single-user assumption.
- A web page can still send blind requests (it cannot read the answers).
  Two endpoints accept an empty body: `POST /index/refresh` starts an
  incremental index sync, and `POST /llm/models` asks the configured provider
  for its model list. Neither returns data to the page.

Never bind the REST API to a network interface. If you need remote access,
use the MCP HTTP server.

### Windows service (LocalSystem, experimental)

The full installer and `install-backend-service.bat` run the backend as a
Windows service under LocalSystem, so it can index while no user is logged
in. Combined with the unauthenticated REST API, this means: **any local
process, including one of another standard user, can use `POST /config` to
point the indexer at a folder it cannot read itself, and then read the
extracted text through the API.** This is a local privilege escalation for
reading files. The service setup is therefore experimental: install it only
on a machine with a single user, and prefer running the backend as your own
user (the app-only installer plus `rootmind api`, or `run-backend.bat`). The
parsers also run as LocalSystem in this setup (see "Untrusted files" below).

The install step reduces what else can go wrong:

- It refuses to register the service when the executable's folder, NSSM's
  folder or, in source mode, the checkout or the Python installation can be
  written by non-administrators. Source mode only considers the checkout's
  virtual environment and Python installed for all users; per-user Python
  installations and whatever `python` is on `PATH` are never used. Source
  mode is for development only.
- It refuses a data folder that someone other than SYSTEM or Administrators
  created beforehand (standard users may create folders under
  `%ProgramData%`), or one that contains junctions or symbolic links.
- The installer hooks call PowerShell by its absolute path in `System32`, not
  by name.

### MCP over stdio

The MCP client starts `rootmind-mcp` as a child process with the client's
user rights. There is no authentication because there is no listening socket.
All five tools are read-only.

### MCP over HTTP (bearer token)

- Every MCP endpoint requires `Authorization: Bearer <token>`. Only
  `/health` is open; there are no `/docs`, `/redoc` or `/openapi.json` pages.
- It only serves by default. With `--index` it also indexes, using the
  folders from its own config.
- The token comes from `MCP_API_TOKEN` or `data/api_token.txt`. If neither
  exists, the server generates 32 random bytes (`secrets.token_urlsafe`) and
  writes them to that file. If the variable or the file is present but empty,
  the server refuses to start (fail closed).
- Tokens are compared in constant time (`secrets.compare_digest`). The server
  never logs or prints the token, only its source and an 8-character SHA-256
  fingerprint. Tests assert that the token never appears in log output.
- Requests with a browser `Origin` that is not a loopback origin (or listed in
  `MCP_ALLOWED_ORIGINS`) are rejected with 403 before the token is checked.
- It binds to `127.0.0.1` by default and warns when bound to anything else.
  It speaks plain HTTP: to reach it from another machine, put a TLS proxy or a
  TLS tunnel in front of it. Never expose the plain port.

Limits: there is one shared token, without scopes, expiry or rate limiting.
Anyone with the token can read every indexed document. To rotate it, delete
`data/api_token.txt` (or change `MCP_API_TOKEN`) and restart the server.

### Prompt injection from indexed documents

Indexed documents are untrusted input. A document can contain text such as
"ignore all previous instructions", and the model will read it. RootMind
limits what such text can achieve:

- **Read-only tools.** The agent can search, list and read. It cannot write
  files, run commands, send messages or make network requests. A successful
  injection can produce a wrong or misleading answer; it cannot make RootMind
  act.
- **Data framing.** Tool results wrap document text in `<document>` and
  `<snippet>` tags. Tag-like text inside, opening or closing and in any
  letter case, is escaped, so it cannot end the wrapper or start a new one.
  File names in result headers are reduced to one line without tags. The
  system prompt tells the model that this content is data and that
  instructions inside documents must not be followed. This reduces the risk;
  it does not remove it.
- **Budget.** At most 8 tool calls run per model turn and at most 200,000
  characters of tool output per question, so injected text cannot make one
  question read the whole index or run up an unbounded bill.
- **Scope.** Tools only return documents from the folders in the current
  configuration.
- **Rendering in the desktop app.** Answers are rendered as Markdown without
  raw HTML. The CSP blocks remote images and remote network requests, which
  closes the usual way of leaking data through a rendered answer. Links open
  only on click, only for `http`, `https` and `mailto`, and in the system
  browser. A crafted link that you click can still carry data in its URL.
- **Evals.** Eval case c15 has an injection line in its source document that
  asks the model to say all prices are now free. The live eval fails the case
  if the word "free" appears in the answer.

**MCP clients with other tools.** When you connect RootMind to an MCP client
that also has powerful tools (a shell, file writes, web access), document text
enters that client's context. An injected instruction could try to make the
client use those other tools. RootMind cannot prevent that; rely on the
client's permission prompts and do not auto-approve tool calls after reading
untrusted documents.

### File access

- Each folder connector resolves every path (following symlinks) and rejects
  anything outside the folder root, including sibling folders with the same
  prefix and symlinks that point outside. While listing a folder it does not
  descend into symbolic links or NTFS junctions.
- The tools and the REST API read text from the index, not from disk.
  `GET /documents/{id}/path`, which the desktop app uses to open a file,
  resolves the stored path through the same connector check.
- The desktop app can only open files with the extensions the backend indexes
  (a test checks this against the parser list). It cannot launch executables,
  scripts or shortcuts through the opener. The allowed types include HTML and
  Office files, which open in their default applications with those
  applications' own protections.

### Untrusted files and parsers

Every file in an indexed folder that matches the include patterns and has a
parser is parsed: PDFs with `pypdf`, Office files with `python-docx`,
`openpyxl` and `python-pptx`, and text formats (TXT, CSV, Markdown, JSON,
XML, HTML, reStructuredText) as text. Other extensions are not indexed.
A malicious file that exploits a parser bug runs code with the backend's
rights, which for the Windows service is LocalSystem. Mitigations are limited
to `max_file_size_mb` (default 50 MB) and keeping dependencies up to date.
Index only folders whose content you would open yourself.

### Desktop app

- CSP: scripts, styles and images only from the app itself (styles may also
  be inline); no plugins, `<base>` or form posts; network requests only to the
  Tauri IPC and to `localhost` and `127.0.0.1`. Tauri applies the CSP to
  production builds; `pnpm tauri dev` loads the page from the Vite server
  without it.
- Capabilities: no `core:default` set. The window may set its theme, open a
  folder picker, open web and mail links in the default apps (the Markdown
  renderer only passes `http`, `https` and `mailto`), reveal a file in
  Explorer, and open files of the indexed types. The developer tools toggle
  exists only in debug builds.
- Rust commands: the only app commands save, load, list and delete chat
  history. Every command that takes a conversation id validates it, so it
  cannot escape the history folder.

### Build and supply chain

- Installers are **not code-signed**. Windows SmartScreen will warn. Build
  from source if you cannot verify the origin of an installer; CI artifacts
  come with a `SHA256SUMS.txt`.
- NSSM is downloaded at build time and checked against a pinned SHA-256; a
  mismatch deletes the file and fails the build. It is never committed.
- Dependencies are locked (`uv.lock`, `pnpm-lock.yaml`, `Cargo.lock`) and CI
  installs from the lockfiles. Dependabot proposes monthly updates; a CI job
  scans all tracked files for secrets (detect-secrets, against the audited
  baseline `.secrets.baseline`).
- GitHub Actions are pinned by commit SHA. The workflows have read-only
  tokens, use no secrets and publish nothing. Tests never contact an LLM
  provider: real SDK clients cannot be constructed in tests, no keys are set,
  and the provider base URLs point at a closed local port.

## Out of scope

- Several users on one machine, or a backend shared between users.
- Exposing the REST API to a network, or the MCP HTTP server without TLS.
- Attackers with administrator rights or physical access to the machine.
- Content that LLM providers receive, store or log under their own terms.
- Answers that are wrong because the model misread or was misled by a
  document. Check important answers against the cited sources.
