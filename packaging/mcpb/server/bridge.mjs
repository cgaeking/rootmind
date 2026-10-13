// RootMind for Claude Desktop: a stdio-to-HTTP bridge to the RootMind service.
//
// Claude Desktop starts this file with its built-in Node.js and talks MCP over
// stdin/stdout (newline-delimited JSON-RPC). The bridge forwards every message
// to the MCP endpoint of the installed RootMind service (POST
// http://127.0.0.1:<port>/mcp with its bearer token) and writes the JSON-RPC
// answers back to stdout.
//
// The port and the token file come from
//   "%ProgramFiles%\RootMind\backend\rootmind-backend.exe" service status --json
// which every account may run. The token is read from the file the status
// names and is never logged. Logs go to stderr, which Claude Desktop writes to
// its MCP log.
//
// When RootMind cannot be reached, every request is answered with a JSON-RPC
// error whose message says what to do. The bridge looks the service up again
// for the next request, and once per request after a connection error, so it
// follows a service that was repaired or restarted on another port.
//
// Node.js standard library only: the bundle ships no node_modules.

import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import http from "node:http";
import path from "node:path";
import readline from "node:readline";

export const RELEASES_URL = "https://github.com/cgaeking/rootmind/releases/latest";
export const SETTINGS = "RootMind → Settings → RootMind service";
export const MCP_TOGGLE = "Serve MCP over HTTP";
export const ERROR_CODE = -32000;

const STATUS_TIMEOUT_MS = 30_000;
const REQUEST_TIMEOUT_MS = 300_000;
const CONNECTION_ERRORS = new Set([
  "ECONNREFUSED",
  "ECONNRESET",
  "ECONNABORTED",
  "EHOSTUNREACH",
  "ENETUNREACH",
  "EPIPE",
  "EADDRNOTAVAIL",
]);

/** The friendly failure messages, by reason (also sent as `error.data.reason`). */
export const MESSAGES = {
  not_installed: () =>
    `RootMind is not installed on this computer. Install it from ${RELEASES_URL}, then try again.`,
  too_old: () =>
    `The installed RootMind is too old for Claude Desktop. Install the latest version from ${RELEASES_URL}, then try again.`,
  status_failed: (detail) =>
    `RootMind could not report the state of its service (${detail}). Open ${SETTINGS}, or run the RootMind installer again from ${RELEASES_URL}; then try again.`,
  service_missing: () =>
    `The RootMind service is not set up. Open ${SETTINGS} and choose Repair, or run the RootMind installer again from ${RELEASES_URL}; then try again.`,
  mcp_off: () =>
    `MCP over HTTP is turned off. Turn on "${MCP_TOGGLE}" in ${SETTINGS}, then try again.`,
  mcp_not_ours: () =>
    `A Windows service named RootMind-MCP-HTTP exists that RootMind did not install. Remove it, run the RootMind installer again from ${RELEASES_URL}, then try again.`,
  mcp_stopped: () =>
    `The RootMind MCP service (RootMind-MCP-HTTP) is stopped. In ${SETTINGS}, turn "${MCP_TOGGLE}" off and on again, or restart Windows; then try again.`,
  token_unreadable: () =>
    "This Windows account cannot read RootMind's MCP token: the RootMind service was set up for another account. Use Claude Desktop from the account that installed RootMind, or uninstall RootMind and install it again from this account.",
  unreachable: (url, code) =>
    `RootMind's MCP service does not answer at ${url} (${code}). In ${SETTINGS}, check that the service runs and turn "${MCP_TOGGLE}" off and on again, or restart Windows; then try again.`,
  token_rejected: () =>
    `RootMind's MCP service did not accept its token. Run the RootMind installer again from ${RELEASES_URL} to repair the service, then try again.`,
  timeout: (seconds) =>
    `RootMind's MCP service did not answer within ${seconds} seconds. Try again with a narrower request.`,
  http_error: (status, detail) =>
    `RootMind's MCP service answered with HTTP ${status}${detail ? `: ${detail}` : ""}.`,
  no_answer: (status) => `RootMind's MCP service sent no answer to the request (HTTP ${status}).`,
};

export class BridgeError extends Error {
  constructor(reason, ...args) {
    super(MESSAGES[reason](...args));
    this.reason = reason;
  }
}

/** `%ProgramFiles%\RootMind\backend\rootmind-backend.exe`, or null without %ProgramFiles%. */
export function defaultBackendExe(env = process.env) {
  // ProgramW6432 names the 64-bit Program Files folder even in a 32-bit process.
  const base = env.ProgramW6432 || env.ProgramFiles;
  return base ? path.win32.join(base, "RootMind", "backend", "rootmind-backend.exe") : null;
}

export function defaultStatusCommand(env = process.env) {
  const file = defaultBackendExe(env);
  return file ? { file, args: ["service", "status", "--json"] } : null;
}

function firstLine(text, max = 200) {
  const line = String(text ?? "").trim().split(/\r?\n/)[0] ?? "";
  return line.length > max ? `${line.slice(0, max)}…` : line;
}

