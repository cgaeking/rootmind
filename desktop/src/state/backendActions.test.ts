import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "./store";
import { jsonResponse, resetStore, serviceInfoFixture } from "../test/helpers";
import type { ServiceResult, SidecarStatus } from "../api/backend";

vi.mock("../api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/backend")>()),
  serviceConnect: vi.fn(),
  serviceInfo: vi.fn(),
  sidecarStart: vi.fn(),
  sidecarRestart: vi.fn(),
  sidecarStatus: vi.fn(),
}));

const backend = await import("../api/backend");
const actions = await import("./backendActions");
const { recheck } = await import("../hooks/useBackend");

const SIDECAR = "http://127.0.0.1:50001";
const SERVICE = "http://127.0.0.1:8000";

function sidecar(over: Partial<SidecarStatus> = {}): SidecarStatus {
  return { state: "running", url: SIDECAR, token: "s", exitCode: null, error: null, logTail: [], ...over };
}

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  resetStore();
  for (const fn of [
    backend.serviceConnect,
    backend.serviceInfo,
    backend.sidecarStart,
    backend.sidecarRestart,
    backend.sidecarStatus,
  ]) {
    vi.mocked(fn).mockReset();
  }
  vi.mocked(backend.serviceInfo).mockResolvedValue(serviceInfoFixture());
  // Every backend answers; the tests look at which one is chosen.
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input) =>
    String(input).endsWith("/")
      ? jsonResponse({ name: "RootMind API", version: "1.2.0", auth: "bearer" })
      : jsonResponse({ status: "healthy", is_running: false }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(async () => {
  // Let a check started by the test finish before the mocks are reset.
  await recheck();
  vi.unstubAllGlobals();
});

describe("startBuiltin", () => {
  it("uses the started backend and stores the auto mode", async () => {
    vi.mocked(backend.sidecarStart).mockResolvedValue({ url: SIDECAR, token: "s" });
    expect(await actions.startBuiltin()).toBe(true);
    const s = useStore.getState();
    expect(s.kind).toBe("builtin");
    expect(s.baseUrl).toBe(SIDECAR);
    expect(s.token).toBe("s");
    expect(localStorage.getItem("rlm.backendMode")).toBe("auto");
  });

  it("shows the error of a failed start", async () => {
    vi.mocked(backend.sidecarStart).mockRejectedValue("Windows security blocked the RootMind backend.");
    vi.mocked(backend.sidecarStatus).mockResolvedValue(
      sidecar({ state: "failed", url: null, token: null, error: "blocked" }),
    );
    expect(await actions.startBuiltin()).toBe(false);
    const s = useStore.getState();
    expect(s.kind).toBe("builtin");
    expect(s.conn).toBe("offline");
    expect(s.sidecar?.error).toBe("blocked");
  });
});

describe("runServiceChange", () => {
  const kinds = { successKind: "service" as const, fallbackKind: "builtin" as const };

  it("switches to the service after turning it on", async () => {
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: true, url: SERVICE, token: "v" });
    useStore.setState({ kind: "builtin", baseUrl: SIDECAR, token: "s", conn: "online" });
    const op = vi.fn(
      async (): Promise<ServiceResult> => ({ ok: true, backend: { url: SERVICE, token: "v" } }),
    );
    const seen: string[] = [];
    const unsub = useStore.subscribe((s) => seen.push(s.conn));
    const out = await actions.runServiceChange(op, { ...kinds, switching: true });
    unsub();
    expect(out).toMatchObject({ kind: "result", result: { ok: true } });
    expect(seen[0]).toBe("switching");
    const s = useStore.getState();
    expect(s.kind).toBe("service");
    expect(s.baseUrl).toBe(SERVICE);
    expect(s.token).toBe("v");
  });

  it("keeps the built-in backend Rust restarted after a cancel", async () => {
    vi.mocked(backend.sidecarStatus).mockResolvedValue(sidecar({ url: "http://127.0.0.1:50009" }));
    useStore.setState({ kind: "builtin", baseUrl: SIDECAR, token: "s", conn: "online" });
    const out = await actions.runServiceChange(
      async () => ({
        ok: false,
        cancelled: true,
        code: 1223,
        backend: { url: "http://127.0.0.1:50009", token: "s" },
      }),
      { ...kinds, switching: true },
    );
    expect(out).toMatchObject({ kind: "result", result: { ok: false, cancelled: true } });
    const s = useStore.getState();
    expect(s.kind).toBe("builtin");
    expect(s.baseUrl).toBe("http://127.0.0.1:50009");
    expect(s.conn).not.toBe("switching");
  });

  it("keeps the service when a change failed but the service still answers", async () => {
    vi.mocked(backend.sidecarStatus).mockResolvedValue(sidecar({ state: "stopped", url: null, token: null }));
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: true, url: SERVICE, token: "v" });
    useStore.setState({ kind: "service", baseUrl: SERVICE, token: "v", conn: "online" });
    await actions.runServiceChange(
      async () => ({ ok: false, cancelled: false, code: 4, backend: { url: SERVICE, token: "v" } }),
      { successKind: "service", fallbackKind: "service", switching: true },
    );
    expect(useStore.getState().kind).toBe("service");
    expect(useStore.getState().conn).not.toBe("switching");
  });

  it("shows the fallback kind as unreachable when no backend runs", async () => {
    vi.mocked(backend.sidecarStatus).mockResolvedValue(
      sidecar({ state: "failed", url: null, token: null, error: "exited", exitCode: 1 }),
    );
    useStore.setState({ kind: "builtin", baseUrl: SIDECAR, token: "s", conn: "online" });
    await actions.runServiceChange(
      async () => ({ ok: false, cancelled: false, code: 7, backend: null }),
      { ...kinds, switching: true },
    );
    // Let the follow-up check finish.
    await vi.waitFor(() => expect(useStore.getState().conn).toBe("offline"));
    expect(useStore.getState().kind).toBe("builtin");
    expect(useStore.getState().baseUrl).toBe("");
  });

  it("falls back to the built-in backend when turning off removed the service but its start failed", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue(serviceInfoFixture({ installed: false }));
    vi.mocked(backend.sidecarStatus).mockResolvedValue(
      sidecar({ state: "failed", url: null, token: null, error: "timeout" }),
    );
    useStore.setState({ kind: "service", baseUrl: SERVICE, token: "v", conn: "online" });
    const out = await actions.runServiceChange(
      async () => {
        throw "The background service was removed, but the built-in backend did not start: timeout";
      },
      { successKind: "builtin", fallbackKind: "service", switching: true },
    );
    expect(out.kind).toBe("error");
    await vi.waitFor(() => expect(useStore.getState().conn).toBe("offline"));
    expect(useStore.getState().kind).toBe("builtin");
  });
});

