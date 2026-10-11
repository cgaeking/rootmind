# RootMind desktop

Tauri 2 app (React + TypeScript UI, thin Rust shell) for chatting with and searching your indexed documents. It is a pure client of the backend REST API (`http://127.0.0.1:8000` by default) and never starts or manages the backend itself. The Rust side only stores chat history as JSON files in the app data folder.

## Prerequisites

Node 24, pnpm 11, Rust stable (on Windows with the MSVC build tools) and WebView2, which ships with Windows 10/11.

## Develop

```bash
# terminal 1: backend, with the Vite dev origin (http://localhost:1420) allowed by CORS
cd backend
RLM_DEV=1 uv run rootmind api        # PowerShell: $env:RLM_DEV=1; uv run rootmind api

# terminal 2: desktop
cd desktop
pnpm install
pnpm tauri dev
```

## Checks

These are the same checks that `.github/workflows/desktop.yml` runs:

```bash
pnpm lint          # ESLint: JS/TS recommended rules and React hooks rules
pnpm typecheck     # tsc for the app and for vite.config.ts
pnpm build         # tsc + vite build; the Rust build embeds dist/

cd src-tauri
cargo fmt --check
cargo clippy --all-targets -- -D warnings
cargo test         # safe_id unit tests and the opener scope test (tests/opener_scope.rs)
```

The opener scope test reads `../backend/src/rootmind/indexer/parser.py`, so it needs the full repository checkout.

## Language

The UI is English by default. Build the German variant with `VITE_APP_LANG=de` (PowerShell: `$env:VITE_APP_LANG='de'`). All strings live in `src/i18n/labels.en.ts` and `src/i18n/labels.de.ts`, which must have identical keys.

## Installers

`pnpm tauri build` produces an app-only, per-user NSIS installer; run the backend yourself next to it. The full installer also bundles the backend exe and registers it as a Windows service that runs as LocalSystem. It is **experimental** and only for single-user machines (see [SECURITY.md](../SECURITY.md#windows-service-localsystem-experimental)). It is built with an overlay config:

```bash
pnpm tauri build --config src-tauri/tauri.installer.conf.json
```

The overlay expects `backend/dist/rootmind-backend.exe` and `backend/deploy/windows/tools/nssm.exe` to exist. See [ARCHITECTURE.md](../ARCHITECTURE.md#packaging) for the full build order. Installers are not code-signed, so Windows SmartScreen shows a warning.

On version tags (`v*`) and on manual runs, the desktop workflow also builds the app-only installer and keeps it, with a `SHA256SUMS.txt`, as a workflow artifact. It does not publish anything.

## Security notes

The app renders model output, which can carry prompt injection from indexed documents, so the webview gets as little as possible:

- **CSP** (`src-tauri/tauri.conf.json`): scripts and images only from the app itself (styles may also be inline); no plugins, `<base>` or form posts; network requests only to the Tauri IPC and to `localhost`/`127.0.0.1` (the backend). Remote images in answers are blocked, which closes the usual exfiltration channel. A backend on another host would need a CSP change. Tauri applies the CSP to production builds only; `pnpm tauri dev` loads the page from the Vite server without it.
- **Markdown**: `react-markdown` without raw HTML. Links open in the system browser and only for `http`, `https` and `mailto`; the webview itself never navigates or opens a popup.
- **Capabilities** (`src-tauri/capabilities/default.json`): no `core:default` set, only the APIs the UI calls (window theme, folder picker, open link, reveal in Explorer, open file). Opening files is limited to the document types the backend indexes, so the webview cannot launch executables, scripts or shortcuts. `tests/opener_scope.rs` checks this.

See [SECURITY.md](../SECURITY.md) for the full threat model.
