# Windows deployment

| File | Purpose |
|------|---------|
| `run-backend.bat` | Runs the REST API from a source checkout in a console window on `http://127.0.0.1:8000`. |

To run RootMind in the background on Windows, use the background service of the release
build: Settings → Background service in the desktop app, or
`rootmind-backend.exe service install` from an elevated prompt (see the
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

## Migrating from a manual NSSM setup

Earlier versions installed the backend as a Windows service with NSSM
(`install-backend-service.bat`, `_service-backend.ps1` and `fetch-nssm.ps1`, now removed), and
some setups registered NSSM services by hand, for example a REST API service and an MCP over
HTTP service. To move such a setup to the built-in service host, from an elevated prompt:

1. Stop and remove the old services, for each service name:

   ```bat
   nssm remove <name> confirm
   ```

   or, without NSSM at hand:

   ```bat
   sc.exe stop <name>
   sc.exe delete <name>
   ```

2. Install the new services with the old ports, folders and MCP token, from the unzipped
   release build (`rootmind-backend-windows-x64.zip`):

   ```bat
   "<unzipped>\rootmind-backend\rootmind-backend.exe" service install --port <old API port> ^
       --mcp-http --mcp-port <old MCP port> ^
       --import-config "<old RLM_HOME>\config.yaml" ^
       --mcp-token-from "<old RLM_HOME>\data\api_token.txt"
   ```

   If the old data folder is `%ProgramData%\RootMind` itself, copy its `config.yaml` and
   `data\api_token.txt` to another folder first and pass the copies; if `service install`
   then refuses that folder (exit code 6), rename it and run the command again.

   With the same ports and token, tunnels and MCP clients need no change. The index is
   rebuilt in the new data folder. The REST API now requires a token; the desktop app reads
   it, other REST clients need `Authorization: Bearer <token>` with the token from
   `%ProgramData%\RootMind\data\rest_token.txt`.
3. In the desktop app, open Settings → Advanced and choose **Use default backend**, so the
   app uses the new service instead of a fixed address.
4. Once the new service has indexed your folders, delete the old program folder and the
   NSSM binary. Delete the old data folder too, unless it is `%ProgramData%\RootMind`: the
   new service now uses that folder (if you renamed it in step 2, delete the renamed copy).

The service reads only folders that the installing account can open without administrator
rights, and no network drives or shares. Folders it cannot read are marked in the app and
listed by `GET /connectors` with `"available": false` and the reason.

## If you installed the former per-machine app

Earlier source versions could build a per-machine "full installer" that put the app in
`%ProgramFiles%\RootMind` and set up the NSSM service. Its uninstaller stops and deletes
any service named `RootMind-Backend`, which is also the new service's name. Uninstall that
app from Settings → Apps **first**, then install the current app (it installs for your
account only) and turn the background service on. The old app cannot talk to the new,
token-protected service anyway.

## Removing a service created by the old scripts

The old scripts registered a service named `RootMind-Backend` that runs NSSM. While it
exists, `service install` stops with exit code 3, leaves it untouched and prints the commands
to remove it. From an elevated prompt:

```bat
sc.exe stop RootMind-Backend
sc.exe delete RootMind-Backend
```

Then delete the old program folder with `nssm.exe`. Copy `%ProgramData%\RootMind\config.yaml`
to another folder and pass the copy to `service install --import-config`, so the settings
move to the new layout (the index then lives in `data\db\` and is rebuilt).

The old scripts also created `%ProgramData%\RootMind`, owned by Administrators and
accessible to SYSTEM and Administrators only. `service install` reuses an existing folder
only when it is still protected: every item owned by SYSTEM or Administrators, no write
access for other accounts, no junctions, symbolic links or hard-linked files. A folder that
fails the check, for example one created by hand (which inherits "Users: create" from
ProgramData), is refused with exit code 6. Remove or rename it from an elevated prompt and
run `service install` again; the new folder starts with a fresh index.
