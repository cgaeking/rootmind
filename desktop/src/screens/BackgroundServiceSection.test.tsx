import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../state/store";
import { resetStore, serviceInfoFixture } from "../test/helpers";
import type { ServiceInfo, ServiceResult } from "../api/backend";

vi.mock("../api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/backend")>()),
  inTauri: () => true,
  serviceInfo: vi.fn(),
  serviceEnable: vi.fn(),
  serviceDisable: vi.fn(),
  serviceUpdate: vi.fn(),
  serviceSetMcp: vi.fn(),
  serviceDetails: vi.fn(),
  serviceMcpToken: vi.fn(),
}));

// The switch itself is tested in backendActions.test.ts; here only the outcome matters.
vi.mock("../state/backendActions", () => ({
  runServiceChange: vi.fn(async (op: () => Promise<ServiceResult>) => ({
    kind: "result",
    result: await op(),
  })),
}));

const backend = await import("../api/backend");
const { BackgroundServiceSection } = await import("./BackgroundServiceSection");

const ours: ServiceInfo = serviceInfoFixture({
  installed: true,
  ours: true,
  state: "running",
  port: 8000,
  url: "http://127.0.0.1:8000",
  tokenReadable: true,
});

function failed(code: number, cancelled = false): ServiceResult {
  return { ok: false, cancelled, code, backend: null };
}

beforeEach(() => {
  resetStore();
  useStore.setState({
    kind: "builtin",
    baseUrl: "http://127.0.0.1:50001",
    token: "s",
    conn: "online",
    appMode: { version: "1.2.0", dev: false, builtinAvailable: true },
  });
  vi.mocked(backend.serviceInfo).mockResolvedValue(serviceInfoFixture());
});

afterEach(() => {
  cleanup();
});

function toggle(): HTMLInputElement {
  return screen.getByRole("switch", { name: "Run RootMind in the background" }) as HTMLInputElement;
}

describe("BackgroundServiceSection", () => {
  it("turns the service on with MCP and reports a cancelled prompt", async () => {
    vi.mocked(backend.serviceEnable).mockResolvedValue(failed(1223, true));
    render(<BackgroundServiceSection />);
    expect(await screen.findByText("Off. RootMind runs while the app is open.")).toBeTruthy();
    expect(toggle().disabled).toBe(false);

    fireEvent.click(toggle());
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Also serve MCP over HTTP (for n8n and other remote agents)" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));

    await waitFor(() => expect(backend.serviceEnable).toHaveBeenCalledWith(true));
    expect(await screen.findByText("Nothing was changed.")).toBeTruthy();
  });

  it("shows the message for a failure code and the details on request", async () => {
    vi.mocked(backend.serviceEnable).mockResolvedValue(failed(4));
    vi.mocked(backend.serviceDetails).mockResolvedValue("service status --json (exit code 0):\n{}");
    render(<BackgroundServiceSection />);
    await screen.findByText("Off. RootMind runs while the app is open.");

    fireEvent.click(toggle());
    fireEvent.click(screen.getByRole("button", { name: "Turn on" }));
    expect(
      await screen.findByText("The port for the background service is in use by another program."),
    ).toBeTruthy();
    expect(backend.serviceEnable).toHaveBeenCalledWith(false);

    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(await screen.findByText(/service status --json \(exit code 0\)/)).toBeTruthy();
  });

  it("turns the service off, optionally deleting its data", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue(ours);
    vi.mocked(backend.serviceDisable).mockResolvedValue({
      ok: true,
      backend: { url: "http://127.0.0.1:50002", token: "s" },
    });
    render(<BackgroundServiceSection />);
    await waitFor(() => expect(toggle().checked).toBe(true));

    fireEvent.click(toggle());
    fireEvent.click(
      screen.getByRole("checkbox", { name: "Also delete the service's data (index, settings, MCP token)" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Turn off" }));
    await waitFor(() => expect(backend.serviceDisable).toHaveBeenCalledWith(true));
  });

  it("is unavailable when the service belongs to another account", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue({
      ...ours,
      tokenReadable: false,
      blockedReason: "other_account",
    });
    render(<BackgroundServiceSection />);
    expect(
      await screen.findByText("The background service belongs to another account on this computer."),
    ).toBeTruthy();
    expect(toggle().disabled).toBe(true);
    expect(screen.queryByRole("button", { name: "Repair" })).toBeNull();
  });

  it("is unavailable for an account that is not an administrator", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue(
      serviceInfoFixture({ canElevate: false, blockedReason: "not_admin" }),
    );
    render(<BackgroundServiceSection />);
    expect(
      await screen.findByText("Turning on the background service needs an administrator account."),
    ).toBeTruthy();
    expect(toggle().disabled).toBe(true);
  });

  it("offers an update when the service is older than the app", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue(ours);
    vi.mocked(backend.serviceUpdate).mockResolvedValue({
      ok: true,
      backend: { url: "http://127.0.0.1:8000", token: "v" },
    });
    useStore.setState({
      kind: "service",
      baseUrl: "http://127.0.0.1:8000",
      token: "v",
      conn: "online",
      version: "1.2.0",
      appMode: { version: "1.3.0", dev: false, builtinAvailable: true },
    });
    render(<BackgroundServiceSection />);
    expect(
      await screen.findByText("The background service runs version 1.2.0; this app is version 1.3.0."),
    ).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Update service" }));
    await waitFor(() => expect(backend.serviceUpdate).toHaveBeenCalledTimes(1));
  });

  it("keeps a custom backend in use and says what turning the service off changes", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue(ours);
    vi.mocked(backend.serviceSetMcp).mockResolvedValue({
      ok: true,
      backend: { url: "http://127.0.0.1:8000", token: "v" },
    });
    useStore.setState({ kind: "custom", baseUrl: "http://example.test:9000", token: null, conn: "offline" });
    const { runServiceChange } = await import("../state/backendActions");
    render(<BackgroundServiceSection />);
    expect(
      await screen.findByText(
        "The background service is installed, but the app uses a custom backend (see Advanced).",
      ),
    ).toBeTruthy();
    // Repair would switch away from the custom backend.
    expect(screen.queryByRole("button", { name: "Repair" })).toBeNull();

    fireEvent.click(screen.getByRole("checkbox", { name: "Serve MCP over HTTP (for n8n and other remote agents)" }));
    await waitFor(() => expect(backend.serviceSetMcp).toHaveBeenCalledWith(true));
    expect(vi.mocked(runServiceChange).mock.lastCall?.[1]).toMatchObject({ keepBackend: true });

    fireEvent.click(toggle());
    expect(
      screen.getByText(
        "The app then uses its built-in backend instead of the custom backend set under Advanced.",
      ),
    ).toBeTruthy();
  });

  it("shows the MCP endpoint and reveals the token only on request", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue({
      ...ours,
      mcp: { installed: true, state: "running", port: 3000, url: "http://127.0.0.1:3000/mcp" },
    });
    vi.mocked(backend.serviceMcpToken).mockResolvedValue("mcp-secret");
    useStore.setState({ kind: "service", baseUrl: "http://127.0.0.1:8000", token: "v", conn: "online" });
    render(<BackgroundServiceSection />);
    expect(await screen.findByDisplayValue("http://127.0.0.1:3000/mcp")).toBeTruthy();
    expect(screen.queryByDisplayValue("mcp-secret")).toBeNull();
    expect(backend.serviceMcpToken).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole("button", { name: "Show token" }));
    expect(await screen.findByDisplayValue("mcp-secret")).toBeTruthy();
    expect(screen.getByText(/Header Value: Bearer mcp-secret/)).toBeTruthy();
  });
});
