import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { resetStore } from "../test/helpers";
import { useStore } from "./store";
import type { AutoInputs } from "./autoSetup";

vi.mock("../api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/backend")>()),
  inTauri: () => true,
  serviceUpdate: vi.fn(),
  serviceConnect: vi.fn(),
  serviceInfo: vi.fn(),
}));

const backend = await import("../api/backend");
const { autoServiceAction, checkAutoSetup, resetAutoSetup } = await import("./autoSetup");

const base: AutoInputs = {
  platform: "linux",
  dev: false,
  kind: "service",
  conn: "offline",
  problem: "notInstalled",
  info: null,
  serviceVersion: null,
  appVersion: "2.3.0",
  attempted: new Set(),
};

describe("autoServiceAction", () => {
  it("sets the service up on Linux when it was never set up", () => {
    expect(autoServiceAction(base)).toBe("setup");
  });

  it("updates a service older than the app", () => {
    const online = { ...base, conn: "online" as const, problem: null };
    expect(autoServiceAction({ ...online, serviceVersion: "2.2.0" })).toBe("update");
    expect(autoServiceAction({ ...online, serviceVersion: "2.3.0rc1" })).toBe("update");
    expect(autoServiceAction({ ...online, serviceVersion: "2.3.0" })).toBeNull();
    expect(autoServiceAction({ ...online, serviceVersion: "2.4.0" })).toBeNull();
    expect(autoServiceAction({ ...online, serviceVersion: null })).toBeNull();
  });

  it("does nothing on Windows, in a debug build, for a custom backend or twice", () => {
    expect(autoServiceAction({ ...base, platform: "windows" })).toBeNull();
    expect(autoServiceAction({ ...base, platform: null })).toBeNull();
    expect(autoServiceAction({ ...base, dev: true })).toBeNull();
    expect(autoServiceAction({ ...base, kind: "custom" })).toBeNull();
    expect(autoServiceAction({ ...base, attempted: new Set(["setup" as const]) })).toBeNull();
  });

  it("leaves other problems to Repair", () => {
    for (const problem of ["notRunning", "noAnswer", "portBusy", "foreign", "otherAccount"] as const) {
      expect(autoServiceAction({ ...base, problem })).toBeNull();
    }
  });
});

describe("checkAutoSetup", () => {
  beforeEach(() => {
    resetStore();
    resetAutoSetup();
    vi.mocked(backend.serviceUpdate).mockReset();
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: false, reason: "not_installed" });
    vi.mocked(backend.serviceInfo).mockRejectedValue("no info");
    useStore.setState({
      appMode: { version: "2.3.0", dev: false, platform: "linux" },
      kind: "service",
      conn: "offline",
      serviceProblem: "notInstalled",
    });
  });

  afterEach(() => {
    resetAutoSetup();
  });

  it("runs the set-up once and shows why it failed", async () => {
    vi.mocked(backend.serviceUpdate).mockResolvedValue({
      ok: false,
      cancelled: false,
      code: 11,
      backend: null,
    });
    const run = checkAutoSetup(null);
    expect(useStore.getState().autoService).toEqual({
      action: "setup",
      status: "running",
      message: null,
    });
    await run;
    expect(backend.serviceUpdate).toHaveBeenCalledTimes(1);
    const auto = useStore.getState().autoService;
    expect(auto?.status).toBe("failed");
    expect(auto?.message).toMatch(/systemd user manager/);
    // Once per app run: Set up stays as the fallback.
    expect(checkAutoSetup(null)).toBeNull();
    expect(backend.serviceUpdate).toHaveBeenCalledTimes(1);
  });

  it("clears the state after a successful set-up", async () => {
    vi.mocked(backend.serviceUpdate).mockResolvedValue({
      ok: true,
      backend: { url: "http://127.0.0.1:8000", token: "t" },
    });
    await checkAutoSetup(null);
    expect(useStore.getState().autoService).toBeNull();
  });

  it("does nothing on Windows", () => {
    useStore.setState({ appMode: { version: "2.3.0", dev: false, platform: "windows" } });
    expect(checkAutoSetup(null)).toBeNull();
    expect(useStore.getState().autoService).toBeNull();
  });
});
