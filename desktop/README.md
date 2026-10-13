# RootMind desktop

Tauri 2 app (React + TypeScript UI, Rust shell) for chatting with and searching your indexed documents. It talks to the backend over its REST API and manages that backend itself:

- **Built-in backend** (default): the installer ships the backend in `$INSTDIR\backend\`, and the app starts it as a child process on a free `127.0.0.1` port with a new token for every start (a crash restart included). Its home is `%LOCALAPPDATA%\io.github.cgaeking.rootmind\backend` (settings, index, `logs\backend.log`). It stops when the app exits and is restarted after a crash.
- **Background service**: Settings → Background service installs the backend as a Windows service (one administrator approval), connects to it with the token from `%ProgramData%\RootMind\data\rest_token.txt` after checking that the service owns its port, and can also turn on MCP over HTTP.
- **Custom backend**: Settings → Advanced connects to a backend at another address, with an optional token.

The Rust side also stores chat history as JSON files in the app data folder. [Deployment modes](../ARCHITECTURE.md#deployment-modes) describes how the app chooses a backend, and [Interfaces between the parts](../ARCHITECTURE.md#interfaces-between-the-parts) the commands between the UI and Rust.

## Prerequisites

Node 24, pnpm 11, Rust 1.96 or newer (on Windows with the MSVC build tools) and WebView2, which ships with Windows 10/11.

## Develop

A development build has no bundled backend, so run one from source and confirm its address (`http://127.0.0.1:8000`) in the dialog the app shows:

```bash
# terminal 1: backend, with the Vite dev origin (http://localhost:1420) allowed by CORS
cd backend
RLM_DEV=1 uv run rootmind api        # PowerShell: $env:RLM_DEV=1; uv run rootmind api

# terminal 2: desktop
cd desktop
pnpm install
pnpm tauri dev
```

To use another port, start the backend with `--port <port>` and enter that address. The address is stored as a custom backend; **Use default backend** under Settings → Advanced removes it. `pnpm dev` in a plain browser (without Tauri) behaves like a development build without a bundled backend.

To run a development build with the bundled backend, build the backend and copy it into place (see [Build the installer yourself](../README.md#build-the-installer-yourself), steps 1 and 2), then:

```powershell
$env:RLM_HOME = "C:\path\to\scratch-folder"   # absolute; debug builds use it as the built-in backend's home
pnpm tauri dev --config src-tauri/tauri.release.conf.json
```

### Which backend the app uses

On every start, `decideBackend()` in `src/state/bootstrap.ts` decides:

1. A stored custom backend wins.
2. A debug build uses the bundled backend if it has one, otherwise it asks for an address and contacts nothing until you connect. It never picks the background service.
3. A release build uses the background service when it is installed by this app and its token is readable.
4. Otherwise, the built-in backend.

`localStorage` keeps `rlm.backendMode` (`auto` or `custom`), plus `rlm.baseUrl` and `rlm.token` only for a custom backend. Switching back to the built-in backend or the service removes those two keys.

## Checks

These are the same checks that `.github/workflows/desktop.yml` runs:

```bash
pnpm lint          # ESLint: JS/TS recommended rules and React hooks rules
pnpm typecheck     # tsc for the app, vite.config.ts and vitest.config.ts
pnpm test          # vitest with happy-dom: backend choice, store, client, stale-data guards, service messages, labels, language, paths, folders, LLM key
pnpm build         # tsc + vite build; the Rust build embeds dist/

cd src-tauri
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test         # unit tests, the opener scope test, and lifecycle tests with a fake backend process
```

`pnpm test` needs no browser, network or Tauri. The opener scope test reads `../backend/src/rootmind/indexer/parser.py`, so it needs the full repository checkout. The lifecycle tests in `tests/sidecar_lifecycle.rs` start the `fake_backend` example on `127.0.0.1:0`. One test of the elevation helper runs only on GitHub Actions, where no approval prompt appears.

## Language

The UI speaks English and German. Settings → Appearance → Language offers *System* (the default), *English* and *Deutsch*; *System* follows the Windows display language as the webview reports it (`navigator.language`), German for any German locale and English otherwise. The choice is stored per user in `localStorage` (`rlm.language`, absent for *System*) and applies at once, without a restart. All strings live in `src/i18n/labels.en.ts` and `src/i18n/labels.de.ts`, which must have identical keys; the type checker and `src/i18n/labels.test.ts` enforce it. `src/i18n/labels.ts` explains how a language change reaches every screen.

## Installer

The release installer bundles the backend through the overlay `src-tauri/tauri.release.conf.json`, which maps `src-tauri/binaries/rootmind-backend/` (filled by `scripts/prepare_backend.py` in the repository root, gitignored) to `$INSTDIR\backend\`:

```bash
pnpm tauri build --config src-tauri/tauri.release.conf.json
```

Without the overlay, `pnpm tauri build` produces an app without a backend. The installer is a per-user NSIS installer (`%LOCALAPPDATA%\RootMind` by default, no administrator rights), built from `src-tauri/installer/installer.nsi`: Tauri's NSIS template for the locked Tauri CLI without the reinstall page, and without the folder page when the app is installed. Running it over an installed version (older, newer or the same) replaces that version in place without questions; app data and shortcuts stay. After an update of `@tauri-apps/cli`, refresh the template with `python scripts/nsis_template.py --write` from the repository root ([ARCHITECTURE.md](../ARCHITECTURE.md#packaging) has the details). The hooks in `src-tauri/installer/hooks.nsh`:

- close the app and then its backend before installing or uninstalling;
- remove the installed `$INSTDIR\backend\` folder before the new one is copied, so an update leaves no stale backend files;
- on an interactive uninstall, offer to remove the background service (elevated `service uninstall` of the program under `%ProgramFiles%\RootMind Service\`);
- leave the service in place on a silent or passive uninstall (`/S`, `/P`).

The full build order is in the [README](../README.md#build-the-installer-yourself). Releases are built by `.github/workflows/release.yml`, which also runs the installer smoke test `desktop/scripts/installer-smoke.ps1`. Installers are not code-signed, so Windows SmartScreen shows a warning.

## Security notes

The app renders model output, which can carry prompt injection from indexed documents, so the webview gets as little as possible:

- **CSP** (`src-tauri/tauri.conf.json`): scripts and images only from the app itself (styles may also be inline); no plugins, `<base>` or form posts; network requests only to the Tauri IPC and to `localhost`/`127.0.0.1` (the backend). Remote images in answers are blocked, which closes the usual exfiltration channel. A backend on another host would need a CSP change. Tauri applies the CSP to production builds only; `pnpm tauri dev` loads the page from the Vite server without it.
- **Markdown**: `react-markdown` without raw HTML. Links open in the system browser and only for `http`, `https` and `mailto`; the webview itself never navigates or opens a popup.
- **Capabilities** (`src-tauri/capabilities/default.json`): no `core:default` set, only the APIs the UI calls (window theme, folder picker, open link, reveal in Explorer, open file). Opening files is limited to the document types the backend indexes, so the webview cannot launch executables, scripts or shortcuts. `tests/opener_scope.rs` checks this. The shell plugin, which starts the built-in backend, is used from Rust only and gets no permission here.
- **Commands**: the UI can start, restart and query the built-in backend and run a fixed set of background service actions. Rust builds every program path, argument and environment variable; the webview cannot choose them. Elevated commands go through the Windows approval prompt.
- **Tokens**: the built-in backend's token exists only in memory and in the backend's environment. The service's token is read only after the listener check. A custom backend's token is kept in `localStorage`.

See [SECURITY.md](../SECURITY.md) for the full threat model.
