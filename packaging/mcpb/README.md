# RootMind for Claude Desktop (MCP Bundle)

This folder is the source of `rootmind.mcpb`, an [MCP Bundle](https://github.com/modelcontextprotocol/mcpb)
that installs RootMind in Claude Desktop with a double click and no configuration. Each release
ships it next to the installer. It needs RootMind 2.0 or later, installed with the installer, and
**Serve MCP over HTTP** turned on (Settings → RootMind service; on by default).

## Files

| File | Content |
| --- | --- |
| `manifest.json` | Bundle manifest (`manifest_version` 0.3), server type `node`, Windows only, no `user_config` |
| `server/index.mjs` | Entry point that Claude Desktop starts with its built-in Node.js |
| `server/bridge.mjs` | The stdio-to-HTTP bridge |
| `icon.png` | A copy of `desktop/src-tauri/icons/icon.png` (512×512) |
| `test/` | `node:test` tests with a fake MCP server and a fake status command; not packed |
| `.mcpbignore` | Keeps the tests and this README out of the bundle |

The bundle uses Node's standard library only, so it has no `package.json` and no `node_modules`.

## How the bridge works

Claude Desktop starts only local (stdio) MCP servers, and RootMind's MCP server for the installed
app is the service `RootMind-MCP-HTTP`, which speaks MCP over HTTP on 127.0.0.1 with a bearer
token. The bridge connects the two:

1. At start it runs `"%ProgramFiles%\RootMind\backend\rootmind-backend.exe" service status --json`
   (`ProgramW6432` first, so a 32-bit Node finds the 64-bit folder too). From `mcp_http` it takes
   the port and the token file (`%ProgramData%\RootMind\data\api_token.txt`) and reads the token.
2. Every newline-delimited JSON-RPC message from stdin goes to `POST http://127.0.0.1:<port>/mcp`
   with `Authorization: Bearer <token>`. JSON-RPC answers (JSON, or an event stream) are written to
   stdout, one per line. A notification gets HTTP 202 and no output. After `initialize`, requests
   carry `MCP-Protocol-Version`. Requests run concurrently; the answers carry their ids.
3. On a refused or reset connection, or HTTP 401, it runs the status command and reads the token
   again, once per request, so it follows a service that was restarted on another port.
4. When RootMind cannot be used, every request (starting with `initialize`) is answered with a
   JSON-RPC error (code -32000, `error.data.reason`) whose message says what to do; notifications
   are dropped with a log line. The next request looks again, so fixing RootMind needs no restart
   of Claude Desktop.

| `reason` | When | The message tells the user to |
| --- | --- | --- |
| `not_installed` | No `rootmind-backend.exe` under `%ProgramFiles%\RootMind\backend` | install RootMind from the latest release |
| `too_old` | The status has no `mcp_http` | install the latest release |
| `status_failed` | The status command fails or prints no JSON | open Settings → RootMind service or reinstall |
| `service_missing` | No RootMind service | choose Repair or reinstall |
| `mcp_off` | MCP over HTTP is off | turn on **Serve MCP over HTTP** |
| `mcp_not_ours` | A `RootMind-MCP-HTTP` service RootMind did not install | remove it and reinstall |
| `mcp_stopped` | The MCP service is stopped | switch **Serve MCP over HTTP** off and on, or restart Windows |
| `token_unreadable` | The token file is not readable: the service belongs to another Windows account | use the account that installed RootMind, or reinstall from this one |
| `unreachable` | Still no connection after the second lookup | check the service, switch MCP off and on, or restart Windows |
| `token_rejected` | Still HTTP 401 after reading the token again | reinstall to repair the service |
| `timeout`, `http_error`, `no_answer` | No answer in 300 s, an HTTP error without a JSON-RPC body, an empty answer to a request | (states the problem) |

The token is sent only in the `Authorization` header to 127.0.0.1. It is never written to stdout
or to the log. Logs go to stderr (Claude Desktop's MCP log for the extension) with the prefix
`[rootmind]`. Unlike the desktop app, the bridge does not check that the service's own process
owns the port (Node.js has no API for that); it sends the token only to the port that
`service status` reports for the running (or starting) MCP service.

## Test, validate and pack

```bash
node --test "packaging/mcpb/test/*.test.mjs"            # Node 24
npx --yes @anthropic-ai/mcpb@2.1.2 validate packaging/mcpb/manifest.json
npx --yes @anthropic-ai/mcpb@2.1.2 pack packaging/mcpb rootmind.mcpb
```

The tests also check that the manifest lists exactly the tools of `backend/src/rootmind/tools.py`,
carries the version of `desktop/package.json` and uses the app icon. `scripts/check_versions.py`
compares the manifest's version with the other version files, so set it together with them for a
release.

CI: `.github/workflows/mcpb.yml` runs the tests on Linux and Windows and validates and packs the
bundle for pull requests that touch this folder. For a version tag, `release.yml` builds
`rootmind.mcpb` in the job `mcpb` and publishes it with the release, in `SHA256SUMS.txt` and with an
attestation. The bundle is not signed (`mcpb sign`); Claude Desktop shows it as unsigned.

To try a build by hand, open the packed `rootmind.mcpb` in Claude Desktop (double-click it, or drag
it onto Settings → Extensions), or run the bridge on its own and type JSON-RPC lines:

```bash
node packaging/mcpb/server/index.mjs
{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-06-18","capabilities":{},"clientInfo":{"name":"test","version":"0"}}}
{"jsonrpc":"2.0","id":2,"method":"tools/list"}
```

`manifest_version` 0.3 is the current version of the [manifest spec](https://github.com/modelcontextprotocol/mcpb/blob/main/MANIFEST.md);
0.4 only adds the `uv` server type and is marked experimental.
