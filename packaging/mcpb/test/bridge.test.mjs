// Tests of the RootMind MCP Bundle's stdio-to-HTTP bridge:
//   node --test packaging/mcpb/test/
// A fake MCP server stands in for the RootMind service and a small Node
// script (fixtures/fake-status.mjs) for `rootmind-backend.exe service status --json`.

import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { mkdirSync, mkdtempSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import http from "node:http";
import { tmpdir } from "node:os";
import path from "node:path";
import readline from "node:readline";
import { PassThrough } from "node:stream";
import { after, before, describe, test } from "node:test";
import { fileURLToPath } from "node:url";

import {
  ERROR_CODE,
  RELEASES_URL,
  createBridge,
  defaultBackendExe,
  defaultStatusCommand,
  interpretStatus,
} from "../server/bridge.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
const BUNDLE = path.resolve(HERE, "..");
const ROOT = path.resolve(BUNDLE, "..", "..");
const FAKE_STATUS = path.join(HERE, "fixtures", "fake-status.mjs");
const RUN_BRIDGE = path.join(HERE, "fixtures", "run-bridge.mjs");
const TOKEN = "test-token-7f3c9a1e5b2d4c6f8a0b"; // pragma: allowlist secret (test value)
const TOOL_NAMES = ["search_documents", "search_fuzzy", "list_documents", "read_document", "get_statistics"];

// --- fixtures ------------------------------------------------------------------

/** A fake RootMind MCP endpoint. `respond(message, req, res)` may take over a request. */
async function startServer({ token = TOKEN, name = "fake", respond } = {}) {
  const requests = [];
  const server = http.createServer((req, res) => {
    const chunks = [];
    req.on("data", (c) => chunks.push(c));
    req.on("end", () => {
      const body = Buffer.concat(chunks).toString("utf8");
      const message = JSON.parse(body);
      requests.push({ method: req.method, url: req.url, headers: req.headers, message });
      if (req.headers.authorization !== `Bearer ${token}`) {
        res.writeHead(401, { "Content-Type": "application/json", "WWW-Authenticate": "Bearer" });
        return res.end(JSON.stringify({ detail: "Invalid token" }));
      }
      if (respond && respond(message, req, res)) return;
      if (!("id" in message)) {
        res.writeHead(202);
        return res.end();
      }
      const json = (payload) => {
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ jsonrpc: "2.0", id: message.id, ...payload }));
      };
      switch (message.method) {
        case "initialize":
          return json({
            result: {
              protocolVersion: message.params?.protocolVersion ?? "2025-06-18",
              capabilities: { tools: { listChanged: false } },
              serverInfo: { name: "rootmind", version: "2.0.0" },
            },
          });
        case "tools/list":
          return json({
            result: { tools: TOOL_NAMES.map((n) => ({ name: n, description: n, inputSchema: { type: "object" } })) },
          });
        case "tools/call":
          return json({
            result: {
              content: [{ type: "text", text: `${name}: ${message.params.name} ${JSON.stringify(message.params.arguments)}` }],
              isError: false,
            },
          });
        default:
          return json({ error: { code: -32601, message: `Method not found: ${message.method}` } });
      }
    });
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  return {
    port: server.address().port,
    requests,
    close: () =>
      new Promise((resolve) => {
        server.closeAllConnections?.();
        server.close(resolve);
      }),
  };
}

/** A port on which nothing listens. */
async function closedPort() {
  const server = http.createServer();
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  await new Promise((resolve) => server.close(resolve));
  return port;
}

let workDir;
let counter = 0;

