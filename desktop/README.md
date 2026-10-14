# RootMind desktop

Tauri 2 app (React + TypeScript UI, Rust shell) for Windows and Linux, for chatting with and searching your indexed documents. It does not start a backend of its own: it is a client of the RootMind service and talks to it over its REST API. On Windows the service is a Windows service that the installer sets up; on Linux it is two systemd user services of the account, which the app sets up on its first start and updates after an update of the app (`src/state/autoSetup.ts`).

- **RootMind service** (default): the app reads the service's state and port from Windows, checks that the service's own process listens on that port, and only then reads the token from `%ProgramData%\RootMind\data\rest_token.txt`. Settings → **RootMind service** shows its state, address and version, offers **Repair** and **Update service**, and switches MCP over HTTP; each change needs administrator approval.
- **On Linux** the app reads the units `rootmind-backend.service` and `rootmind-mcp-http.service` from `~/.config/systemd/user/` and their state and main PID from `systemctl --user show`, checks the listener in `/proc`, and reads the token from `~/.local/share/rootmind/data/rest_token.txt`. Set up, Repair, Update service and the MCP switch run `service install` as the account, without a prompt (`src-tauri/src/linux/`, `unitfile.rs`, `procnet.rs`). The texts say "background service" and systemd where Windows says Windows service (the `linux` sections of the label files).
- **Custom backend**: Settings → **Advanced** connects to a backend at another address, with an optional token. **Use the RootMind service** there returns to the service.

