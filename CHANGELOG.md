# Changelog

All notable changes to RootMind are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The backend and the desktop app share one version number.

## [Unreleased]

### Added

- **Windows installer with the backend included.** `RootMind-Setup-x64.exe` installs the app and its backend for the current account, without administrator rights or Python. The app starts its built-in backend on a free loopback port, with a new REST token for every start, restarts it after a crash and stops it when the app exits. Its data lives in `%LOCALAPPDATA%\io.github.cgaeking.rootmind\backend`.
- **Background service.** Settings → Background service runs RootMind as the Windows service `RootMind-Backend`, which keeps the index up to date while the app is closed and starts with Windows. It is offered to administrator accounts and needs one administrator approval. The service runs as LocalSystem, but reads only folders and files that the installing account can read without elevation; network drives and shares are not available. Optionally, the separate service `RootMind-MCP-HTTP` serves MCP over HTTP as the virtual account `NT SERVICE\RootMind-MCP-HTTP`. Turning the service on or off carries the settings over; the service builds its own index.
- `rootmind-backend.exe service install | uninstall | status` does the same from the command line. `rootmind service status` and `rootmind service uninstall` also work from a uv or pip install; `install` needs the release build.
- REST API authentication with a bearer token: `RLM_API_TOKEN`, or `rootmind api --require-token`, which creates `data/rest_token.txt`. While a token is required, every route except `GET`/`HEAD` on `/` and `/health` answers 401 without it, and `/docs`, `/redoc` and `/openapi.json` are turned off. `GET /` reports `"auth": "bearer"` or `"none"`.
- `rootmind api` options `--port 0` (a free port, announced on stdout as `ROOTMIND_LISTENING {"port":N}`), `--exit-with-parent` and `--import-config FILE`.
- `rootmind mcp` and `rootmind mcp-http` subcommands, so the packaged executable also runs both MCP servers.
- MCP over HTTP re-reads the folder list when `config.yaml` changes. Its tools follow an added or removed folder without a restart (the in-process indexer of `--index` still needs one).
- `GET /connectors` lists folders that cannot be used with `"available": false` and the reason. When the background service may not read a new folder, `POST /config` refuses it with 400; a folder that is already configured stays and is listed as unavailable.
- Desktop app: Settings → Advanced for a backend at another address (with an optional token); a one-time question when a RootMind server is found on `http://127.0.0.1:8000` on the first start; hints in an empty chat; a second launch focuses the running window.
- Release workflow: a version tag builds the backend and the installer, tests both, and publishes a GitHub Release with `SHA256SUMS.txt` and build provenance attestations. winget manifests are prepared in `packaging/winget/`, not yet submitted.
- This changelog.

### Changed

- **`rootmind api` now uses `api.host` and `api.port` from `config.yaml`.** The bind address is taken from `--host`/`--port`, then `RLM_HOST`/`RLM_PORT`, then `config.yaml`, then `127.0.0.1:8000`. Before, the command-line defaults always won, so a non-default `api` section was ignored. If your `config.yaml` sets `api.host` or `api.port`, `rootmind api` now binds there. `RLM_HOST` and `RLM_PORT` now apply to `rootmind api` as well, not only to the packaged executable started without arguments.
- The packaged backend is a PyInstaller onedir build: `rootmind-backend.exe` plus an `_internal\` folder, about 900 files. It starts faster than the former single-file executable and includes the MCP servers. The installer puts it in `%LOCALAPPDATA%\RootMind\backend\`, and the release offers it as `rootmind-backend-windows-x64.zip` for the command line and servers.
- There is one installer, and it installs per user. The desktop workflow no longer builds installers; the release workflow does.
- `MCP_API_TOKEN` and `RLM_API_TOKEN` are removed from the process environment after they have been read.

### Fixed

- Settings, Folders and Status kept showing the previous backend's data after the app switched to another backend, and saving the settings could send that data to the new backend.

### Removed

- The NSSM-based Windows service setup: `backend/deploy/windows/install-backend-service.bat`, `_service-backend.ps1` and `fetch-nssm.ps1`. NSSM is no longer downloaded or used. `backend/deploy/windows/README.md` describes how to move an existing setup to the new service.
- The per-machine "full installer" overlay `desktop/src-tauri/tauri.installer.conf.json` and its NSIS hooks. If you built and installed it, uninstall it before you turn on the new background service: its uninstaller removes any service named `RootMind-Backend` (see `backend/deploy/windows/README.md`).

### Security

- The background service replaces the former LocalSystem service, whose unauthenticated REST API let any local process read files with SYSTEM rights. The new service requires a token, confines reads to the installing account's unelevated rights, and keeps its program in an administrators-only folder. [SECURITY.md](SECURITY.md) describes the model.

## [1.1.0] - 2026-10-11

First public version of the source code. No release binaries were published.