/** Run `service status --json` and parse its output. Throws BridgeError. */
export function runStatus(command, timeoutMs = STATUS_TIMEOUT_MS) {
  if (!command) return Promise.reject(new BridgeError("not_installed"));
  return new Promise((resolve, reject) => {
    execFile(
      command.file,
      command.args,
      { timeout: timeoutMs, windowsHide: true, maxBuffer: 1024 * 1024, encoding: "utf8" },
      (error, stdout, stderr) => {
        if (error) {
          if (error.code === "ENOENT") return reject(new BridgeError("not_installed"));
          if (error.killed) return reject(new BridgeError("status_failed", "no answer in time"));
          const detail =
            typeof error.code === "number"
              ? `exit code ${error.code}${stderr.trim() ? `: ${firstLine(stderr)}` : ""}`
              : error.code || error.message;
          return reject(new BridgeError("status_failed", detail));
        }
        try {
          resolve(JSON.parse(stdout));
        } catch {
          reject(new BridgeError("status_failed", "unexpected output"));
        }
      },
    );
  });
}

/**
 * The MCP port and token file from the status JSON, or the BridgeError that
 * says why MCP over HTTP cannot be used.
 */
export function interpretStatus(status) {
  if (!status || typeof status !== "object") throw new BridgeError("status_failed", "unexpected output");
  const mcp = status.mcp_http;
  if (!mcp || typeof mcp !== "object") throw new BridgeError("too_old");
  if (!mcp.installed) throw new BridgeError(status.installed ? "mcp_off" : "service_missing");
  if (!Number.isInteger(mcp.port)) throw new BridgeError("mcp_not_ours");
  if (mcp.state !== "running" && mcp.state !== "start_pending") throw new BridgeError("mcp_stopped");
  if (!mcp.token_readable || typeof mcp.token_file !== "string") {
    throw new BridgeError("token_unreadable");
  }
  return { port: mcp.port, tokenFile: mcp.token_file, state: mcp.state };
}

export async function readToken(tokenFile) {
  let token;
  try {
    token = (await readFile(tokenFile, "utf8")).trim();
  } catch {
    throw new BridgeError("token_unreadable");
  }
  if (!token) throw new BridgeError("token_unreadable");
  return token;
}

/** POST one body to the endpoint; resolves with {status, contentType, headers, body}. */
function post({ host, port, token, body, headers, timeoutMs }) {
  return new Promise((resolve, reject) => {
    const request = http.request(
      {
        host,
        port,
        path: "/mcp",
        method: "POST",
        agent: false,
        headers: {
          "Content-Type": "application/json",
          Accept: "application/json, text/event-stream",
          "Content-Length": Buffer.byteLength(body),
          ...headers,
          Authorization: `Bearer ${token}`,
        },
      },
      (response) => {
        const chunks = [];
        response.on("data", (chunk) => chunks.push(chunk));
        response.on("error", reject);
        response.on("end", () =>
          resolve({
            status: response.statusCode,
            contentType: String(response.headers["content-type"] || ""),
            headers: response.headers,
            body: Buffer.concat(chunks).toString("utf8"),
          }),
        );
      },
    );
    request.setTimeout(timeoutMs, () => {
      const error = new Error("request timed out");
      error.code = "BRIDGE_TIMEOUT";
      request.destroy(error);
    });
    request.on("error", reject);
    request.end(body);
  });
}

/** JSON-RPC messages in an SSE body (`data:` lines of each event). */
function parseEventStream(text) {
  const messages = [];
  for (const event of text.split(/\r?\n\r?\n/)) {
    const data = event
      .split(/\r?\n/)
      .filter((line) => line.startsWith("data:"))
      .map((line) => line.slice(5).replace(/^ /, ""))
      .join("\n");
    if (!data.trim()) continue;
    try {
      messages.push(JSON.parse(data));
    } catch {
      // not JSON-RPC; ignore
    }
  }
  return messages;
}

function isJsonRpc(value) {
  if (Array.isArray(value)) return value.length > 0 && value.every(isJsonRpc);
  return !!value && typeof value === "object" && value.jsonrpc === "2.0";
}

/** The ids of the requests in a message (a batch may hold several). */
function requestIds(message) {
  const items = Array.isArray(message) ? message : [message];
  return items
    .filter((m) => m && typeof m === "object" && typeof m.method === "string" && "id" in m)
    .map((m) => m.id);
}

function errorResponse(id, error) {
  const reason = error instanceof BridgeError ? error.reason : "internal";
  return {
    jsonrpc: "2.0",
    id,
    error: { code: ERROR_CODE, message: error.message, data: { reason } },
  };
}

/**
 * Create a bridge between `input`/`output` (newline-delimited JSON-RPC) and
 * the RootMind service.
 *
 * Options (tests replace them): `statusCommand` ({file, args} or null),
 * `host`, `requestTimeoutMs`, `statusTimeoutMs`, `log` (a function taking a
 * line; default: stderr).
 */
