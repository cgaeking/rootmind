# Windows deployment

| File | Purpose |
|------|---------|
| `run-backend.bat` | Runs the REST API from a source checkout in a console window on `http://127.0.0.1:8000`. |

To run RootMind in the background on Windows, use the Windows service of the release build.
The RootMind installer (`RootMind-Setup-x64.exe`) sets it up for the account signed in to
Windows; Settings → RootMind service in the desktop app shows its state and offers
**Repair** (administrator approval). On a server without the app, run
`rootmind-backend.exe service install` from `rootmind-backend-windows-x64.zip`, unzipped into a
folder that only administrators can change (for example under `%ProgramFiles%`; other folders
are refused with exit code 10), in an elevated prompt (see the
[README](../../../README.md#install-for-developers-and-servers)). The service runs from
`%ProgramFiles%\RootMind Service\`, keeps its data in `%ProgramData%\RootMind`, requires a
token on its REST API and reads only what the installing account can read. See
[SECURITY.md](../../../SECURITY.md#background-service).

## run-backend.bat

From the `backend\` folder, after `uv sync`:

```bat
deploy\windows\run-backend.bat
```

It runs `python -m rootmind.main api --host 127.0.0.1 --port 8000` with the checkout's
`.venv` (or `python` from `PATH`), as your own user and without a token. Extra arguments are
passed on, for example `--require-token`. Close the window to stop it.