/** A token file and a status control file for the fake status command. */
function makeMachine({ token = TOKEN } = {}) {
  const dir = path.join(workDir, `machine-${++counter}`);
  mkdirSync(dir, { recursive: true });
  const tokenFile = path.join(dir, "api_token.txt");
  writeFileSync(tokenFile, `${token}\n`, "utf8");
  const control = path.join(dir, "status.json");
  const machine = {
    tokenFile,
    control,
    statusCommand: { file: process.execPath, args: [FAKE_STATUS, control] },
    /** Make the next `service status --json` print this status. */
    setStatus(overrides = {}, mcpOverrides = {}) {
      machine.setControl({ stdout: statusJson(tokenFile, overrides, mcpOverrides) });
    },
    setControl(value) {
      writeFileSync(control, JSON.stringify(value), "utf8");
    },
  };
  return machine;
}

/** A machine whose status points at `server`. */
function machineWithServer(server, mcpOverrides = {}) {
  const machine = makeMachine();
  machine.setStatus({}, { port: server.port, ...mcpOverrides });
  return machine;
}

function statusJson(tokenFile, overrides, mcpOverrides) {
  return {
    installed: true,
    ours: true,
    state: "running",
    port: 8000,
    version: "2.0.0",
    binary: "C:\\Program Files\\RootMind Service\\2.0.0-0a1b2c3d\\rootmind-backend.exe",
    user_sid: "S-1-5-21-1-2-3-1001",
    token_file: "C:\\ProgramData\\RootMind\\data\\rest_token.txt",
    token_readable: true,
    ...overrides,
    mcp_http: {
      installed: true,
      state: "running",
      port: 3000,
      token_file: tokenFile,
      token_readable: true,
      ...mcpOverrides,
    },
    warnings: [],
  };
}

/** An in-process bridge on in-memory streams. */
function startSession(options) {
  const input = new PassThrough();
  const output = new PassThrough();
  const logs = [];
  const bridge = createBridge({ input, output, log: (line) => logs.push(line), ...options });
  const done = bridge.run();
  const messages = [];
  const waiters = [];
  readline.createInterface({ input: output }).on("line", (line) => {
    const message = JSON.parse(line);
    messages.push(message);
    for (const w of [...waiters]) {
      if (w.match(message)) {
        waiters.splice(waiters.indexOf(w), 1);
        w.resolve(message);
      }
    }
  });
  const session = {
    logs,
    messages,
    send(message) {
      input.write(`${typeof message === "string" ? message : JSON.stringify(message)}\n`);
    },
    waitFor(match) {
      const found = messages.find(match);
      if (found) return Promise.resolve(found);
      return new Promise((resolve) => waiters.push({ match, resolve }));
    },
    request(message) {
      session.send(message);
      return session.waitFor((m) => m.id === message.id);
    },
    async close() {
      input.end();
      await done;
      const text = logs.join("\n") + JSON.stringify(messages);
      assert.ok(!text.includes(TOKEN), "the token must never appear in the logs or the output");
      return { messages, logs };
    },
  };
  return session;
}

async function waitUntil(condition, timeoutMs = 10_000) {
  const end = Date.now() + timeoutMs;
  while (!condition()) {
    if (Date.now() > end) throw new Error("timed out waiting");
    await new Promise((resolve) => setTimeout(resolve, 10));
  }
}

let nextId = 1;
const req = (method, params = {}) => ({ jsonrpc: "2.0", id: nextId++, method, params });
const initialize = () =>
  req("initialize", {
    protocolVersion: "2025-06-18",
    capabilities: {},
    clientInfo: { name: "claude-ai", version: "0.1.0" },
  });

function assertFriendlyError(response, id, reason, ...phrases) {
  assert.equal(response.id, id);
  assert.equal(response.result, undefined);
  assert.equal(response.error.code, ERROR_CODE);
  assert.equal(response.error.data.reason, reason);
  for (const phrase of phrases) assert.ok(response.error.message.includes(phrase), `${response.error.message} lacks ${phrase}`);
}

before(() => {
  workDir = mkdtempSync(path.join(tmpdir(), "rootmind-mcpb-"));
});

after(() => {
  rmSync(workDir, { recursive: true, force: true });
});

// --- happy path ----------------------------------------------------------------

