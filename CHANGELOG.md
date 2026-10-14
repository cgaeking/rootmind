# Changelog

All notable changes to RootMind are documented in this file. The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and the project uses [Semantic Versioning](https://semver.org/spec/v2.0.0.html). The backend and the desktop app share one version number.

## [Unreleased]

## [2.3.0] - 2026-10-14

### Added

- **RootMind for Linux** (#21). Each release ships `RootMind-amd64.deb` (Ubuntu, Debian), `RootMind-x86_64.rpm` (Fedora, openSUSE Tumbleweed) and `RootMind-x86_64.AppImage` (other distributions), built with Tauri on Ubuntu 22.04 (glibc 2.35 or later, x86-64). The app, its window, chat, search, folders, settings, remote access and MCP over HTTP work as on Windows; the texts name systemd user services where Windows names a Windows service.
- **The background service on Linux runs as two systemd user services of your account**, `rootmind-backend.service` (REST API and indexing) and `rootmind-mcp-http.service` (MCP over HTTP, on by default), in `~/.config/systemd/user/`. No root, no system-wide daemon and no administrator prompt: the app sets them up on its first start and updates them when the app is newer than the service (after a package update), with **Set up** and **Repair** in Settings › RootMind service as the fallback and the error shown when that fails. The services start when you log in; `loginctl enable-linger` keeps them running without a login (for example for n8n).
- `rootmind-backend service install | uninstall | status [--json]` on Linux, with the same options (except `--user-sid`), the same `status --json` shape (plus `unit_dir` and `data_dir`) and the same exit codes where they apply; 11 means that the account's systemd user manager cannot be reached (for example under `su` or `sudo`), and 8 that it runs as root. The data folder is `$XDG_DATA_HOME/rootmind` (default `~/.local/share/rootmind`) with the same layout as on Windows: folders 0700 and token, authtoken and config files 0600, created with those modes; an existing folder is used only when it belongs to the account and nobody else can change it (else exit 6), and the service checks the folder and its secret files again when it starts. `service install` copies the program into `runtime/<version>-<id>/` of the data folder (an AppImage runs from a temporary mount), points the units there and deletes older copies once the new one runs; it refuses to copy from a folder another account can change (exit 10). It writes `service.json` (where the copy is; no secret) for the Claude Desktop bundle. `uninstall` removes the units and the program copies and keeps the data unless `--purge`.
- The service reports ready to systemd once it listens (`Type=notify`). A config, token or folder error ends it with exit code 2, which systemd does not restart; any other failure is restarted after 5 seconds. Like on Windows, `service install` and the app send a token only after checking that the unit's own process listens on the port (Linux: `/proc/net/tcp` and the process's sockets).
- `rootmind-backend-linux-x64.tar.gz`: the Linux backend on its own, for the command line and servers.
- The Claude Desktop bundle (`rootmind.mcpb`) also runs on Linux: it asks the program copy named in `~/.local/share/rootmind/service.json` (else `/usr/lib/RootMind/backend/rootmind-backend`) for `service status --json`, looks it up again for every connection, and words its hints for Linux.
- CI: the Linux backend build with its smoke test (PowerShell 7) and an end-to-end test that installs, checks, updates and removes the systemd user units on GitHub's Ubuntu runner (`backend-exe.yml`); a Linux build and test of the desktop app's Rust shell (`desktop.yml`); the Linux packages in the release, checked by installing the .deb and running both packaged backends, with checksums and attestations (`release.yml`).

### Changed

- The `service` commands say "runs on Windows and Linux only" elsewhere (exit code 2). The Windows service, its installer and their behavior are unchanged.
- Release notes list the Linux downloads.

## [2.2.0] - 2026-10-14

### Added

- **Remote access (ngrok)** for cloud assistants (Grok Bot, a cloud-hosted n8n, other web agents), off by default (#27). Settings → RootMind service → **Remote access (ngrok)** takes an ngrok authtoken (write-only: once saved only "Saved" is shown) and an optional reserved domain; one checkbox, after a confirmation that anyone with the MCP token can read every indexed document and that what a cloud assistant reads goes to its provider, gives MCP over HTTP a public HTTPS address. The status (off, connecting, connected with the public MCP endpoint and **Copy**, or ngrok's error) is shown in the same place. The tunnel runs inside the `RootMind-MCP-HTTP` service with ngrok's open-source Python SDK (no ngrok program is installed), forwards only to the MCP server, starts with the service, survives updates, reconnects with growing pauses, and closes within seconds when turned off. Requests through it still need the MCP token and pass the `Origin` check. Disabled while MCP over HTTP is off; turning MCP over HTTP off also turns remote access off. No administrator approval is needed.
- REST API: `GET /remote-access` and `POST /remote-access` (bearer token). The authtoken is stored in `%ProgramData%\RootMind\data\ngrok_authtoken.txt`, readable only by SYSTEM, Administrators and the MCP service account, never in `config.yaml`, never returned and never logged; `enabled` and `domain` go to `config.yaml` (`remote_access`). The MCP service reports the tunnel's state in `data\remote_access.json`, the one file in the data folder it may write.
- The smoke test checks that the packaged backend imports the ngrok SDK and that remote access is off by default; the service end-to-end test checks the new files' permissions.

### Changed

- Docs: "Reach RootMind from cloud assistants" now starts with the built-in remote access; your own tunnel (Tailscale Funnel, the ngrok agent) remains the alternative. SECURITY.md describes the threat model of the public address and how the authtoken is stored.

## [2.1.1] - 2026-10-14

### Added

- Docs: "Reach RootMind from cloud assistants" (Grok Bot, cloud-hosted n8n and similar) with tunnel steps for ngrok and Tailscale Funnel; the app's n8n hint mentions using the tunnel address when n8n runs elsewhere.

### Changed

- Docs: releases are described as not code-signed; the code signing policy no longer names a signing service, and winget is being submitted with unsigned binaries.
- The installer exits with 0 whenever it installed the app, also when `service install` failed (it used to exit with that command's code). RootMind shows the problem and **Repair**, and the installer's uninstall entry records the outcome: `RootMindServiceSetup` (`installed`, `skipped` or `failed`), `RootMindServiceSetupReason` and `RootMindServiceSetupCode`. Only another install folder still stops it (exit code 10).
- `service install` run as LocalSystem only updates an existing RootMind service, for the account that service belongs to; it never sets one up (exit code 8).

### Fixed

- **Silent and managed installs no longer fail** (winget's validation, Intune, Configuration Manager). An installer that runs as SYSTEM or another service account, or as another account than the one signed in to Windows, no longer stops with exit code 8: it installs the app, leaves the background service to the app and exits with 0. RootMind then says "The RootMind service is not set up yet" and offers **Set up**, which sets the service up for the signed-in account with one administrator approval. An update run as SYSTEM still updates an existing service. The removal of a RootMind 1.x installation runs only when the signed-in account runs the installer, never as SYSTEM. The Claude Desktop bundle points to **Set up** as well.

## [2.1.0] - 2026-10-14

### Added

- **RootMind for Claude Desktop.** Each release ships `rootmind.mcpb`, an MCP Bundle: double-click it and Claude Desktop installs RootMind's tools, with nothing to configure. A small bridge (Node.js standard library only, `packaging/mcpb`) asks `rootmind-backend.exe service status --json` for the MCP port and token file and forwards Claude Desktop's stdio messages to the service's MCP over HTTP on 127.0.0.1. When RootMind is not installed, MCP over HTTP is off, the service is stopped or belongs to another Windows account, its error says what to do; it follows the service to a new port. Needs RootMind with **Serve MCP over HTTP** on (the default). The bundle is in `SHA256SUMS.txt` and has an attestation like the other release files; `scripts/check_versions.py` also checks its manifest's version, and the workflow `mcpb.yml` tests, validates and packs it.

### Changed

- MCP: the server now tells clients that RootMind holds the user's own files on this computer and names the indexed folders (folder names only), so assistants such as Claude Desktop pick RootMind for questions about local documents instead of a cloud drive connector. The `search_documents` description and the bundle's manifest say the same.

## [2.0.0] - 2026-10-13

### Breaking

- **RootMind now always runs as a Windows service, and installing needs an administrator.** The installer is per machine and asks for administrator approval once; the account signed in to Windows must approve it itself. The desktop app no longer has a built-in backend of its own.
- **The 1.x built-in backend's data is not migrated.** Upgrading removes a 1.x installation of your account, but its folders, settings, API keys and index are not carried over: add your folders and API key again. A 1.x background service, if it was on, is updated and keeps its data. Chats stay.

### Added

- The installer sets up the services `RootMind-Backend` and `RootMind-MCP-HTTP` for the signed-in account (`service install --port auto --mcp-port auto`): REST on port 8000 or one from 18300–18399, MCP over HTTP on port 3000 or one from 18400–18499. If that step fails, the app stays installed and the installer names the cause and the log `%ProgramData%\RootMind\logs\install.log`.
- Settings → **RootMind service**: the state, address and version of the service, **Repair** when it is stopped or does not answer, **Serve MCP over HTTP**, and the MCP endpoint, the token and the values for n8n to copy. Repair and **Update service** run `service install --port auto --mcp-port auto` elevated with the backend installed with the app (`%ProgramFiles%\RootMind\backend`), so an older service is brought to the app's version; only when that backend is missing do they use the service's own copy under `%ProgramFiles%\RootMind Service\`. Switching MCP over HTTP runs that copy.
- `service install` exit code 10: it refuses to copy the program from a folder that accounts other than administrators can change, such as a download folder. Unzip the release build into a folder under `%ProgramFiles%` before running it on a server.
- Code signing policy ([CODE_SIGNING.md](CODE_SIGNING.md)) with team roles and a privacy policy, linked from the READMEs and the release notes, in preparation for signed releases.

### Changed

- The installer always installs to `%ProgramFiles%\RootMind`, with an uninstall entry for the whole computer (winget scope `machine`); another folder (`/D=`) is refused with exit code 10. It stops without changes when an administrator approves it for another signed-in account (exit code 8). A service set up for another account is not taken over: uninstall RootMind, then install it from the new account.
- MCP over HTTP is on by default: `service install` turns it on for a new install unless `--no-mcp-http` is given; an update keeps the current setting.
- Updates keep the service's ports, tokens, settings and data; the installer updates the service in place. Uninstalling removes the services and keeps `%ProgramData%\RootMind` unless **Delete the application data** is ticked; a silent uninstall never deletes data.
- The desktop app is a client of the RootMind service. When the service is missing, stopped or set up for another account, the app explains what to do; Settings → Advanced still connects to a backend at another address. Development builds ask for a backend address.
- For MCP clients, use MCP over HTTP of the service. `rootmind-backend.exe mcp` from the app's folder does not serve the service's index.

### Removed

- The built-in backend (a child process of the app) and the Settings switch between it and the background service.
- `rootmind api --exit-with-parent`, `rootmind api --import-config`, the startup line `ROOTMIND_LISTENING {"port":N}` and `service install --import-config`.

## [1.3.0] - 2026-10-13

### Added

- German README (`README.de.md`) with a language switch at the top of both READMEs, and a row of status badges: latest release, backend and desktop CI, license, platform and downloads ([#18](https://github.com/cgaeking/rootmind/issues/18)).
- Quickstart in English and German (`docs/QUICKSTART.md`, `docs/QUICKSTART.de.md`): from the installer to a first search, an API key for questions, and MCP for Claude Desktop, Claude Code and n8n. Both READMEs link to it at the top ([#19](https://github.com/cgaeking/rootmind/issues/19)).
- Desktop app: Settings → Appearance → **Language** with *System*, *English* and *Deutsch*. *System* follows the Windows display language (German for a German Windows, English otherwise). The choice is stored per user and switches every screen at once, without a restart ([#13](https://github.com/cgaeking/rootmind/issues/13)).
- Desktop app: Settings → **About** shows the app version and the version of the backend in use (they differ when the background service runs an older build), author, license and links to the repository, the releases and the issues, which open in the default browser ([#17](https://github.com/cgaeking/rootmind/issues/17)).

### Changed

- Desktop app: the UI language is no longer chosen at build time; `VITE_APP_LANG` has no effect any more ([#13](https://github.com/cgaeking/rootmind/issues/13)).
- Desktop app: the Folders screen has no **Save** button. A folder picked in the dialog, a removed folder and an edited path are saved right away. A folder the backend refuses (for example one the background service may not read) is taken back out, and the reason is shown ([#14](https://github.com/cgaeking/rootmind/issues/14)).
- Desktop app: paths are shown with backslashes on Windows (folder list, search results, chat sources, Status). Typed paths may use either separator; stored paths are unchanged ([#15](https://github.com/cgaeking/rootmind/issues/15)).
- Desktop app: Settings → LLM has **Save** next to the API key instead of **Load models**. The key is checked with the provider first: a valid key is saved together with the selected provider and a model (the selected one if the provider lists it, otherwise the first listed), and fills the model list, so no further Save is needed; an invalid one shows the provider's error and changes nothing. The model field stays disabled while the selected provider has no saved key. A pasted key preselects its provider by format (`sk-ant-` Anthropic, `xai-` xAI, other `sk-` OpenAI) with a hint, and another provider can still be chosen. Saved keys stay masked ([#16](https://github.com/cgaeking/rootmind/issues/16)).

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
