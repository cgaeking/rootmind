# Contributing

Thanks for taking a look. RootMind is a personal project maintained on a
best-effort basis, so small, focused pull requests are easiest to review.
Please open an issue first for larger changes. Report security problems
privately, as described in [SECURITY.md](SECURITY.md).

## Development setup

Prerequisites:

- Backend: [uv](https://docs.astral.sh/uv/) (installs Python 3.12 if needed).
- Desktop: Node 24, pnpm 11 (the version is pinned in `package.json`), Rust
  1.96 or newer, and on Windows the MSVC build tools and WebView2. On Linux,
  Tauri's prerequisites: on Debian and Ubuntu
  `sudo apt install build-essential libwebkit2gtk-4.1-dev libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev`
  (other distributions: see Tauri's prerequisites for WebKitGTK 4.1). CI
  builds and tests the desktop app on Windows and its Rust shell on
  Ubuntu 22.04.

Backend:

```bash
cd backend
uv sync
uv run ruff check .
uv run ruff format .
uv run pytest -q
uv run pytest ../scripts/tests -q         # release scripts: versions, notes, backend copy, winget, NSIS template
uv run python scripts/run_evals.py        # retrieval regression (dry evals), offline
python ../scripts/check_versions.py       # backend, desktop and bundle versions match
```

The Claude Desktop bundle in `packaging/mcpb/` has its own tests (Node 24,
standard library only) and is validated with the mcpb CLI; see
[packaging/mcpb/README.md](packaging/mcpb/README.md):

```bash
node --test "packaging/mcpb/test/*.test.mjs"
npx --yes @anthropic-ai/mcpb@2.1.2 validate packaging/mcpb/manifest.json
```

The backend CLI has the commands `api`, `index`, `rebuild-trigrams`, `mcp`,
`mcp-http` and `service`, and the client commands of the running service
(`status`, `folders`, `search`, `ask`, `config`, `mcp tokens`,
`remote-access`); `uv run rootmind <command> --help` describes each. The client
commands' tests (`tests/test_client_cli.py`) run against a fake REST API
(`tests/fake_rest.py`) and a fake service probe; the `no_real_service` fixture
makes any lookup of the machine's own service fail, so a test never reads the
token of a RootMind service you use. Do not run `rootmind status` and the other
client commands from a development checkout against your own service while
debugging unless you mean to change it.

Desktop:

```bash
cd desktop
pnpm install
pnpm lint
pnpm typecheck
pnpm test          # vitest with happy-dom; no browser, network or Tauri needed
pnpm build
cd src-tauri
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test         # unit tests and the opener scope test
```

CI runs the same commands (`.github/workflows/backend.yml` and
`.github/workflows/desktop.yml`). The backend tests run on Windows and Linux;
tests of Windows permissions, and of Linux file modes, are skipped on the
other system, while the install logic of both platforms runs everywhere with
fake service managers.

### Running the desktop app during development

A development build (`pnpm tauri dev`) never connects to the RootMind service,
which holds your real index and settings, and contacts no backend on its own:
the app shows the backend dialog, which suggests `http://127.0.0.1:8000`. Start
a backend from source with the Vite dev origin allowed, then confirm its
address there:

```bash
cd backend
RLM_DEV=1 uv run rootmind api                 # PowerShell: $env:RLM_DEV=1; uv run rootmind api
RLM_DEV=1 uv run rootmind api --port 18000    # if 8000 is taken; enter http://127.0.0.1:18000
```

The address you connect to is stored as a custom backend; **Use the RootMind
service** under Settings → Advanced removes it again. `pnpm dev` in a plain
browser behaves the same way.

The connection to the service, **Repair** and the MCP over HTTP switch only
work in a release build installed by the installer (see
[Build the installer yourself](README.md#build-the-installer-yourself)). Try
them in a virtual machine or Windows Sandbox, not on a machine where you use
RootMind. On Linux the same goes for the release packages: a release build
sets up the systemd user services of the account that starts it, so try it
under a test account or in a virtual machine.

### Ports for local testing

- Unit tests use FastAPI's `TestClient` or port 0; they never need a free
  port.
- The smoke test of a backend build uses 18765 and 18766 by default, and the
  service end-to-end tests in CI (Windows and Linux) 18080 and 18081. The installer smoke test
  lets the installer choose the ports, as it does for users: 8000 and 3000
  when free, otherwise 18300–18399 and 18400–18499.
- Do not point tests or a development build at a port where a RootMind
  backend you use every day is listening, such as the RootMind service on
  8000.

### Packaged backend, service and installer

```powershell
cd backend
uv sync --locked --no-dev --group build
uv run --no-sync pyinstaller packaging/rootmind-backend.spec --noconfirm --clean --distpath dist
powershell -File packaging/smoke-test.ps1 -ExeDir dist/rootmind-backend -ApiPort 18765 -McpPort 18766 -WorkDir <scratch folder>
uv sync                                   # back to the development environment
```

The smoke test works with Windows PowerShell 5.1 and PowerShell 7 and writes
only below `-WorkDir`. The same commands build the Linux backend on Linux
(`pwsh -File packaging/smoke-test.ps1 ...` with PowerShell 7). Build it on the
oldest glibc you want to support (CI and releases use Ubuntu 22.04); the spec
replaces the symbolic links PyInstaller creates there with copies.

Linux packages, with the Linux backend build in `backend/dist/`:

```bash
python scripts/prepare_backend.py           # copies and checks the build (rootmind-backend, no .exe)
cd desktop
pnpm tauri build --config src-tauri/tauri.release.conf.json
# -> src-tauri/target/release/bundle/{deb,rpm,appimage}/
```

`tauri.linux.conf.json` is merged automatically on Linux and selects the three
formats; building the AppImage downloads linuxdeploy from GitHub.

Installing the Windows services, the approval prompts and the installer are
tested in CI on GitHub's Windows runners, which run elevated. Both scripts
change the machine and need an elevated process:

- `backend/tests/e2e/service_e2e.ps1` installs, updates and removes the
  services with a backend build, including the refusals, the folder
  permissions, the tokens and switching MCP over HTTP off and on. It refuses
  to run unless the process is elevated and `CI=true` is set (or `-Force` is
  given, for throwaway machines), and unless no RootMind service or data
  folder exists.
- `desktop/scripts/installer-smoke.ps1` installs the per-machine installer
  silently, checks the services it sets up, updates and reinstalls over it
  and uninstalls it. Then it installs it silently as SYSTEM through a one-off
  scheduled task (the app without a service, the skipped service step
  recorded in the uninstall entry), sets the service up for the runner
  account with `service install` as the app's Set up does, updates it as
  SYSTEM and uninstalls again. It refuses unless `CI=true` or `-Force` and an elevated
  process, and whenever RootMind is installed (per machine, or 1.x for the
  current account), a RootMind service exists, or one of these folders
  exists: `%ProgramFiles%\RootMind`, `%ProgramFiles%\RootMind Service`,
  `%ProgramData%\RootMind`, or the app data folder
  `%LOCALAPPDATA%\<identifier>` named by `-Identifier`, which release.yml
  takes from `tauri.conf.json`. It does not cover an installer approved by
  another account than the signed-in one (the runner has a single account).

On your own machine, `rootmind service status` and an unelevated
`rootmind service install` (which stops before it changes anything) are safe
to run. For the rest, use a virtual machine or Windows Sandbox.

On Linux, `backend/tests/e2e/service_e2e_linux.py` installs, checks, updates
and removes the systemd user units of the account that runs it with a Linux
backend build (refusals, file modes, tokens, indexing, restarts, MCP over
HTTP off and on, the command line, named MCP tokens and the network
listener, uninstall). It needs a running user manager for the account
(CI: `sudo loginctl enable-linger "$USER"` and `XDG_RUNTIME_DIR`), refuses to
run as root, unless `CI=true` (or `--force`), and when
`~/.local/share/rootmind` or a `rootmind-*` unit exists. A container with
systemd as PID 1 and a lingering test account works too.

## Rules

- **English** for code, comments, docstrings, logs and docs. UI strings live in
  `desktop/src/i18n/labels.en.ts` and `labels.de.ts`, which must have the same
  keys.
- **Tests never call a real LLM or the network.** Real Anthropic and OpenAI
  clients cannot be constructed inside the test suite. Pass a scripted fake
  from `backend/tests/fakes.py` to the engine instead. Use obviously fake,
  low-entropy test keys and tokens.
- **Never commit** `config.yaml`, `.env`, `data/`, `logs/`, API keys, tokens or
  real documents. Test and eval data must be fictional; the eval corpus is
  generated by `backend/scripts/make_demo_corpus.py`. CI scans all tracked
  files with detect-secrets. Mark a fake test key with
  `# pragma: allowlist secret`; to accept another false positive, run
  `uvx --from detect-secrets==1.5.0 detect-secrets scan --baseline .secrets.baseline`
  and then `detect-secrets audit .secrets.baseline` the same way.
- **Style:** ruff for Python (line length 100), ESLint and `tsc` for
  TypeScript, rustfmt and clippy for Rust. `.editorconfig` covers the rest.
- **Commits:** [Conventional Commits](https://www.conventionalcommits.org/)
  (`feat:`, `fix:`, `docs:`, `test:`, ...).
- **Bug fixes come with a regression test.**
- **Changelog:** add user-visible changes to the `## [Unreleased]` section of
  [CHANGELOG.md](CHANGELOG.md).
- **Versions** change only in the release commit (see [Releasing](#releasing)).

## Common changes

**Add a tool.** Add its schema to `TOOLS` in `backend/src/rootmind/tools.py`
and its function to `_EXECUTORS`; an import-time check fails if the two
disagree. Validate arguments with the `_int_arg`/`_str_arg`/`_float_arg`
helpers and raise `ToolInputError` for bad input. Keep tools read-only. Both
MCP servers pick the tool up automatically, and `test_mcp_stdio.py` checks
that all surfaces expose the same tools. Mention the tool in `SYSTEM_PROMPT`
in `rlm_engine/engine.py` and add tests to `tests/test_tools.py`.

**Add a file type.** Add a parser to `DocumentParser.PARSERS` in
`backend/src/rootmind/indexer/parser.py`. If the format has pages, slides or
sheets, emit the same markers as the existing parsers so locations work. Add
the extension to the opener scope in
`desktop/src-tauri/capabilities/default.json`; the Rust test
`tests/opener_scope.rs` fails until you do. Consider adding it to the default
`include` patterns in `config.py` and `config.example.yaml`.

**Add a connector.** Subclass `BaseConnector` in
`backend/src/rootmind/connectors/` and register the type in
`create_connectors` in `api/app.py`. A connector must keep every path inside
its root, read through the folder access policy it is given
(`backend/src/rootmind/access.py`), and under a policy never follow a link
itself (no `Path.resolve()`, `stat()` or `os.walk` on paths below the root),
as `LocalConnector` does; see
`LocalConnector._resolve_path`, `tests/test_connector_confinement.py` and
`tests/test_connector_access.py`.

**Add a REST route.** `tests/test_api_auth.py` calls every route without a
token and expects 401, except for `/` and `/health`. A new route is covered
automatically; do not add it to the open list.

**Change the eval corpus.** Edit `scripts/make_demo_corpus.py`, regenerate
with `uv run python scripts/make_demo_corpus.py`, and update
`evals/dataset.json`. `tests/test_corpus_current.py` fails when the committed
files and the generator disagree.

**Update the Tauri CLI.** The installer is built from
`desktop/src-tauri/installer/installer.nsi`, Tauri's NSIS template for the
locked `@tauri-apps/cli` with RootMind's changes (no reinstall page, no folder
page for an update). After an update of `@tauri-apps/cli`, Dependabot's
included, run `python scripts/nsis_template.py --write` (needs network
access) and commit the result to the same pull request;
`scripts/tests/test_nsis_template.py` fails until then. The script refuses
when the upstream template changed where RootMind's changes apply; adapt its
anchors in that case.

## Inspecting the index

The index is a plain SQLite file (`data/index.db` under `RLM_HOME` or the
working directory). It contains the full text of your documents, so do not
share it.

```sql
-- documents by status
SELECT status, COUNT(*) FROM documents GROUP BY status;

-- files that failed to parse, and why
SELECT file_path, error_message FROM documents WHERE status = 'error';

-- largest documents by extracted text
SELECT file_name, content_length FROM documents ORDER BY content_length DESC LIMIT 10;

-- try an FTS5 query with highlighted snippets
SELECT doc_id, snippet(documents_fts, 2, '>>>', '<<<', '...', 30)
FROM documents_fts WHERE documents_fts MATCH '"invoice"';
```

Tool calls are logged at INFO level. `uv run rootmind --debug api` adds debug
output, such as skipped unchanged files and FTS query strategies that SQLite
rejected.

## Releasing

A tag `vX.Y.Z` makes `.github/workflows/release.yml` build the backend and
the installer, test both, render the winget manifests and publish a GitHub
Release with `RootMind-Setup-x64.exe`, the Linux packages
`RootMind-amd64.deb`, `RootMind-x86_64.rpm` and `RootMind-x86_64.AppImage`,
`rootmind.mcpb` (Claude Desktop), `rootmind-backend-windows-x64.zip`,
`rootmind-backend-linux-x64.tar.gz`, `SHA256SUMS.txt` and attestations. The release notes are the version's
section of `CHANGELOG.md` plus a fixed part about the downloads and how to
verify them.

1. **Versions.** Set the new version in `backend/pyproject.toml`,
   `desktop/package.json`, `desktop/src-tauri/Cargo.toml` and
   `packaging/mcpb/manifest.json`, then run
   `uv lock` in `backend/` and `cargo check` in `desktop/src-tauri/` so the
   lockfiles follow. For a prerelease, write `1.2.0rc1` in `pyproject.toml`
   and `1.2.0-rc.1` in the other three files; the tag is then `v1.2.0-rc.1`,
   spelled exactly like that, and the release is marked as a prerelease.
   Update the tag in the README's `uv tool install` line as well.
2. **Changelog.** Turn `## [Unreleased]` into `## [X.Y.Z] - YYYY-MM-DD` and
   start a new, empty `## [Unreleased]` above it. For a prerelease, use the
   prerelease version in the heading, for example `## [1.2.0-rc.1] - YYYY-MM-DD`.
3. **Check locally:**

   ```bash
   python scripts/check_versions.py --tag vX.Y.Z
   python scripts/release_notes.py vX.Y.Z      # prints the release notes
   ```

4. **Dry run.** Push the release commit to a branch and run the workflow
   manually: `gh workflow run release.yml --ref <branch>` (or Actions →
   release → Run workflow). Every job except "Publish the release" must pass.
   The run's summary shows the release notes, the installer's size and its
   SHA-256; the artifacts are `rootmind-installer-windows-x64`,
   `rootmind-backend-windows-x64`, `rootmind-backend-linux-x64`,
   `rootmind-linux-packages`, `winget-manifests` and `rootmind-mcpb`.
5. **Manual checks** in Windows Sandbox or a throwaway virtual machine, with
   the installer from the dry run:
   - a first install by double-click, including the SmartScreen warning and
     the administrator prompt, and cancelling that prompt; afterwards the app
     connects to the service and Settings → **RootMind service** shows it
     running;
   - a standard account signed in and an administrator approving the prompt:
     the installer installs the app, says that the service will be set up
     from the app, exits with 0, and the uninstall entry records
     `RootMindServiceSetup = skipped` with the reason; the standard account's
     app then shows "The RootMind service is not set up yet" with the note
     that an administrator account is needed, and no **Set up**;
   - a silent install as SYSTEM (`psexec -s` or a scheduled task) while an
     administrator account is signed in: exit code 0, no message, no service;
     that account opens RootMind, sees "The RootMind service is not set up
     yet", chooses **Set up**, approves the prompt, and the app connects;
   - an upgrade over RootMind 1.x installed for the same account, once with
     and once without the 1.x background service: the 1.x installation is
     gone, chats stay, and a 1.x service keeps its folders and index;
   - running the installer by double-click over an older build, and over the
     same build: Welcome, Installing, Finish, with no "Already Installed" page
     and no folder page; chats, settings and shortcuts stay, and the service
     is updated and keeps its ports and tokens;
   - **Repair** after `Stop-Service RootMind-Backend` (the approval prompt
     names `%ProgramFiles%\RootMind\backend\rootmind-backend.exe`), and
     **Update service** when the service runs an older version than the app:
     afterwards it runs the app's version;
   - the installer with `/D=C:\RootMind`: it stops with exit code 10 and
     changes nothing;
   - **Serve MCP over HTTP** off and on again, including cancelling the
     approval prompt;
   - **Remote access (ngrok)** with a real ngrok account (CI has none): save
     the authtoken and a reserved domain, turn it on and confirm; the status
     shows **Connected** and the public MCP endpoint, which answers
     `initialize` with the MCP token and 401 without it; a wrong authtoken
     and a domain in use elsewhere show ngrok's error; unticking closes the
     public address within seconds; it comes back after a restart of the
     computer and after an update; turning **Serve MCP over HTTP** off turns
     it off; no log under `%ProgramData%\RootMind\logs` contains the
     authtoken;
   - installing from an administrator account with User Account Control
     turned off;
   - Claude Code and n8n with the **MCP endpoint** and token from Settings,
     once with the app running and once with it closed: search results
     follow a change in an indexed folder;
   - uninstalling from Windows Settings → Apps, interactively with and
     without **Delete the application data**, and with `/S`: the services are
     removed, and `%ProgramData%\RootMind` is deleted only when the box was
     ticked;
   - on Linux desktops (a virtual machine or a test account; for example
     Ubuntu 24.04 with GNOME and Fedora with KDE): the .deb, the .rpm and the
     AppImage start, the first start sets up the user services without a
     question and the app connects; Settings → **RootMind service** shows
     them running, **Serve MCP over HTTP** switches without a prompt; after
     installing a newer package the app updates the service on its own;
     `systemctl --user stop rootmind-backend` leads to **Repair**; the
     AppImage keeps the service running after it is closed and deleted;
     `loginctl enable-linger` keeps the services running after logout; the
     uninstall steps in the README leave no unit behind.
6. **Tag** the merged release commit on `main` and push the tag:

   ```bash
   git tag vX.Y.Z
   git push origin vX.Y.Z
   ```

   The first job fails early if `CHANGELOG.md` has no section for the version.

**winget.** The manifests are prepared in `packaging/winget/` and rendered by
every release run, but RootMind is not submitted to the winget repository
yet; the first submission is planned once the binaries are code-signed.
[packaging/winget/README.md](packaging/winget/README.md) has the submission
steps. Do not add `winget install` to the README before the first submission
is merged.

**One-time repository settings** (GitHub web UI):

- turn on release immutability, so published assets cannot be replaced;
- add a tag ruleset for `v*`, so only maintainers can create or move release
  tags;
- optionally, turn on Dependabot security updates.