describe("forwarding", () => {
  test("initialize, tools/list and tools/call reach the service and come back", async () => {
    const server = await startServer();
    const machine = machineWithServer(server);
    const session = startSession({ statusCommand: machine.statusCommand });

    const init = initialize();
    const initResponse = await session.request(init);
    assert.equal(initResponse.result.serverInfo.name, "rootmind");
    assert.equal(initResponse.result.protocolVersion, "2025-06-18");

    session.send({ jsonrpc: "2.0", method: "notifications/initialized" });

    const list = req("tools/list");
    const listResponse = await session.request(list);
    assert.deepEqual(
      listResponse.result.tools.map((t) => t.name),
      TOOL_NAMES,
    );

    const call = req("tools/call", { name: "search_documents", arguments: { query: "invoice" } });
    const callResponse = await session.request(call);
    assert.equal(callResponse.result.content[0].text, 'fake: search_documents {"query":"invoice"}');

    const { messages, logs } = await session.close();
    await server.close();

    // One answer per request, none for the notification.
    assert.deepEqual(messages.map((m) => m.id), [init.id, list.id, call.id]);
    assert.deepEqual(
      server.requests.map((r) => r.message.method),
      ["initialize", "notifications/initialized", "tools/list", "tools/call"],
    );
    for (const r of server.requests) {
      assert.equal(r.method, "POST");
      assert.equal(r.url, "/mcp");
      assert.equal(r.headers.authorization, `Bearer ${TOKEN}`);
      assert.equal(r.headers["content-type"], "application/json");
      assert.match(r.headers.accept, /application\/json/);
      assert.equal(r.headers.origin, undefined);
    }
    // Later requests carry the negotiated protocol version.
    assert.equal(server.requests.at(-1).headers["mcp-protocol-version"], "2025-06-18");
    assert.ok(logs.some((l) => l.includes(`http://127.0.0.1:${server.port}/mcp`)));
  });

  test("the service is looked up once for many requests", async () => {
    const server = await startServer();
    const machine = machineWithServer(server);
    const session = startSession({ statusCommand: machine.statusCommand });
    const requests = [initialize(), req("tools/list"), req("tools/list"), req("ping")];
    const responses = await Promise.all(requests.map((r) => session.request(r)));
    assert.equal(responses.length, 4);
    const { logs } = await session.close();
    await server.close();
    assert.equal(logs.filter((l) => l.startsWith("RootMind MCP endpoint")).length, 1);
  });

  test("a JSON-RPC error of the service is passed through unchanged", async () => {
    const server = await startServer();
    const machine = machineWithServer(server);
    const session = startSession({ statusCommand: machine.statusCommand });
    const r = req("resources/list");
    const response = await session.request(r);
    assert.deepEqual(response.error, { code: -32601, message: "Method not found: resources/list" });
    await session.close();
    await server.close();
  });

  test("a line that is not JSON gets a parse error", async () => {
    const server = await startServer();
    const machine = machineWithServer(server);
    const session = startSession({ statusCommand: machine.statusCommand });
    session.send("{not json");
    const response = await session.waitFor((m) => m.id === null);
    assert.equal(response.error.code, -32700);
    await session.close();
    await server.close();
    assert.equal(server.requests.length, 0);
  });

  test("an event-stream answer is unpacked into its messages", async () => {
    const server = await startServer({
      respond(message, _req, res) {
        if (message.method !== "tools/list") return false;
        res.writeHead(200, { "Content-Type": "text/event-stream" });
        res.end(
          `event: message\ndata: ${JSON.stringify({ jsonrpc: "2.0", id: message.id, result: { tools: [] } })}\n\n`,
        );
        return true;
      },
    });
    const machine = machineWithServer(server);
    const session = startSession({ statusCommand: machine.statusCommand });
    const r = req("tools/list");
    assert.deepEqual((await session.request(r)).result, { tools: [] });
    await session.close();
    await server.close();
  });
});

