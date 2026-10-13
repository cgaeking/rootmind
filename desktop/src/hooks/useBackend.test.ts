import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../state/store";
import { jsonResponse, resetStore } from "../test/helpers";

vi.mock("../api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/backend")>()),
  serviceConnect: vi.fn(),
  sidecarStatus: vi.fn(),
}));

const backend = await import("../api/backend");
const { recheck } = await import("./useBackend");

const fetchMock = vi.fn<typeof fetch>();

/** A backend answering /health, / (with `auth`) and /status (401 unless `token`). */
function backendAt(base: string, opts: { auth?: "bearer" | "none"; token?: string } = {}) {
  fetchMock.mockImplementation(async (input, init) => {
    const url = String(input);
    if (!url.startsWith(base)) throw new TypeError("Failed to fetch");
    const path = url.slice(base.length);
    if (path === "/health") return jsonResponse({ status: "healthy" });
    if (path === "/") {
      return jsonResponse({ name: "RootMind API", version: "1.2.0", auth: opts.auth ?? "none" });
    }
    const sent = new Headers(init?.headers).get("Authorization");
    if (opts.token && sent !== `Bearer ${opts.token}`) {
      return jsonResponse({ detail: "Invalid token" }, 401);
    }
    return jsonResponse({ is_running: false });
  });
}

beforeEach(() => {
  resetStore();
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
  vi.mocked(backend.serviceConnect).mockReset();
  vi.mocked(backend.sidecarStatus).mockReset();
});

afterEach(async () => {
  // Let a check started by the test finish before the mocks are reset.
  await recheck();
  vi.unstubAllGlobals();
});

describe("recheck", () => {
  it("does nothing before a backend was chosen", async () => {
    await recheck();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(useStore.getState().conn).toBe("unknown");
  });

  it("goes online with the version, checking the token once", async () => {
    backendAt("http://a", { auth: "bearer", token: "t" });
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: "t", conn: "checking" });
    await recheck();
    const s = useStore.getState();
    expect(s.conn).toBe("online");
    expect(s.version).toBe("1.2.0");
    const paths = fetchMock.mock.calls.map((c) => String(c[0]));
    expect(paths.filter((p) => p === "http://a/status")).toHaveLength(1);
  });

  it("reports a rejected custom token as unauthorized", async () => {
    backendAt("http://a", { auth: "bearer", token: "right" });
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: "wrong", conn: "online" });
    await recheck();
    expect(useStore.getState().conn).toBe("unauthorized");
  });

  it("reports a backend that needs a token we do not have as unauthorized", async () => {
    backendAt("http://a", { auth: "bearer", token: "t" });
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: null, conn: "checking" });
    await recheck();
    expect(useStore.getState().conn).toBe("unauthorized");
  });

  it("marks an unreachable custom backend offline", async () => {
    backendAt("http://elsewhere");
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: null, conn: "online" });
    await recheck();
    expect(useStore.getState().conn).toBe("offline");
  });

  it("takes over the built-in backend's new port after a crash restart", async () => {
    backendAt("http://127.0.0.1:50002", { auth: "bearer", token: "t" });
    vi.mocked(backend.sidecarStatus).mockResolvedValue({
      state: "running",
      url: "http://127.0.0.1:50002",
      token: "t",
      exitCode: null,
      error: null,
      logTail: [],
    });
    useStore.setState({
      kind: "builtin",
      baseUrl: "http://127.0.0.1:50001",
      token: "t",
      conn: "online",
    });
    await recheck();
    const s = useStore.getState();
    expect(s.baseUrl).toBe("http://127.0.0.1:50002");
    // The URL change triggers another check (the heartbeat effect); run it here.
    await recheck();
    expect(useStore.getState().conn).toBe("online");
  });

  it("shows a restarting built-in backend as starting, a failed one as offline", async () => {
    backendAt("http://elsewhere");
    useStore.setState({ kind: "builtin", baseUrl: "http://127.0.0.1:50001", token: "t", conn: "online" });
    vi.mocked(backend.sidecarStatus).mockResolvedValue({
      state: "restarting",
      url: null,
      token: null,
      exitCode: 1,
      error: null,
      logTail: [],
    });
    await recheck();
    expect(useStore.getState().conn).toBe("starting");

    vi.mocked(backend.sidecarStatus).mockResolvedValue({
      state: "failed",
      url: null,
      token: null,
      exitCode: 3,
      error: "exited",
      logTail: ["boom"],
    });
    await recheck();
    expect(useStore.getState().conn).toBe("offline");
    expect(useStore.getState().sidecar?.error).toBe("exited");
  });

  it("asks Rust for the service's URL and token on every check", async () => {
    backendAt("http://127.0.0.1:8000", { auth: "bearer", token: "svc" });
    vi.mocked(backend.serviceConnect).mockResolvedValue({
      ok: true,
      url: "http://127.0.0.1:8000",
      token: "svc",
    });
    useStore.setState({ kind: "service", baseUrl: "", token: null, conn: "checking" });
    await recheck();
    await recheck();
    expect(useStore.getState().conn).toBe("online");
    expect(useStore.getState().token).toBe("svc");
    expect(backend.serviceConnect).toHaveBeenCalledTimes(2);
  });

  it("clears the service token when the listener check fails", async () => {
    backendAt("http://127.0.0.1:8000", { auth: "bearer", token: "svc" });
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: false, reason: "listener_mismatch" });
    useStore.setState({
      kind: "service",
      baseUrl: "http://127.0.0.1:8000",
      token: "svc",
      conn: "online",
    });
    await recheck();
    const s = useStore.getState();
    expect(s.conn).toBe("offline");
    expect(s.token).toBeNull();
    expect(s.baseUrl).toBe("");
    expect(s.serviceIssue).toBe("listener_mismatch");
    // No request reached the port.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("leaves a running service change alone", async () => {
    useStore.setState({ kind: "service", baseUrl: "http://x", token: "t", conn: "switching" });
    await recheck();
    expect(backend.serviceConnect).not.toHaveBeenCalled();
    expect(useStore.getState().conn).toBe("switching");
  });

  it("drops a result for a backend that is no longer in use", async () => {
    fetchMock.mockImplementation(async (input) => {
      // The user switches to another backend while /health is in flight.
      useStore.setState({ kind: "custom", baseUrl: "http://b", conn: "checking" });
      if (String(input).endsWith("/health")) return jsonResponse({ status: "healthy" });
      return jsonResponse({ name: "RootMind API", version: "1.2.0", auth: "none" });
    });
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: null, conn: "checking" });
    await recheck();
    // http://a answered, but that answer must not mark http://b online.
    expect(useStore.getState().baseUrl).toBe("http://b");
    expect(useStore.getState().conn).toBe("checking");
  });
});
