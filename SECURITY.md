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
the desktop app and the indexed folders all belong to the same user. The REST
API and the MCP HTTP server listen on `127.0.0.1` by default.

The desktop app uses the RootMind Windows service, which requires a token on
every request, so other accounts on the same computer and web pages cannot use
it. RootMind does not protect against malware that runs under your own
account, against local administrators, or against exposing its ports to a
network. These are out of scope (see the end of this document).

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

The backend keeps its files in its home folder (`RLM_HOME`):

- the RootMind service, which the desktop app uses: `%ProgramData%\RootMind`;
- command line: `RLM_HOME`, or the working directory when it is unset.

| Data | Location | Notes |
|---|---|---|
| API keys saved through the desktop app | `config.yaml` and its backup `config.yaml.bak` in the home folder | **Plain text.** Written atomically and owner-only (0600) on Linux and macOS; on Windows the folder's permissions apply. In the service's folder the installing account, SYSTEM, administrators and, while MCP over HTTP is on, the MCP service account can read them (see [Background service](#background-service)). From the command line, prefer environment variables (`ANTHROPIC_API_KEY`, `OPENAI_API_KEY`, `XAI_API_KEY`); the service sees only machine-wide variables, which every process on the computer can read |
| Index | Service: `data\db\index.db`; command line: `data\index.db`; plus `-wal`/`-shm` | Contains the **full extracted text** of every indexed file. In the service's folder the installing account can read it |
| REST token | `data\rest_token.txt` (service, and `--require-token`), or `RLM_API_TOKEN` | See [REST API](#rest-api) |
| MCP HTTP token | `data\api_token.txt`, unless `MCP_API_TOKEN` is set | In the service's folder readable by the installing account, SYSTEM, administrators and the MCP service account (see [MCP over HTTP](#mcp-over-http-bearer-token)). Created with mode 0600 on Linux and macOS; elsewhere on Windows it inherits the folder's permissions |
| Logs | Service: `logs\backend\`, `logs\mcp-http\` and `logs\install.log`. Command line: the console | Contain file paths, questions and tool arguments. RootMind never logs API keys or tokens, only a short fingerprint of a token; provider error messages are logged as the provider sends them |
| Chat history | `%APPDATA%\io.github.cgaeking.rootmind\conversations` | Questions, answers and source paths in plain JSON |
| Custom backend address and token | The desktop app's local storage | Only when you connect to a backend under Settings → Advanced |
| Data of the RootMind 1.x built-in backend | `%LOCALAPPDATA%\io.github.cgaeking.rootmind\backend` | Left in place by the upgrade to 2.0 and no longer used: its `config.yaml` (with API keys saved in 1.x), index and logs. Delete the folder, or tick **Delete the application data** when uninstalling, which removes it with the chats |

RootMind never indexes its own data: the `data` folder, the database's folder
and, when `RLM_HOME` is set, all of `RLM_HOME` are skipped even inside an
indexed folder, and a folder inside them is refused.

## Threat model

### REST API

The REST API serves the desktop app on the same machine. Whether it requires a
token depends on how it runs:

| Mode | Token | Where the token lives |
|---|---|---|
| RootMind service (the desktop app's backend) | Always | `%ProgramData%\RootMind\data\rest_token.txt`, readable by SYSTEM, Administrators and the installing account; not by the MCP service account or other accounts. Never in the service's registry entries. The app reads it only after the listener check (see [Background service](#background-service)) and never stores it |
| Custom backend (Settings → Advanced) | Optional, entered by you | The app's local storage |
| `rootmind api` from the command line | Off, unless `RLM_API_TOKEN` is set or `--require-token` is given | The variable, or `<RLM_HOME>/data/rest_token.txt` (created if missing) |

With a token:

- Every route except `GET`/`HEAD` on `/` and `/health` requires
  `Authorization: Bearer <token>`. Missing or wrong tokens get 401. The token
  is read only from that header, never from the query string or a cookie, and
  compared in constant time (`secrets.compare_digest`).
- `/docs`, `/redoc` and `/openapi.json` are turned off. WebSocket connections
  are closed unless they carry the token.
- An empty token (variable or file) refuses to start. `RLM_API_TOKEN` is
  removed from the process environment after it has been read. Logs show only
  where the token came from and an 8-character SHA-256 fingerprint.

In every mode:

- It binds to `127.0.0.1` by default (`api.host`, `--host`). Binding to
  another address logs a warning, and requests from non-loopback addresses
  are refused with 403 regardless of the bind address.
- `TrustedHostMiddleware` rejects requests whose `Host` header is not
  `127.0.0.1`, `localhost` or `tauri.localhost`. This blocks DNS rebinding
  attacks from web pages and requests that arrive through a tunnel.
- CORS allows only the desktop app's origins (and the Vite dev server when
  `RLM_DEV` is set), without credentials. Web pages cannot read responses.
- Endpoints that change state or call the LLM take JSON bodies. A web page
  cannot send JSON cross-origin without a CORS preflight, which fails, so
  "simple" form or `no-cors` requests to these endpoints get 422. Tests cover
  this for `POST /config`.
- `GET /config` never returns API keys, only an ellipsis plus at most the last
  four characters. A masked value sent back by a client keeps the stored key.

Requests pass the checks in this order: loopback peer (403), CORS, `Host`
header (400), token (401).

Remaining risks:

- **Without a token** (`rootmind api` from the command line), any local
  process can use the API: read the text of every indexed document, ask
  questions on your API key, and change the configuration, including which
  folders are indexed. Use `--require-token` when other people or programs
  you do not trust use the same computer.
- **A token does not stop programs that run under your own account.** They
  can read the service's token file or the memory of the app.
- A web page can still send blind requests to a backend without a token (it
  cannot read the answers). Two endpoints accept an empty body:
  `POST /index/refresh` starts an incremental index sync, and
  `POST /llm/models` asks the configured provider for its model list.

Never bind the REST API to a network interface. If you need remote access,
use the MCP HTTP server.

### Background service

The RootMind service (`RootMind-Backend`) is the desktop app's backend. It
runs the REST API and the indexer as **LocalSystem**, so it can index while
no one is logged in and read the user's folders without changing their
permissions. The installer sets it up with `rootmind-backend.exe service
install`; on a machine without the app, the same command from the release zip
in an elevated prompt does it.

**Who can install it.** Setting the service up needs one administrator
approval (UAC) by the account that will use it. The installer compares the
account it runs as (from its process token) with the user of its Windows
session and sets the service up only for a user account that is the signed-in
one, or that runs it in a session without a user (SSH, a CI runner). When it
runs as SYSTEM or another service account (Intune, Configuration Manager,
winget's validation machines), or as another account than the signed-in one
(an administrator approving the prompt for a standard account, "over the
shoulder"), it installs only the app, never a service for an account it
cannot identify, records why in its uninstall entry and exits with 0. The app
then offers **Set up**, which runs `service install` elevated for the
signed-in account. Only an update run as SYSTEM may touch the service:
`service install` as LocalSystem updates our existing service for the
account it is registered for and refuses everything else (exit code 8),
including setting up a service for an account no one approved. A 1.x
installation in the user's profile is removed only when the signed-in
account runs the installer, never as SYSTEM. `service install` checks on its
own that the approving account is the account it sets the service up for,
and it never takes over a service that belongs to another account (exit
code 8): to move RootMind to another account, uninstall it and install it
from that account. A standard account cannot get the service, because its
REST API and its document parsers run as SYSTEM: a parser bug would then
lift a standard account to SYSTEM. The installer still refuses another
install folder than `%ProgramFiles%\RootMind` (exit code 10), whoever runs
it.

**Reads are confined to the account's unelevated rights.** The service reads
only what the installing account can read **without administrator rights**
(`backend/src/rootmind/winaccess.py`):

- It builds a Windows AuthZ access context from the account's SID. When User
  Account Control is on, the groups that UAC filters out of an
  administrator's standard token (Administrators, Backup Operators and the
  others Windows filters) become deny-only, as in the account's normal token.
  INTERACTIVE and LOCAL are treated as present; NETWORK, BATCH and SERVICE are
  deny-only. The service checks at startup that the filter takes effect.
- Every folder and every file is checked on its open handle: its final path
  must lie inside the indexed folder (so a junction swapped in after listing
  gains nothing), and the account must be allowed to list the folder or read
  the file. A hard link shares its file's permissions, so it gains nothing
  either. Results are cached per security descriptor.
- Refused locations: network drives and shares, device paths, paths without a
  drive letter (such as volume shadow copies), whole drives, the Windows,
  Program Files and ProgramData folders and everything below them, the folder
  that holds all user profiles, other users' profiles, folders the account
  cannot list without elevation, and folders reached through a symbolic link.
  A network path is refused before anything is opened, so adding one never
  makes the service authenticate to another computer with the computer's
  account.
- Inside an indexed folder the service never follows a link. Symbolic links,
  junctions and mount points are skipped while listing, without looking at
  their targets. A stored document path is resolved without opening it, and
  every folder on the way to a file is examined (without following it) before
  the file is used; a path through a link counts as missing. Files are opened
  without following a link at the file itself, and the handle is checked
  before anything is read. What remains is a race: a folder replaced by a
  link in the moment between its check and the open. Creating symbolic links
  needs administrator rights or Developer Mode.
- `POST /config` refuses a new folder that the policy does not allow, with
  400 and the reason. A folder that is already configured, or that becomes
  unreadable later, stays in the settings and is listed by `GET /connectors`
  with `"available": false`; the app marks it, and other settings can still
  be saved.
- Every check fails closed. If the access context cannot be built (for
  example for a domain account whose domain cannot be reached, or when the
  installing account is not a user account), `service install` exits with 9
  and the service refuses to start; it never falls back to reading as SYSTEM.

Some folders the account can open in Explorer are still refused: folders
shared only with groups that a context built from the SID does not contain
(CONSOLE LOGON, This Organization, the logon session, Microsoft or cloud
account groups), and folders with "Read" but without "Read & execute"
(traverse) permission.

**MCP over HTTP runs as its own account.** The service `RootMind-MCP-HTTP`,
on by default, runs as the virtual account `NT SERVICE\RootMind-MCP-HTTP`
with only `SeChangeNotifyPrivilege`. It serves from the index and never reads
the indexed folders. It can read `config.yaml` and `data\api_token.txt`, write
the database folder and its own logs, and nothing else in the data folder; in
particular it cannot read the REST token. `config.yaml` also holds the LLM API
keys saved through the app, so the MCP service account can read them, although
it never uses them. Turning MCP over HTTP off (Settings → RootMind service)
removes the service and the account's entries from the data folder. What the
token gives access to is described under
[MCP over HTTP](#mcp-over-http-bearer-token).

**Program files.** `service install` copies the program into a new folder,
`%ProgramFiles%\RootMind Service\<version>-<id>\`, for every install and
update: file by file, refusing links and checking the hash of each written
file. The folder is writable by SYSTEM and Administrators only, and older
version folders are deleted afterwards (files in use at the next restart). The
services always run from there, never from the app's install folder
([ARCHITECTURE.md](ARCHITECTURE.md#background-service) has the reasons). The
source of that copy is checked too: the installer installs only into
`%ProgramFiles%\RootMind` and refuses any other folder (`/D=`, or a changed
install location) with exit code 10 before anything changes, and
`service install` refuses with exit code 10 to copy from a folder that
accounts other than administrators can change, such as a download folder: no
such account may write there, or move, replace or re-permission anything in
it or in the folders above it. Their registered command lines are quoted. Elevated code takes these
paths from the Windows known-folder API, never from environment variables,
which an elevated process inherits from the user. The service host ignores
`RLM_API_TOKEN`, `MCP_API_TOKEN`, `CONFIG_PATH`, `RLM_DEV`, `RLM_HOST` and
`RLM_PORT`, refuses a database outside its `data` folder, and gives the
backend service a temporary folder that only SYSTEM and Administrators can
access.

**Data folder.** `%ProgramData%\RootMind` (`SY` SYSTEM, `BA` Administrators,
`U` the installing account, `M` the MCP service account, only while MCP over
HTTP is on):

```
RootMind\              owner BA, protected: SY full, BA full, U read, M read (this folder and its files)
  config.yaml          inherited (M may read it: folder list and database path)
  data\                inherited, M: nothing
    rest_token.txt     inherited (M: nothing)
    api_token.txt      inherited + M read
    db\                inherited + M modify (index.db, -wal, -shm)
  logs\                inherited
    backend\           service log
    mcp-http\          inherited + M modify
    install.log        results of install, update and uninstall
  tmp\                 protected: SY full, BA full
```

A missing folder is created with these permissions in one step. An existing
one, for example one kept by an uninstall without **Delete the application
data**, is reused only when every item in it is owned by SYSTEM or
Administrators, grants write access to no other account (except M on its two
folders), and contains no reparse points or hard-linked files. An update stops
both services before it checks the folder, and the permissions are written
through a handle of each item, never through a link that replaced it. Files
that `service install` creates are owned by Administrators, also for an
administrator with User Account Control turned off. Otherwise `service
install` exits with 6: "%ProgramData%\RootMind exists but is not protected.
Remove or rename it from an elevated prompt, then try again." A folder
created by hand inherits "Users: create" from ProgramData and is refused.

**Elevated commands from the app.** The webview can call only a fixed set of
commands; the Rust side builds every program path and argument. Set up,
Repair and "Update service" elevate `service install --user-sid <the signed-in
account>` with the backend installed with the app, `%ProgramFiles%\RootMind\backend\`; only when that is missing do they use the
service's registered copy under `%ProgramFiles%\RootMind Service\`. Switching MCP
over HTTP on or off elevates the registered copy, after checking that the
registered path lies under `%ProgramFiles%\RootMind Service\`. Removing the
service is the uninstaller's job; the app sets it up only when the installer
did not (Set up).

**Token handoff.** Before the app reads the service's token, it checks that
the process that listens on the service's port is the service itself: the
process ID from the Service Control Manager must equal the owner of the
listening socket. A program of another account (or forwarded from WSL) that
takes over the port never receives the token. The check is repeated on every
connection check (every 5 seconds, paused while a question is being
answered). If the service crashes and another program binds its port before
the next check, requests made in that window can still carry the token.
`service install` makes the same check before its own test requests with the
REST and MCP tokens: if another program listens on the port, it stops with
exit code 7 and sends no token.

**Approving the prompts.** Every program the app or the installer runs as
administrator lies in a folder that only administrators can change: the
app's backend in `%ProgramFiles%\RootMind\backend\` (the installer accepts no
other folder) or the service's copy under `%ProgramFiles%\RootMind Service\`.
Malware running as your account therefore cannot replace it before you
approve, and `service install` refuses to copy from a folder that other
accounts can change (exit 10). The prompts show an unsigned
program, so approve one only when you started the action yourself.

**Document parsers run as SYSTEM** in the service, on files the account can
read. A malicious file that exploits a parser bug would run code as SYSTEM.
Index only folders whose content you would open yourself.

**Uninstall and purge.** Uninstalling RootMind (Windows Settings → Apps) runs
`service uninstall` with the service's own copy: it stops and deletes both
services, deletes the program folders (files in use at the next restart) and
`data\rest_token.txt`. It deletes anything in the data folder only when the
folder passes the same check as for a reinstall; otherwise it leaves the
folder untouched and says so. Without **Delete the application data**, and
always for a silent uninstall (`/S`), the data folder stays: `config.yaml`
(with saved API keys), the index, the logs and the MCP token, so MCP clients
keep working after a reinstall. With the box ticked, `service uninstall
--purge` deletes `%ProgramData%\RootMind`, and the uninstaller also deletes
the app data (chats) of the account that runs it. To delete the data later,
remove `%ProgramData%\RootMind` from an elevated prompt. An uninstall that is
part of an update (`/UPDATE`) keeps the service. If the services cannot be
removed, the uninstaller names the command to run from an elevated prompt,
and `service status` warns while RootMind is no longer installed. To rotate a
token, delete its file from an elevated prompt and run `service install`
again.

### MCP over stdio

The MCP client starts `rootmind-mcp` (or `rootmind-backend.exe mcp`) as a
child process with the client's user rights. There is no authentication
because there is no listening socket. All five tools are read-only. The
stdio server serves the database its own `config.yaml` names (a source or uv
setup); it cannot open the service's index, whose folder the account can only
read.

### MCP over HTTP (bearer token)

The RootMind service serves MCP over HTTP **by default**: the service
`RootMind-MCP-HTTP` listens on `127.0.0.1`, port 3000 or one from
18400–18499. Its token is in `%ProgramData%\RootMind\data\api_token.txt`,
which the installing account, SYSTEM, Administrators and the MCP service
account can read. So every program that runs as the installing account, and
every administrator, can read the token and through it every indexed
document. That is the same group that can already read the REST token and the
index file itself. Other accounts can connect to the port, but without the
token they get 401. The token stays the same across updates and across an
uninstall that keeps the data, and copies of it live in the MCP clients you
set up (for example n8n credentials or the Claude Code configuration). If you
do not use MCP, turn it off under Settings → RootMind service.

The Claude Desktop bundle (`rootmind.mcpb`) keeps no copy: it runs as the
signed-in account, reads the token file named by `service status --json` at
start and after a connection error, and sends the token only in the
`Authorization` header to `127.0.0.1`. It never writes the token to its output
or its log (stderr); tests assert both. Unlike the app, it does not check
which process owns the port (Node.js has no API for that); it sends the token
only to the port that `service status` reports for the running MCP service.

- Every MCP endpoint requires `Authorization: Bearer <token>`. Only
  `/health` is open; there are no `/docs`, `/redoc` or `/openapi.json` pages.
- It only serves by default. With `--index` it also indexes, using the
  folders from its own config. It re-reads the folder list when `config.yaml`
  changes.
- The token comes from `MCP_API_TOKEN` or `data/api_token.txt`. If neither
  exists, the server generates 32 random bytes (`secrets.token_urlsafe`) and
  writes them to that file. If the variable or the file is present but empty,
  the server refuses to start (fail closed). The service never generates it:
  `service install` writes it, or takes it from `--mcp-token-from FILE`.
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
`data/api_token.txt` (or change `MCP_API_TOKEN`) and restart the server; for
the RootMind service, see [Background service](#background-service).

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

- Each folder connector resolves every path and rejects anything outside the
  folder root, including sibling folders with the same prefix and symlinks
  that point outside. While listing a folder it does not descend into
  symbolic links or NTFS junctions. In the background service, paths are
  resolved without following any link, links are skipped without being
  followed, and the folder access policy above checks every folder and file
  as well.
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
rights: your own on the command line, LocalSystem in the RootMind service. Mitigations are limited to `max_file_size_mb`
(default 50 MB) and keeping dependencies up to date. Index only folders whose
content you would open yourself.

### Desktop app

- CSP: scripts, styles and images only from the app itself (styles may also
  be inline); no plugins, `<base>` or form posts; network requests only to the
  Tauri IPC and to `localhost` and `127.0.0.1`. Tauri applies the CSP to
  production builds; `pnpm tauri dev` loads the page from the Vite server
  without it.
- Capabilities: no `core:default` set. The window may set its theme, open a
  folder picker, open web and mail links in the default apps (the Markdown
  renderer only passes `http`, `https` and `mailto`), reveal a file in
  Explorer, and open files of the indexed types. The app has no shell
  plugin, and the webview has no permission to start processes. The
  developer tools toggle exists only in debug builds.
- Rust commands: chat history (save, load, list, delete; every command that
  takes a conversation id validates it, so it cannot escape the history
  folder) and the RootMind service (state, connect, repair or update, MCP
  over HTTP on or off, MCP token, details). No command takes a program path,
  arguments or environment variables from the webview. The app starts the
  backend program from its install folder only for `service status --json`
  (not elevated) and for the elevated `service install` of Repair and
  "Update service"; switching MCP over HTTP runs the service's registered
  copy.
- The app starts no backend of its own. A debug build never connects to the
  installed service on its own; it asks for a backend address.

### Build and supply chain

- Releases are built by `.github/workflows/release.yml` from a version tag,
  without caches. The jobs that build and test have read-only tokens; one
  final job with write access packs and publishes what they built. Pull
  requests and manual runs are dry runs with read-only tokens and no
  secrets.
- Every release asset is listed in `SHA256SUMS.txt` and has a build
  provenance attestation (`gh attestation verify <file> --repo
  cgaeking/rootmind`). The installer contains exactly the backend build that
  passed the smoke and service tests; its tree hash is checked before and
  after packaging. With GitHub's immutable releases turned on (see
  [CONTRIBUTING.md](CONTRIBUTING.md#releasing)), published assets cannot be
  replaced.
- The binaries are **not code-signed** yet. Windows SmartScreen will warn, and
  Smart App Control may block them. Verify the checksum and the attestation,
  or build from source.
- Dependencies are locked (`uv.lock`, `pnpm-lock.yaml`, `Cargo.lock`) and CI
  installs from the lockfiles; the release build fails if `Cargo.lock` does
  not match. The Rust compiler version is pinned in the workflows as well.
  Dependabot proposes monthly updates; a CI job
  scans all tracked files for secrets (detect-secrets, against the audited
  baseline `.secrets.baseline`).
- GitHub Actions are pinned by commit SHA and check out without persisted
  credentials. Tests never contact an LLM provider: real SDK clients cannot be
  constructed in tests, no keys are set, and the provider base URLs point at
  a closed local port.

## Out of scope

- Several users sharing one backend. The RootMind service belongs to the
  account that installed it.
- Exposing the REST API to a network, or the MCP HTTP server without TLS.
- Malware running under your own account, attackers with administrator
  rights, and physical access to the machine.
- Content that LLM providers receive, store or log under their own terms.
- Answers that are wrong because the model misread or was misled by a
  document. Check important answers against the cited sources.