// --- notifications -------------------------------------------------------------

describe("notifications", () => {
  test("are forwarded and answered with nothing", async () => {
    const server = await startServer();
    const machine = machineWithServer(server);
    const session = startSession({ statusCommand: machine.statusCommand });
    session.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    session.send({ jsonrpc: "2.0", method: "notifications/cancelled", params: { requestId: 1 } });
    const ping = req("ping");
    await session.request(ping);
    const { messages } = await session.close();
    await server.close();
    assert.deepEqual(messages.map((m) => m.id), [ping.id]);
    assert.deepEqual(
      server.requests.map((r) => r.message.method).sort(),
      ["notifications/cancelled", "notifications/initialized", "ping"],
    );
  });

  test("a JSON-RPC message the service sends back for a notification is written", async () => {
    const server = await startServer({
      respond(message, _req, res) {
        if (message.method !== "notifications/custom") return false;
        res.writeHead(200, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ jsonrpc: "2.0", method: "notifications/message", params: { level: "info" } }));
        return true;
      },
    });
    const machine = machineWithServer(server);
    const session = startSession({ statusCommand: machine.statusCommand });
    session.send({ jsonrpc: "2.0", method: "notifications/custom" });
    const message = await session.waitFor((m) => m.method === "notifications/message");
    assert.equal(message.params.level, "info");
    await session.close();
    await server.close();
  });

  test("while RootMind is unreachable they are dropped without output", async () => {
    const session = startSession({ statusCommand: null });
    session.send({ jsonrpc: "2.0", method: "notifications/initialized" });
    const { messages, logs } = await session.close();
    assert.deepEqual(messages, []);
    assert.ok(logs.some((l) => l.includes("Dropped a notification (notifications/initialized)")));
  });
});

// --- friendly failures ---------------------------------------------------------

