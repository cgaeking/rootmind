# Changelog

All notable changes to RootMind are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The backend and the desktop app share one version number.

## [Unreleased]

## [1.2.0] - 2026-10-12

### Added

- **Windows installer with the backend included.** `RootMind-Setup-x64.exe` installs the app and its backend for the current account, without administrator rights or Python. The app starts its built-in backend on a free loopback port, with a new REST token for every start, restarts it after a crash and stops it when the app exits. Its data lives in `%LOCALAPPDATA%\io.github.cgaeking.rootmind\backend`.
- **Updates without questions.** Running the installer over an installed version (older, newer or the same) replaces it in place: no "already installed" page and no choice of folder. Settings, index, chats and shortcuts are kept, and no files of the replaced backend are left behind. The installer only asks to close RootMind while it is running.
- **Backend for the command line and servers.** `rootmind-backend-windows-x64.zip` holds the backend on its own, the same build that the installer puts in `%LOCALAPPDATA%\RootMind\backend\`: a PyInstaller onedir build, `rootmind-backend.exe` plus an `_internal\` folder of about 900 files, with both MCP servers included. It runs without the app and installs the background service.
- **Background service.** Settings → Background service runs RootMind as the Windows service `RootMind-Backend`, which keeps the index up to date while the app is closed and starts with Windows. It is optional, offered to administrator accounts and needs one administrator approval. The service runs as LocalSystem, but reads only folders and files that the installing account can read without elevation; network drives and shares are not available. Optionally, the separate service `RootMind-MCP-HTTP` serves MCP over HTTP as the virtual account `NT SERVICE\RootMind-MCP-HTTP`. Turning the service on or off carries the settings over; the service builds its own index.
- `rootmind-backend.exe service install | uninstall | status` does the same from the command line. `rootmind service status` and `rootmind service uninstall` also work from a uv or pip install; `install` needs the release build.
- REST API authentication with a bearer token: `RLM_API_TOKEN`, or `rootmind api --require-token`, which creates `data/rest_token.txt`. While a token is required, every route except `GET`/`HEAD` on `/` and `/health` answers 401 without it, and `/docs`, `/redoc` and `/openapi.json` are turned off. `GET /` reports `"auth": "bearer"` or `"none"`. `RLM_API_TOKEN` is removed from the process environment after it has been read.
- `rootmind api` options `--port 0` (a free port, announced on stdout as `ROOTMIND_LISTENING {"port":N}`), `--exit-with-parent` and `--import-config FILE`.
- `rootmind mcp` and `rootmind mcp-http` subcommands, so the packaged executable also runs both MCP servers.
- MCP over HTTP re-reads the folder list when `config.yaml` changes. Its tools follow an added or removed folder without a restart (the in-process indexer of `--index` still needs one).
- `GET /connectors` lists folders that cannot be used with `"available": false` and the reason. When the background service may not read a new folder, `POST /config` refuses it with 400; a folder that is already configured stays and is listed as unavailable.
- Desktop app: Settings → Advanced for a backend at another address (with an optional token); hints in an empty chat; a second launch focuses the running window.
- Release workflow: a version tag builds the backend and the installer, tests both, and publishes a GitHub Release with the installer, the backend zip, `SHA256SUMS.txt` and build provenance attestations. winget manifests are prepared in `packaging/winget/`, not yet submitted.
- This changelog.

### Changed

- **`rootmind api` now uses `api.host` and `api.port` from `config.yaml`.** The bind address is taken from `--host`/`--port`, then `RLM_HOST`/`RLM_PORT`, then `config.yaml`, then `127.0.0.1:8000`. Before, the command-line defaults always won, so a non-default `api` section was ignored. If your `config.yaml` sets `api.host` or `api.port`, `rootmind api` now binds there. `RLM_HOST` and `RLM_PORT` now apply to `rootmind api` as well.
- `MCP_API_TOKEN` is removed from the process environment after it has been read.

### Fixed

- Settings, Folders and Status kept showing the previous backend's data after the app switched to another backend, and saving the settings could send that data to the new backend.

### Security

- The built-in backend and the background service require a bearer token on the REST API. The service confines its reads to the installing account's unelevated rights and keeps its program in a folder that only administrators can change. [SECURITY.md](SECURITY.md) describes the model.

## [1.1.0] - 2026-10-11

First public version of the source code. No release binaries were published.
