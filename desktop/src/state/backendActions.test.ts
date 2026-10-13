import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "./store";
import { jsonResponse, resetStore, serviceInfoFixture } from "../test/helpers";
import type { ServiceResult } from "../api/backend";

vi.mock("../api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/backend")>()),
  inTauri: () => true,
  serviceConnect: vi.fn(),
  serviceInfo: vi.fn(),
}));

const backend = await import("../api/backend");
const actions = await import("./backendActions");
const { recheck } = await import("../hooks/useBackend");

const SERVICE = "http://127.0.0.1:8000";
const ours = serviceInfoFixture({
  installed: true,
  ours: true,
  state: "running",
  port: 8000,
  url: SERVICE,
  tokenReadable: true,
});

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  resetStore();
  vi.mocked(backend.serviceConnect).mockReset();
  vi.mocked(backend.serviceInfo).mockReset();
  vi.mocked(backend.serviceInfo).mockResolvedValue(ours);
  vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: true, url: SERVICE, token: "v" });
  // Every backend answers; the tests look at which one is chosen.
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input) =>
    String(input).endsWith("/")
      ? jsonResponse({ name: "RootMind API", version: "1.3.0", auth: "bearer" })
      : jsonResponse({ status: "healthy", is_running: false }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(async () => {
  // Let a check started by the test finish before the mocks are reset.
  await recheck();
  vi.unstubAllGlobals();
});

describe("connectService", () => {
  it("connects with the URL and token from Rust and stores only the auto mode", async () => {
    localStorage.setItem("rlm.backendMode", "custom");
    localStorage.setItem("rlm.baseUrl", "http://a");
    localStorage.setItem("rlm.token", "t");
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: "t", conn: "online" });

    expect(await actions.connectService()).toBe(true);
    const s = useStore.getState();
    expect(s.kind).toBe("service");
    expect(s.baseUrl).toBe(SERVICE);
    expect(s.token).toBe("v");
    expect(s.serviceProblem).toBeNull();
    expect(localStorage.getItem("rlm.backendMode")).toBe("auto");
    expect(localStorage.getItem("rlm.baseUrl")).toBeNull();
    expect(localStorage.getItem("rlm.token")).toBeNull();
  });

  it("reports a stopped service as a problem", async () => {
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: false, reason: "not_running" });
    vi.mocked(backend.serviceInfo).mockResolvedValue({ ...ours, state: "stopped" });
    expect(await actions.connectService()).toBe(false);
    const s = useStore.getState();
    expect(s.conn).toBe("offline");
    expect(s.serviceProblem).toBe("notRunning");
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe("connectCustom", () => {
  it("uses and remembers the address and token, and clears a service problem", async () => {
    useStore.setState({ kind: "service", conn: "offline", serviceProblem: "notInstalled" });
    await actions.connectCustom(" http://10.0.0.5:8000/ ", " tok ");
    const s = useStore.getState();
    expect(s.kind).toBe("custom");
    expect(s.baseUrl).toBe("http://10.0.0.5:8000");
    expect(s.token).toBe("tok");
    expect(s.serviceProblem).toBeNull();
    expect(localStorage.getItem("rlm.baseUrl")).toBe("http://10.0.0.5:8000");
    expect(backend.serviceConnect).not.toHaveBeenCalled();
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
    expect(backend.serviceConnect).not.toHaveBeenCalled();
    // Nothing persisted: the dialog stores the address the user connects to.
    expect(localStorage.getItem("rlm.backendMode")).toBeNull();
  });

  it("connects to the service", async () => {
    await actions.applyDecision({ action: "service" });
    expect(useStore.getState().kind).toBe("service");
    expect(useStore.getState().conn).toBe("online");
  });
});

describe("runServiceChange", () => {
  it("is 'switching' while the service changes and takes over the URL and token Rust returns", async () => {
    useStore.setState({ kind: "service", baseUrl: SERVICE, token: "v", conn: "online" });
    vi.mocked(backend.serviceConnect).mockResolvedValue({
      ok: true,
      url: "http://127.0.0.1:8001",
      token: "w",
    });
    const op = vi.fn(
      async (): Promise<ServiceResult> => ({
        ok: true,
        backend: { url: "http://127.0.0.1:8001", token: "w" },
      }),
    );
    const seen: string[] = [];
    const unsub = useStore.subscribe((s) => seen.push(s.conn));
    const out = await actions.runServiceChange(op);
    unsub();
    expect(out).toMatchObject({ kind: "result", result: { ok: true } });
    expect(seen[0]).toBe("switching");
    const s = useStore.getState();
    expect(s.kind).toBe("service");
    expect(s.baseUrl).toBe("http://127.0.0.1:8001");
    expect(s.token).toBe("w");
    expect(s.conn).not.toBe("switching");
  });

  it("keeps the connection state with switching: false (the backend dialog's repair)", async () => {
    useStore.setState({ kind: "service", baseUrl: "", token: null, conn: "offline" });
    const seen: string[] = [];
    const unsub = useStore.subscribe((s) => seen.push(s.conn));
    await actions.runServiceChange(
      async () => ({ ok: false, cancelled: true, code: 1223, backend: null }),
      { switching: false },
    );
    unsub();
    expect(seen).not.toContain("switching");
  });

  it("leaves a custom backend in use", async () => {
    useStore.setState({ kind: "custom", baseUrl: "http://a", token: null, conn: "online" });
    const seen: string[] = [];
    const unsub = useStore.subscribe((s) => seen.push(s.conn));
    await actions.runServiceChange(async () => ({ ok: true, backend: { url: SERVICE, token: "v" } }));
    unsub();
    expect(seen).not.toContain("switching");
    expect(useStore.getState().kind).toBe("custom");
    expect(useStore.getState().baseUrl).toBe("http://a");
  });

  it("returns the message of a failed command and leaves 'switching'", async () => {
    useStore.setState({ kind: "service", baseUrl: SERVICE, token: "v", conn: "online" });
    const out = await actions.runServiceChange(async () => {
      throw "Could not start the elevated process";
    });
    expect(out).toEqual({ kind: "error", message: "Could not start the elevated process" });
    expect(useStore.getState().conn).not.toBe("switching");
  });
});