describe("failures answer requests with what to do", () => {
  async function firstAnswer(options, request = initialize()) {
    const session = startSession(options);
    const response = await session.request(request);
    const { logs } = await session.close();
    return { response, request, logs };
  }

  test("RootMind not installed: no %ProgramFiles%", async () => {
    const { response, request } = await firstAnswer({ statusCommand: null });
    assertFriendlyError(response, request.id, "not_installed", "RootMind is not installed", RELEASES_URL);
  });

  test("RootMind not installed: no rootmind-backend.exe", async () => {
    const statusCommand = defaultStatusCommand({ ProgramFiles: path.join(workDir, "no-such-folder") });
    const { response, request } = await firstAnswer({ statusCommand });
    assertFriendlyError(response, request.id, "not_installed", RELEASES_URL);
  });

  test("the status command fails", async () => {
    const machine = makeMachine();
    machine.setControl({ stderr: "error: cannot read the state of the background service: Access denied\n", exit: 1 });
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(response, request.id, "status_failed", "exit code 1", "Access denied", RELEASES_URL);
  });

  test("the status command prints no JSON", async () => {
    const machine = makeMachine();
    machine.setControl({ stdout: "The RootMind background service is not installed." });
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(response, request.id, "status_failed", "unexpected output");
  });

  test("an old RootMind without mcp_http in its status", async () => {
    const machine = makeMachine();
    machine.setControl({ stdout: { installed: true, ours: true, state: "running", port: 8000 } });
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(response, request.id, "too_old", RELEASES_URL);
  });

  test("the RootMind service is not installed", async () => {
    const machine = makeMachine();
    machine.setStatus({ installed: false, ours: false, state: null }, { installed: false, state: null, port: null });
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(response, request.id, "service_missing", "not set up yet", "Set up", RELEASES_URL);
  });

  test("MCP over HTTP is turned off", async () => {
    const machine = makeMachine();
    machine.setStatus({}, { installed: false, state: null, port: null, token_file: null, token_readable: false });
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(
      response,
      request.id,
      "mcp_off",
      'Turn on "Serve MCP over HTTP" in RootMind → Settings → RootMind service',
    );
  });

  test("a RootMind-MCP-HTTP service RootMind did not install", async () => {
    const machine = makeMachine();
    machine.setStatus({}, { port: null, token_file: null, token_readable: false });
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(response, request.id, "mcp_not_ours", "did not install");
  });

  test("the MCP service is stopped", async () => {
    const machine = makeMachine();
    machine.setStatus({}, { state: "stopped" });
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(response, request.id, "mcp_stopped", "is stopped", "Serve MCP over HTTP");
  });

  test("the token is not readable: service set up for another account", async () => {
    const machine = makeMachine();
    machine.setStatus({ token_readable: false }, { token_readable: false });
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(response, request.id, "token_unreadable", "another account");
  });

  test("the token file cannot be read although the status says it can", async () => {
    const machine = makeMachine();
    rmSync(machine.tokenFile);
    machine.setStatus();
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(response, request.id, "token_unreadable");
  });

  test("connection refused", async () => {
    const machine = makeMachine();
    const port = await closedPort();
    machine.setStatus({}, { port });
    const { response, request, logs } = await firstAnswer({ statusCommand: machine.statusCommand });
    assertFriendlyError(response, request.id, "unreachable", `http://127.0.0.1:${port}/mcp`, "ECONNREFUSED");
    // Looked up again once, then gave up.
    assert.equal(logs.filter((l) => l.startsWith("RootMind MCP endpoint")).length, 2);
  });

  test("the service refuses the token", async () => {
    const server = await startServer({ token: "another-token" });
    const machine = machineWithServer(server);
    const { response, request, logs } = await firstAnswer({ statusCommand: machine.statusCommand });
    await server.close();
    assertFriendlyError(response, request.id, "token_rejected", RELEASES_URL);
    assert.equal(server.requests.length, 2, "one retry with a freshly read token");
    assert.ok(logs.some((l) => l.includes("refused the token")));
  });

  test("an HTTP error without a JSON-RPC body", async () => {
    const server = await startServer({
      respond(_message, _req, res) {
        res.writeHead(403, { "Content-Type": "application/json" });
        res.end(JSON.stringify({ detail: "Origin not allowed" }));
        return true;
      },
    });
    const machine = machineWithServer(server);
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    await server.close();
    assertFriendlyError(response, request.id, "http_error", "HTTP 403", "Origin not allowed");
  });

  test("no answer in time", async () => {
    const server = await startServer({ respond: () => true }); // never answers
    const machine = machineWithServer(server);
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand, requestTimeoutMs: 200 });
    await server.close();
    assertFriendlyError(response, request.id, "timeout", "did not answer");
  });

  test("an empty answer to a request", async () => {
    const server = await startServer({
      respond(_message, _req, res) {
        res.writeHead(202);
        res.end();
        return true;
      },
    });
    const machine = machineWithServer(server);
    const { response, request } = await firstAnswer({ statusCommand: machine.statusCommand });
    await server.close();
    assertFriendlyError(response, request.id, "no_answer", "HTTP 202");
  });

  test("later requests get the error too, and work once RootMind is fixed, without a restart", async () => {
    const server = await startServer();
    const machine = makeMachine();
    machine.setStatus({}, { installed: false, state: null, port: null });
    const session = startSession({ statusCommand: machine.statusCommand });

    const init = initialize();
    assertFriendlyError(await session.request(init), init.id, "mcp_off");
    const list = req("tools/list");
    assertFriendlyError(await session.request(list), list.id, "mcp_off");

    machine.setStatus({}, { port: server.port });
    const list2 = req("tools/list");
    assert.equal((await session.request(list2)).result.tools.length, TOOL_NAMES.length);
    await session.close();
    await server.close();
  });
});

// --- re-discovery --------------------------------------------------------------