describe("applyDecision", () => {
  it("asks for a URL in a development build without contacting any backend", async () => {
    localStorage.setItem("rlm.baseUrl", "http://127.0.0.1:8000");
    await actions.applyDecision({ action: "ask_url", url: "http://127.0.0.1:8000", token: null });
    await recheck();
    const s = useStore.getState();
    expect(s.kind).toBe("custom");
    expect(s.baseUrl).toBe("");
    expect(s.conn).toBe("offline");
    expect(fetchMock).not.toHaveBeenCalled();
    // Nothing persisted: the dialog stores the address the user connects to.
    expect(localStorage.getItem("rlm.backendMode")).toBeNull();
  });
});

describe("switchToDefaultBackend", () => {
  it("forgets a custom backend and uses the service when it is ours", async () => {
    localStorage.setItem("rlm.backendMode", "custom");
    localStorage.setItem("rlm.baseUrl", "http://a");
    localStorage.setItem("rlm.token", "t");
    useStore.setState({
      kind: "custom",
      baseUrl: "http://a",
      conn: "online",
      appMode: { version: "1.2.0", dev: false, builtinAvailable: true },
    });
    vi.mocked(backend.serviceInfo).mockResolvedValue(
      serviceInfoFixture({ installed: true, ours: true, tokenReadable: true, url: SERVICE }),
    );
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: true, url: SERVICE, token: "v" });
    await actions.switchToDefaultBackend();
    expect(useStore.getState().kind).toBe("service");
    expect(localStorage.getItem("rlm.backendMode")).toBe("auto");
    expect(localStorage.getItem("rlm.baseUrl")).toBeNull();
    expect(localStorage.getItem("rlm.token")).toBeNull();
  });

  it("starts the built-in backend without a service", async () => {
    useStore.setState({
      kind: "custom",
      baseUrl: "http://a",
      conn: "offline",
      appMode: { version: "1.2.0", dev: false, builtinAvailable: true },
    });
    vi.mocked(backend.sidecarStart).mockResolvedValue({ url: SIDECAR, token: "s" });
    await actions.switchToDefaultBackend();
    expect(useStore.getState().kind).toBe("builtin");
    expect(useStore.getState().baseUrl).toBe(SIDECAR);
  });
});
