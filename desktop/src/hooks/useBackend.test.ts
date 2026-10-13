import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../state/store";
import { jsonResponse, resetStore, serviceInfoFixture } from "../test/helpers";

vi.mock("../api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/backend")>()),
  inTauri: vi.fn(() => true),
  serviceConnect: vi.fn(),
  serviceInfo: vi.fn(),
}));

const backend = await import("../api/backend");
const { recheck } = await import("./useBackend");

const fetchMock = vi.fn<typeof fetch>();

const OURS = serviceInfoFixture({
  installed: true,
  ours: true,
  state: "running",
  port: 8000,
  url: "http://127.0.0.1:8000",
  tokenReadable: true,
});

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
  vi.mocked(backend.serviceInfo).mockReset();
  vi.mocked(backend.serviceInfo).mockResolvedValue(OURS);
  vi.mocked(backend.inTauri).mockReturnValue(true);
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
    expect(s.serviceProblem).toBe("portBusy");
    // No request reached the port.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it("names each reason the service cannot be used", async () => {
    const cases = [
      ["not_installed", serviceInfoFixture(), "notInstalled"],
      ["not_running", { ...OURS, state: "stopped" }, "notRunning"],
      ["token_unreadable", { ...OURS, tokenReadable: false, blockedReason: "other_account" }, "otherAccount"],
      // Another program's service: service_connect sees no service of ours.
      ["not_installed", { ...OURS, ours: false, blockedReason: "foreign_service" }, "foreign"],
    ] as const;
    for (const [reason, info, problem] of cases) {
      vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: false, reason });
      vi.mocked(backend.serviceInfo).mockResolvedValue(info);
      useStore.setState({ kind: "service", baseUrl: "", token: null, conn: "checking" });
      await recheck();
      expect(useStore.getState().conn).toBe("offline");
      expect(useStore.getState().serviceProblem).toBe(problem);
    }
  });

  it("reports a running service whose API does not answer, and clears the problem once it does", async () => {
    vi.mocked(backend.serviceConnect).mockResolvedValue({
      ok: true,
      url: "http://127.0.0.1:8000",
      token: "svc",
    });
    backendAt("http://elsewhere");
    useStore.setState({ kind: "service", baseUrl: "http://127.0.0.1:8000", token: "svc", conn: "online" });
    await recheck();
    expect(useStore.getState().conn).toBe("offline");
    expect(useStore.getState().serviceProblem).toBe("noAnswer");

    backendAt("http://127.0.0.1:8000", { auth: "bearer", token: "svc" });
    await recheck();
    expect(useStore.getState().conn).toBe("online");
    expect(useStore.getState().serviceProblem).toBeNull();
  });

  it("has no service to reach in the browser preview", async () => {
    vi.mocked(backend.inTauri).mockReturnValue(false);
    useStore.setState({ kind: "service", baseUrl: "", token: null, conn: "checking" });
    await recheck();
    expect(useStore.getState().conn).toBe("offline");
    expect(useStore.getState().serviceProblem).toBe("unsupported");
    expect(backend.serviceConnect).not.toHaveBeenCalled();
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