describe("re-discovery", () => {
  test("follows the service to another port after a restart", async () => {
    const first = await startServer({ name: "first" });
    const machine = machineWithServer(first);
    const session = startSession({ statusCommand: machine.statusCommand });

    const call1 = req("tools/call", { name: "get_statistics", arguments: {} });
    assert.match((await session.request(call1)).result.content[0].text, /^first:/);

    // The service restarts on another port.
    await first.close();
    const second = await startServer({ name: "second" });
    machine.setStatus({}, { port: second.port });

    const call2 = req("tools/call", { name: "get_statistics", arguments: {} });
    assert.match((await session.request(call2)).result.content[0].text, /^second:/);
    const call3 = req("tools/call", { name: "get_statistics", arguments: {} });
    assert.match((await session.request(call3)).result.content[0].text, /^second:/);

    const { logs } = await session.close();
    await second.close();
    assert.ok(logs.some((l) => l.includes("ECONNREFUSED") && l.includes("looking the service up again")));
    assert.deepEqual(
      logs.filter((l) => l.startsWith("RootMind MCP endpoint")),
      [
        `RootMind MCP endpoint http://127.0.0.1:${first.port}/mcp (service running)`,
        `RootMind MCP endpoint http://127.0.0.1:${second.port}/mcp (service running)`,
      ],
    );
  });

  test("reads a changed token again", async () => {
    const server = await startServer({ token: "new-token-after-reinstall" });
    const machine = machineWithServer(server); // still holds TOKEN
    const session = startSession({ statusCommand: machine.statusCommand });
    // The lookup at startup reads TOKEN; then the service wants another one.
    await waitUntil(() => session.logs.some((l) => l.startsWith("RootMind MCP endpoint")));
    writeFileSync(machine.tokenFile, "new-token-after-reinstall", "utf8");
    const r = req("tools/list");
    assert.ok((await session.request(r)).result);
    await session.close();
    await server.close();
    assert.equal(server.requests.length, 2);
  });
});

// --- the real process and its stderr -------------------------------------------

describe("process", () => {
  function runProcess(args, env, lines) {
    return new Promise((resolve, reject) => {
      const child = spawn(process.execPath, args, { env, stdio: ["pipe", "pipe", "pipe"] });
      let stdout = "";
      let stderr = "";
      child.stdout.on("data", (d) => (stdout += d));
      child.stderr.on("data", (d) => (stderr += d));
      child.on("error", reject);
      child.on("close", (code) => resolve({ code, stdout, stderr }));
      for (const line of lines) child.stdin.write(`${JSON.stringify(line)}\n`);
      child.stdin.end();
    });
  }

  test("index.mjs without RootMind answers initialize with the install hint and exits", async () => {
    // Windows treats the names case-insensitively; drop every spelling.
    const env = Object.fromEntries(
      Object.entries(process.env).filter(([k]) => !/^(programfiles|programw6432)$/i.test(k)),
    );
    env.ProgramFiles = path.join(workDir, "empty-program-files");
    const init = initialize();
    const { code, stdout, stderr } = await runProcess([path.join(BUNDLE, "server", "index.mjs")], env, [init]);
    assert.equal(code, 0);
    const response = JSON.parse(stdout.trim());
    assertFriendlyError(response, init.id, "not_installed", RELEASES_URL);
    assert.match(stderr, /\[rootmind\] RootMind bridge for Claude Desktop/);
  });

  test("the token never appears on stderr or stdout", async () => {
    // Success, a refused token, a port change and a refused connection: every
    // path that logs.
    const server = await startServer({ token: TOKEN });
    const machine = machineWithServer(server);
    const lines = [initialize(), req("tools/list"), { jsonrpc: "2.0", method: "notifications/initialized" }];
    let result = await runProcess([RUN_BRIDGE, machine.control], process.env, lines);
    assert.equal(result.code, 0);
    assert.equal(result.stdout.trim().split("\n").length, 2);

    const wrongToken = await startServer({ token: "something-else" });
    machine.setStatus({}, { port: wrongToken.port });
    const failed = await runProcess([RUN_BRIDGE, machine.control], process.env, [req("tools/list")]);

    machine.setStatus({}, { port: await closedPort() });
    const refused = await runProcess(
      [RUN_BRIDGE, machine.control],
      process.env,
      [req("tools/list"), { jsonrpc: "2.0", method: "notifications/initialized" }],
    );
    await server.close();
    await wrongToken.close();

    for (const r of [result, failed, refused]) {
      assert.ok(r.stderr.length > 0);
      assert.ok(!r.stderr.includes(TOKEN), "token on stderr");
      assert.ok(!r.stdout.includes(TOKEN), "token on stdout");
    }
    assert.match(failed.stdout, /token_rejected/);
    assert.match(refused.stdout, /unreachable/);
    assert.match(refused.stderr, /Dropped a notification/);
    // The bridge sent the token only to the service, in the header.
    assert.equal(wrongToken.requests[0].headers.authorization, `Bearer ${TOKEN}`);
  });
});

