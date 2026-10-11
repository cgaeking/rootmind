# Windows deployment

Scripts to run the RootMind backend on Windows, either in a console window or as the
background service `RootMind-Backend` that starts on boot. The desktop app connects to it
on `http://127.0.0.1:8000`.

> **The service is experimental.** It runs as LocalSystem, and the REST API has no
> authentication, so any local process can make it read files with SYSTEM rights. Use it
> only on a single-user machine. Otherwise run the backend as your own user
> (`run-backend.bat`). See [Security](#security).

| File | Purpose |
|------|---------|
| `run-backend.bat` | Runs the REST API in a console window on `http://127.0.0.1:8000`. |
| `install-backend-service.bat` | One-click setup (asks for admin rights): fetches NSSM if needed, installs and starts the service. |
| `_service-backend.ps1` | Manages the service: `-Install`, `-Uninstall`, `-Start`, `-Stop`, `-Status`. |
| `fetch-nssm.ps1` | Downloads [NSSM](https://nssm.cc/) 2.24 into `tools\` and verifies its SHA-256. |

## Run modes

1. **Packaged**: the desktop installer ships `rootmind-backend.exe` (PyInstaller build, see
   `../../packaging/`), these scripts and a verified `tools\nssm.exe`, and installs the
   service during setup. No Python is needed.
2. **Source checkout** (development only): without `rootmind-backend.exe` (in `backend\`
   or `backend\dist\`), the service runs `python -m rootmind.main api` with the checkout's
   `.venv` or a Python installed for all users (`%ProgramFiles%\Python3xx`, `C:\Python3xx`;
   not the Microsoft Store build, not a per-user install). The checkout and that Python
   must be writable by administrators only, because the service runs their code as SYSTEM;
   `-Install` refuses otherwise. Then in `backend\`:

   ```bat
   uv sync
   ```

## Quickstart

From the `backend\` folder:

```bat
:: foreground, for a quick test
deploy\windows\run-backend.bat

:: permanent service: right-click, "Run as administrator"
deploy\windows\install-backend-service.bat

:: manage
powershell -File deploy\windows\_service-backend.ps1 -Status
powershell -File deploy\windows\_service-backend.ps1 -Stop
powershell -File deploy\windows\_service-backend.ps1 -Uninstall
```

## Security

- The service runs as **LocalSystem** and binds the REST API to `127.0.0.1:8000`; the
  API refuses connections from other machines.
- Its data lives in `%ProgramData%\RootMind` (`config.yaml` with API keys in plain text,
  `data\index.db` with the extracted text of every indexed file, `logs\`). `-Install`
  takes ownership of that folder, removes every other permission and grants access to
  SYSTEM and Administrators only. It refuses a folder that someone else created before the
  install, or one with junctions or symbolic links in it. RootMind never indexes this
  folder.
- `-Install` refuses to register the service when the executable, NSSM or (in source
  mode) the checkout or Python can be modified by non-administrators.
- The REST API has no authentication. Any process on the machine can call it, including
  `POST /config`, which decides which folders the service (with SYSTEM rights) indexes.
  This setup therefore assumes a **single-user machine**. Do not install the service on a
  machine shared with other local users. See [SECURITY.md](../../../SECURITY.md).

## NSSM provenance

- Source: `https://nssm.cc/release/nssm-2.24.zip`, file `win64/nssm.exe` (public domain).
- SHA-256: `f689ee9af94b00e9e3f0bb072b34caaf207f32dcb4f5782fc9ca351df9a06c97`.
- `fetch-nssm.ps1` deletes the file and exits with an error when the hash does not match.
  Verify an existing copy without downloading: `fetch-nssm.ps1 -FromFile <path>\nssm.exe`.
- Some antivirus products flag NSSM as a generic "service installer" tool. The pinned hash
  identifies the official 2.24 release.
- The binary is never committed; `tools/` is gitignored.