export function createBridge({
  input = process.stdin,
  output = process.stdout,
  log = (line) => process.stderr.write(`${new Date().toISOString()} [rootmind] ${line}\n`),
  statusCommand = defaultStatusCommand(),
  host = "127.0.0.1",
  requestTimeoutMs = REQUEST_TIMEOUT_MS,
  statusTimeoutMs = STATUS_TIMEOUT_MS,
} = {}) {
  let target = null; // {port, token, url}
  let discovering = null;
  const sessionHeaders = {};

  async function discover() {
    const info = interpretStatus(await runStatus(statusCommand, statusTimeoutMs));
    const token = await readToken(info.tokenFile);
    const url = `http://${host}:${info.port}/mcp`;
    log(`RootMind MCP endpoint ${url} (service ${info.state})`);
    return { port: info.port, token, url };
  }

  /** The cached target, or a new lookup. `stale`: a target that just failed. */
  function getTarget(stale = null) {
    if (target && target !== stale) return Promise.resolve(target);
    if (!discovering) {
      target = null;
      discovering = discover()
        .then((found) => {
          target = found;
          return found;
        })
        .catch((error) => {
          log(`RootMind is not reachable: ${error.message}`);
          throw error;
        })
        .finally(() => {
          discovering = null;
        });
    }
    return discovering;
  }

  async function postTo(current, body) {
    try {
      return await post({
        host,
        port: current.port,
        token: current.token,
        body,
        headers: sessionHeaders,
        timeoutMs: requestTimeoutMs,
      });
    } catch (error) {
      return { error };
    }
  }

  /** Forward one message; resolves with the JSON-RPC messages to write. */
  async function forward(message, body) {
    let current = await getTarget();
    let result = await postTo(current, body);
    const retry =
      (result.error && CONNECTION_ERRORS.has(result.error.code)) || result.status === 401;
    if (retry) {
      log(
        result.error
          ? `No connection to ${current.url} (${result.error.code}); looking the service up again`
          : "The MCP service refused the token; looking the service up again",
      );
      current = await getTarget(current);
      result = await postTo(current, body);
    }
    if (result.error) {
      if (result.error.code === "BRIDGE_TIMEOUT") {
        throw new BridgeError("timeout", Math.round(requestTimeoutMs / 1000));
      }
      throw new BridgeError("unreachable", current.url, result.error.code || result.error.message);
    }
    if (result.status === 401) throw new BridgeError("token_rejected");

    const sessionId = result.headers["mcp-session-id"];
    if (typeof sessionId === "string" && sessionId) sessionHeaders["Mcp-Session-Id"] = sessionId;

    if (result.contentType.includes("text/event-stream")) return parseEventStream(result.body);
    if (result.body.trim()) {
      let parsed;
      try {
        parsed = JSON.parse(result.body);
      } catch {
        parsed = undefined;
      }
      if (isJsonRpc(parsed)) {
        const messages = Array.isArray(parsed) ? parsed : [parsed];
        rememberProtocolVersion(message, messages);
        return messages;
      }
      if (result.status >= 400) {
        const detail = parsed && typeof parsed.detail === "string" ? parsed.detail : firstLine(result.body);
        throw new BridgeError("http_error", result.status, detail);
      }
    }
    if (result.status >= 400) throw new BridgeError("http_error", result.status, "");
    if (requestIds(message).length) throw new BridgeError("no_answer", result.status);
    return [];
  }

  /** Send MCP-Protocol-Version on later requests, as streamable HTTP asks. */
  function rememberProtocolVersion(message, responses) {
    if (!message || message.method !== "initialize") return;
    const version = responses.find((r) => r && r.id === message.id)?.result?.protocolVersion;
    if (typeof version === "string") sessionHeaders["MCP-Protocol-Version"] = version;
  }

  function write(messages) {
    for (const m of messages) output.write(`${JSON.stringify(m)}\n`);
  }

  async function handleLine(line) {
    const text = line.trim();
    if (!text) return;
    let message;
    try {
      message = JSON.parse(text);
    } catch (error) {
      write([{ jsonrpc: "2.0", id: null, error: { code: -32700, message: `Parse error: ${error.message}` } }]);
      return;
    }
    try {
      write(await forward(message, text));
    } catch (error) {
      const ids = requestIds(message);
      if (!ids.length) {
        log(`Dropped a notification (${message?.method ?? "no method"}): ${error.message}`);
        return;
      }
      if (!(error instanceof BridgeError)) log(`Unexpected error: ${error.stack || error}`);
      write(ids.map((id) => errorResponse(id, error)));
    }
  }

  /** Read `input` until it ends; resolves when every request is answered. */
  function run() {
    log(`RootMind bridge for Claude Desktop, Node.js ${process.version}`);
    // Look the service up right away so the first request does not wait,
    // and the log shows the state at startup.
    getTarget().catch(() => {});
    const pending = new Set();
    const lines = readline.createInterface({ input, crlfDelay: Infinity });
    lines.on("line", (line) => {
      const task = handleLine(line).finally(() => pending.delete(task));
      pending.add(task);
    });
    return new Promise((resolve) => {
      lines.on("close", async () => {
        await Promise.allSettled([...pending]);
        if (discovering) await discovering.catch(() => {});
        resolve();
      });
    });
  }

  return { run, handleLine, getTarget };
}