// --- helpers and the manifest --------------------------------------------------

describe("discovery helpers", () => {
  test("the backend is looked for under %ProgramFiles%", () => {
    assert.equal(
      defaultBackendExe({ ProgramFiles: "C:\\Program Files" }),
      "C:\\Program Files\\RootMind\\backend\\rootmind-backend.exe",
    );
    assert.equal(
      defaultBackendExe({ ProgramFiles: "C:\\Program Files (x86)", ProgramW6432: "D:\\Programs" }),
      "D:\\Programs\\RootMind\\backend\\rootmind-backend.exe",
    );
    assert.equal(defaultBackendExe({}), null);
    assert.deepEqual(defaultStatusCommand({ ProgramFiles: "C:\\PF" }).args, ["service", "status", "--json"]);
    assert.equal(defaultStatusCommand({}), null);
  });

  test("a starting service is used", () => {
    const info = interpretStatus(statusJson("C:\\t.txt", {}, { state: "start_pending", port: 18400 }));
    assert.deepEqual(info, { port: 18400, tokenFile: "C:\\t.txt", state: "start_pending" });
  });
});

describe("manifest", () => {
  const manifest = JSON.parse(readFileSync(path.join(BUNDLE, "manifest.json"), "utf8"));

  test("lists exactly the tools of the RootMind tool registry", () => {
    const source = readFileSync(path.join(ROOT, "backend", "src", "rootmind", "tools.py"), "utf8");
    const registry = source.slice(source.indexOf("TOOLS: list"), source.indexOf("TOOL_NAMES:"));
    const names = [...registry.matchAll(/^ {8}"name": "([a-z_]+)",$/gm)].map((m) => m[1]);
    assert.deepEqual(names, TOOL_NAMES);
    assert.deepEqual(manifest.tools.map((t) => t.name), names);
    assert.equal(manifest.tools_generated, false);
  });

  test("starts the bridge with Node on Windows only", () => {
    assert.equal(manifest.server.type, "node");
    assert.equal(manifest.server.entry_point, "server/index.mjs");
    assert.deepEqual(manifest.server.mcp_config.args, ["${__dirname}/server/index.mjs"]);
    assert.deepEqual(manifest.compatibility.platforms, ["win32"]);
    assert.equal(manifest.user_config, undefined);
  });

  test("carries the version of the desktop app", () => {
    const pkg = JSON.parse(readFileSync(path.join(ROOT, "desktop", "package.json"), "utf8"));
    assert.equal(manifest.version, pkg.version);
  });

  test("uses the app icon", () => {
    const icon = readFileSync(path.join(BUNDLE, manifest.icon));
    const app = readFileSync(path.join(ROOT, "desktop", "src-tauri", "icons", "icon.png"));
    assert.ok(icon.equals(app), "packaging/mcpb/icon.png differs from desktop/src-tauri/icons/icon.png");
  });
});