The Rust side also stores chat history as JSON files in the app data folder. [Deployment modes](../ARCHITECTURE.md#deployment-modes) describes how the app chooses a backend, and [Interfaces between the parts](../ARCHITECTURE.md#interfaces-between-the-parts) the commands between the UI and Rust.

## Prerequisites

Node 24, pnpm 11, Rust 1.96 or newer (on Windows with the MSVC build tools) and WebView2, which ships with Windows 10/11. On Linux, WebKitGTK 4.1 and Tauri's other prerequisites (Debian and Ubuntu: `sudo apt install build-essential libwebkit2gtk-4.1-dev libxdo-dev libssl-dev libayatana-appindicator3-dev librsvg2-dev`).

## Develop

A development build never connects to the installed RootMind service on its own, because the service holds your real index and settings. It asks for a backend address instead, so run a backend from source and confirm its address (`http://127.0.0.1:8000`) in the dialog the app shows:

```bash
# terminal 1: backend, with the Vite dev origin (http://localhost:1420) allowed by CORS
cd backend
RLM_DEV=1 uv run rootmind api        # PowerShell: $env:RLM_DEV=1; uv run rootmind api

# terminal 2: desktop
cd desktop
pnpm install
pnpm tauri dev
```

To use another port, start the backend with `--port <port>` and enter that address. The address is stored as a custom backend; **Use the RootMind service** under Settings → Advanced removes it. `pnpm dev` in a plain browser (without Tauri) behaves like a development build.

### Which backend the app uses

On every start, `decideBackend()` in `src/state/bootstrap.ts` decides:

1. A stored custom backend wins.
2. A debug build (or the browser preview) asks for an address and contacts nothing until you connect. It never picks the RootMind service.
3. Otherwise, the RootMind service. Whether it works is found out by the connection check (`src/hooks/useBackend.ts`), which also explains a problem in Settings → RootMind service.

`localStorage` keeps `rlm.backendMode` (`auto` or `custom`), plus `rlm.baseUrl` and `rlm.token` only for a custom backend. Switching back to the service removes those two keys.

## Checks

These are the same checks that `.github/workflows/desktop.yml` runs (on Windows; on Ubuntu 22.04 it runs `pnpm build`, `cargo clippy` and `cargo test` for the Linux build of the Rust shell):

```bash
pnpm lint          # ESLint: JS/TS recommended rules and React hooks rules
pnpm typecheck     # tsc for the app, vite.config.ts and vitest.config.ts
pnpm test          # vitest with happy-dom: backend choice, store, client, backend dialog, connection check, stale-data guards, service section and messages, the Linux set-up, settings, labels (with the Linux wording), language, paths, folders, LLM key
pnpm build         # tsc + vite build; the Rust build embeds dist/

cd src-tauri
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test         # unit tests and the opener scope test
```

`pnpm test` needs no browser, network or Tauri. The opener scope test (`tests/opener_scope.rs`) reads `../backend/src/rootmind/indexer/parser.py`, so it needs the full repository checkout. One test of the elevation helper runs only on GitHub Actions, where no approval prompt appears; elsewhere it is skipped.

## Settings

Settings has these sections:

- **Appearance**: theme and language.
- **RootMind service**: state, address and version of the service; **Repair** when it is missing, stopped or does not answer, and **Update service** when it runs an older version than the app (both run `service install --port auto --mcp-port auto` elevated with the backend installed with the app, `%ProgramFiles%\RootMind\backend\`, so Update brings the service to the app's version; only when that is missing with the service's registered copy under `%ProgramFiles%\RootMind Service\`, and with neither the app asks to reinstall RootMind); **Details** after a failed change (`service status --json` and the end of `%ProgramData%\RootMind\logs\install.log`); the checkbox **Serve MCP over HTTP (for Claude Desktop, n8n and other agents)**, which runs `service install --mcp-http` or `--no-mcp-http` with the service's own copy under `%ProgramFiles%\RootMind Service\`, elevated; the **MCP endpoint** with **Copy**, **Copy token**, and the values for **n8n (MCP Client Tool)** with **Copy** and a link **Reach MCP from n8n**; below it **Remote access (ngrok)** (`src/screens/RemoteAccess.tsx`): the write-only ngrok authtoken, an optional reserved domain, a checkbox that asks for confirmation before it gives MCP over HTTP a public address, and the tunnel's status with the public MCP endpoint and **Copy**. It uses the REST API (`GET`/`POST /remote-access`), needs no approval and is disabled while MCP over HTTP is off.
- **LLM**: provider, model and API key.
- **Indexing**: interval, include and exclude patterns.
- **Advanced**: a backend at another address, or **Use the RootMind service**.
- **About**: app and backend versions, and links.

The Folders screen marks a folder the service cannot read ("Not accessible to the RootMind service: …").

On Linux the same section shows the user services: **Set up**, **Repair**, **Update service** and **Serve MCP over HTTP** run `service install` without a prompt; while the app sets the service up or updates it on its own, the section and the backend dialog say so, and a failure is shown there with **Details** (`service status --json` and the end of `~/.local/share/rootmind/logs/install.log`).

## Language

The UI speaks English and German. Settings → Appearance → Language offers *System* (the default), *English* and *Deutsch*; *System* follows the display language as the webview reports it (`navigator.language`), German for any German locale and English otherwise. The choice is stored per user in `localStorage` (`rlm.language`, absent for *System*) and applies at once, without a restart. All strings live in `src/i18n/labels.en.ts` and `src/i18n/labels.de.ts`, which must have identical keys; the type checker and `src/i18n/labels.test.ts` enforce it. `src/i18n/labels.ts` explains how a language change reaches every screen.

## Installer

The release installer ships the backend through the overlay `src-tauri/tauri.release.conf.json`, which maps `src-tauri/binaries/rootmind-backend/` (filled by `scripts/prepare_backend.py` in the repository root, gitignored) to `$INSTDIR\backend\`:

```bash
pnpm tauri build --config src-tauri/tauri.release.conf.json
```

Without the overlay, `pnpm tauri build` produces an app without a backend, which cannot set up the service. The installer is a per-machine NSIS installer (`installMode: perMachine`, always `%ProgramFiles%\RootMind`, uninstall entry under `HKLM`) with one administrator approval prompt, built from `src-tauri/installer/installer.nsi`: Tauri's NSIS template for the locked Tauri CLI without the reinstall page, and without the folder page when the app is installed. Running it over an installed version (older, newer or the same) replaces that version in place without questions; app data, shortcuts and the service's data stay. After an update of `@tauri-apps/cli`, refresh the template with `python scripts/nsis_template.py --write` from the repository root ([ARCHITECTURE.md](../ARCHITECTURE.md#packaging) has the details). The hooks in `src-tauri/installer/hooks.nsh`:

- refuse any install folder other than `%ProgramFiles%\RootMind` (`/D=`, or a changed install location), with a message and exit code 10, before anything changes: the service is set up from the backend in that folder, so it must be one that only administrators can change. This is the only refusal;
- decide whom the service is for: a user account that runs the installer and is the account signed in to Windows (or runs it in a session without a user, such as SSH). As SYSTEM or another service account (deployment tools, winget's validation), or as another account than the signed-in one, the service step is skipped and the app sets the service up later (**Set up**); as SYSTEM, an existing service is still updated for its account;
- close the app and programs that run from `$INSTDIR\backend\` before installing or uninstalling;
- uninstall a RootMind 1.x installation of this account (per user, `%LOCALAPPDATA%\RootMind`) silently and not elevated, only when the signed-in account runs the installer; its uninstall keeps a 1.x background service, which the installer then updates;
- remove the installed `$INSTDIR\backend\` folder before the new one is copied, so an update leaves no stale backend files;
- after copying, run `"$INSTDIR\backend\rootmind-backend.exe" service install --port auto --mcp-port auto --user-sid <installing account>` (as SYSTEM for an update: without `--user-sid`). If it is skipped or fails, the app stays installed and the installer exits with 0; the cause goes to the details list (and a message, when the installer is not silent and someone is signed in), and the uninstall entry records `RootMindServiceSetup` (`installed`, `skipped`, `failed`), `RootMindServiceSetupReason` and `RootMindServiceSetupCode`;
- on uninstall, run `service uninstall` with the service's own copy under `%ProgramFiles%\RootMind Service\`, with `--purge` when **Delete the application data** is ticked. A silent uninstall (`/S`) removes the services but never deletes data; an uninstall that is part of an update (`/UPDATE`) keeps the services.

**Linux packages.** On Linux the same command builds a .deb, an .rpm and an AppImage: `src-tauri/tauri.linux.conf.json` is merged automatically and selects them, and the `linux` section of `tauri.conf.json` adds `Recommends: systemd`; Tauri adds WebKitGTK and GTK as dependencies, the desktop entry and the icons. The backend lands in `/usr/lib/RootMind/backend/`. The packages set up no service: the app does that for each account on its first start. `scripts/prepare_backend.py` recognizes the Linux build (`rootmind-backend` without `.exe`).

The full build order is in the [README](../README.md#build-the-installer-yourself). Releases are built by `.github/workflows/release.yml`, which also runs the installer smoke test `desktop/scripts/installer-smoke.ps1` on the elevated runner. Installers are not code-signed, so Windows SmartScreen shows a warning.

## Security notes

The app renders model output, which can carry prompt injection from indexed documents, so the webview gets as little as possible:

- **CSP** (`src-tauri/tauri.conf.json`): scripts and images only from the app itself (styles may also be inline); no plugins, `<base>` or form posts; network requests only to the Tauri IPC and to `localhost`/`127.0.0.1` (the backend). Remote images in answers are blocked, which closes the usual exfiltration channel. A backend on another host would need a CSP change. Tauri applies the CSP to production builds only; `pnpm tauri dev` loads the page from the Vite server without it.
- **Markdown**: `react-markdown` without raw HTML. Links open in the system browser and only for `http`, `https` and `mailto`; the webview itself never navigates or opens a popup.
- **Capabilities** (`src-tauri/capabilities/default.json`): no `core:default` set, only the APIs the UI calls (window theme, folder picker, open link, reveal in Explorer, open file). Opening files is limited to the document types the backend indexes, so the webview cannot launch executables, scripts or shortcuts. `tests/opener_scope.rs` checks this.
- **Commands**: the UI can query and connect to the RootMind service and run a fixed set of service actions (set up, repair or update, MCP over HTTP on or off, details). Rust builds every program path and argument; the webview cannot choose them. Elevated commands go through the Windows approval prompt; on Linux they run as the account itself, without a prompt.
- **Tokens**: the service's REST token is read only after the listener check, and its MCP token only when you copy it. A custom backend's token is kept in `localStorage`.

See [SECURITY.md](../SECURITY.md) for the full threat model.
