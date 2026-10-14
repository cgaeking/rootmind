import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../state/store";
import { resetStore, serviceInfoFixture } from "../test/helpers";
import type { ServiceInfo, ServiceResult } from "../api/backend";

vi.mock("../api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/backend")>()),
  inTauri: () => true,
  serviceInfo: vi.fn(),
  serviceConnect: vi.fn(),
  serviceUpdate: vi.fn(),
  serviceSetMcp: vi.fn(),
  serviceDetails: vi.fn(),
  serviceMcpToken: vi.fn(),
}));

// The change itself is tested in backendActions.test.ts; here only the outcome matters.
vi.mock("../state/backendActions", () => ({
  runServiceChange: vi.fn(async (op: () => Promise<ServiceResult>) => ({
    kind: "result",
    result: await op(),
  })),
}));

const backend = await import("../api/backend");
const { ServiceSection } = await import("./ServiceSection");

const SERVICE = "http://127.0.0.1:8000";
const MCP = "http://127.0.0.1:8010/mcp";

const ours: ServiceInfo = serviceInfoFixture({
  installed: true,
  ours: true,
  state: "running",
  port: 8000,
  url: SERVICE,
  tokenReadable: true,
  mcp: { installed: true, state: "running", port: 8010, url: MCP },
});

function failed(code: number, cancelled = false): ServiceResult {
  return { ok: false, cancelled, code, backend: null };
}

const writeText = vi.fn(async (text: string) => {
  void text;
});

beforeEach(() => {
  resetStore();
  useStore.setState({
    kind: "service",
    baseUrl: SERVICE,
    token: "v",
    conn: "online",
    version: "1.3.0",
    appMode: { version: "1.3.0", dev: false },
  });
  vi.mocked(backend.serviceInfo).mockResolvedValue(ours);
  vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: true, url: SERVICE, token: "v" });
  writeText.mockClear();
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});

afterEach(() => {
  cleanup();
});

describe("ServiceSection", () => {
  it("shows the running service read-only, without an on/off switch", async () => {
    render(<ServiceSection />);
    expect(await screen.findByText("Running")).toBeTruthy();
    expect(screen.getByText(SERVICE)).toBeTruthy();
    expect(screen.getByText("1.3.0")).toBeTruthy();
    expect(screen.queryByRole("switch")).toBeNull();
    expect(screen.queryByRole("button", { name: "Repair" })).toBeNull();
  });

  it("shows the MCP endpoint and copies the token without displaying it", async () => {
    vi.mocked(backend.serviceMcpToken).mockResolvedValue("mcp-secret");
    render(<ServiceSection />);
    expect(await screen.findByDisplayValue(MCP)).toBeTruthy();
    const toggle = screen.getByRole("checkbox", {
      name: "Serve MCP over HTTP (for Claude Desktop, n8n and other agents)",
    }) as HTMLInputElement;
    expect(toggle.checked).toBe(true);
    expect(backend.serviceMcpToken).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Copy token" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith("mcp-secret"));
    expect(screen.queryByDisplayValue("mcp-secret")).toBeNull();
    expect(screen.queryByText(/mcp-secret/)).toBeNull();
  });

  it("turns MCP over HTTP off and reports a cancelled prompt", async () => {
    vi.mocked(backend.serviceSetMcp).mockResolvedValue(failed(1223, true));
    render(<ServiceSection />);
    await screen.findByDisplayValue(MCP);
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Serve MCP over HTTP (for Claude Desktop, n8n and other agents)" }),
    );
    await waitFor(() => expect(backend.serviceSetMcp).toHaveBeenCalledWith(false));
    expect(await screen.findByText("Nothing was changed.")).toBeTruthy();
  });

  it("explains a stopped service and repairs it; a failure offers the details", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue({ ...ours, state: "stopped" });
    vi.mocked(backend.serviceUpdate).mockResolvedValue(failed(4));
    vi.mocked(backend.serviceDetails).mockResolvedValue("service status --json (exit code 0):\n{}");
    useStore.setState({ baseUrl: "", token: null, conn: "offline", serviceProblem: "notRunning" });
    render(<ServiceSection />);
    expect(await screen.findByText(/The RootMind service is not running/)).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Repair" }));
    await waitFor(() => expect(backend.serviceUpdate).toHaveBeenCalledTimes(1));
    expect(
      await screen.findByText("The port of the RootMind service is in use by another program."),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(await screen.findByText(/service status --json \(exit code 0\)/)).toBeTruthy();
  });

  it("offers Repair for a missing service", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue(serviceInfoFixture());
    vi.mocked(backend.serviceUpdate).mockResolvedValue({
      ok: true,
      backend: { url: SERVICE, token: "v" },
    });
    useStore.setState({ baseUrl: "", token: null, conn: "offline", serviceProblem: "notInstalled" });
    render(<ServiceSection />);
    expect(await screen.findByText(/The RootMind service is not installed/)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Check again" })).toBeTruthy();
    // No MCP settings for a service that is not there.
    expect(screen.queryByRole("checkbox")).toBeNull();
    fireEvent.click(await screen.findByRole("button", { name: "Repair" }));
    await waitFor(() => expect(backend.serviceUpdate).toHaveBeenCalledTimes(1));
  });

  it("asks to close the program on the service's port instead of offering Repair", async () => {
    useStore.setState({ baseUrl: "", token: null, conn: "offline", serviceProblem: "portBusy" });
    render(<ServiceSection />);
    expect(await screen.findByText(/Close that program, then choose Check again/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Repair" })).toBeNull();
  });

  it("explains that another account's RootMind must be reinstalled from this account", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue({
      ...ours,
      tokenReadable: false,
      blockedReason: "other_account",
    });
    useStore.setState({ baseUrl: "", token: null, conn: "offline", serviceProblem: "otherAccount" });
    render(<ServiceSection />);
    expect(await screen.findByText(/uninstall RootMind \(Settings › Apps\)/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Repair" })).toBeNull();
  });

  it("tells an account without administrator rights whom to ask", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue({ ...ours, state: "stopped", canElevate: false });
    useStore.setState({ baseUrl: "", token: null, conn: "offline", serviceProblem: "notRunning" });
    render(<ServiceSection />);
    expect(await screen.findByText(/Repairing needs an administrator account/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Repair" })).toBeNull();
  });

  it("shows the service's state from service_info while a custom backend is in use", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue({ ...ours, state: "stopped" });
    useStore.setState({ kind: "custom", baseUrl: "http://example.test:9000", token: null, conn: "online" });
    render(<ServiceSection />);
    expect(await screen.findByText(/uses the backend at another address/)).toBeTruthy();
    expect(await screen.findByText(/The RootMind service is not running/)).toBeTruthy();
  });

  it("offers an update when the service is older than the app", async () => {
    vi.mocked(backend.serviceUpdate).mockResolvedValue({
      ok: true,
      backend: { url: SERVICE, token: "v" },
    });
    useStore.setState({ version: "1.2.0" });
    render(<ServiceSection />);
    expect(
      await screen.findByText("The RootMind service runs version 1.2.0; this app is version 1.3.0."),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Update service" }));
    await waitFor(() => expect(backend.serviceUpdate).toHaveBeenCalledTimes(1));
  });
});
