import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { useStore } from "../state/store";
import { jsonResponse, resetStore, serviceInfoFixture } from "../test/helpers";
import type { ServiceInfo } from "../api/backend";

vi.mock("../api/backend", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/backend")>()),
  inTauri: () => true,
  serviceInfo: vi.fn(),
  serviceConnect: vi.fn(),
  serviceUpdate: vi.fn(),
  serviceDetails: vi.fn(),
}));

const backend = await import("../api/backend");
const { BackendModal } = await import("./BackendModal");
const { ConnBanner } = await import("../components/ConnBanner");
const { recheck } = await import("../hooks/useBackend");

const SERVICE = "http://127.0.0.1:8000";
const stopped: ServiceInfo = serviceInfoFixture({
  installed: true,
  ours: true,
  state: "stopped",
  port: 8000,
  url: SERVICE,
  tokenReadable: true,
});

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  resetStore();
  useStore.setState({
    kind: "service",
    baseUrl: "",
    token: null,
    conn: "offline",
    serviceProblem: "notRunning",
  });
  vi.mocked(backend.serviceInfo).mockReset();
  vi.mocked(backend.serviceConnect).mockReset();
  vi.mocked(backend.serviceUpdate).mockReset();
  vi.mocked(backend.serviceInfo).mockResolvedValue(stopped);
  vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: false, reason: "not_running" });
  fetchMock.mockReset();
  fetchMock.mockImplementation(async (input) =>
    String(input).endsWith("/")
      ? jsonResponse({ name: "RootMind API", version: "1.3.0", auth: "bearer" })
      : jsonResponse({ status: "healthy", is_running: false }),
  );
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(async () => {
  cleanup();
  await recheck();
  vi.unstubAllGlobals();
});

describe("BackendModal for the RootMind service", () => {
  it("explains a stopped service and keeps the dialog open while repairing", async () => {
    vi.mocked(backend.serviceUpdate).mockResolvedValue({
      ok: false,
      cancelled: true,
      code: 1223,
      backend: null,
    });
    render(<BackendModal />);
    expect(screen.getByRole("dialog", { name: "The RootMind service is not running" })).toBeTruthy();
    expect(screen.getByText(/Repair starts it again/)).toBeTruthy();

    fireEvent.click(await screen.findByRole("button", { name: "Repair" }));
    await waitFor(() => expect(backend.serviceUpdate).toHaveBeenCalledTimes(1));
    expect(await screen.findByText("Nothing was changed.")).toBeTruthy();
    // The dialog never left the "offline" state (no "switching").
    expect(useStore.getState().conn).toBe("offline");
  });

  it("offers Set up for a service that was never set up", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue(serviceInfoFixture());
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: false, reason: "not_installed" });
    let finish: (r: Awaited<ReturnType<typeof backend.serviceUpdate>>) => void = () => {};
    vi.mocked(backend.serviceUpdate).mockReturnValue(
      new Promise((resolve) => {
        finish = resolve;
      }),
    );
    useStore.setState({ serviceProblem: "notInstalled" });
    render(<BackendModal />);
    expect(screen.getByRole("dialog", { name: "The RootMind service is not set up yet" })).toBeTruthy();
    expect(screen.getByText(/Set up installs it for this account/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Repair" })).toBeNull();

    const setUp = await screen.findByRole("button", { name: "Set up" });
    expect(setUp.className).toMatch(/primary/i);
    fireEvent.click(setUp);
    await waitFor(() => expect(backend.serviceUpdate).toHaveBeenCalledTimes(1));
    expect(
      screen.getByText("Setting up the service … approve the administrator prompt."),
    ).toBeTruthy();
    finish({ ok: false, cancelled: true, code: 1223, backend: null });
    expect(await screen.findByText("Nothing was changed.")).toBeTruthy();
  });

  it("tells an account without administrator rights that setting up needs one", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue(serviceInfoFixture({ canElevate: false }));
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: false, reason: "not_installed" });
    useStore.setState({ serviceProblem: "notInstalled" });
    render(<BackendModal />);
    expect(await screen.findByText(/Repairing needs an administrator account/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Set up" })).toBeNull();
  });

  it("asks to free a busy port (no Repair) and checks again on request", async () => {
    vi.mocked(backend.serviceInfo).mockResolvedValue({ ...stopped, state: "running" });
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: false, reason: "listener_mismatch" });
    useStore.setState({ serviceProblem: "portBusy" });
    render(<BackendModal />);
    expect(
      screen.getByRole("dialog", { name: "The RootMind service cannot use its port" }),
    ).toBeTruthy();
    expect(screen.getByText(/Close that program/)).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Repair" })).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Check again" }));
    await waitFor(() => expect(backend.serviceConnect).toHaveBeenCalled());
  });

  it("connects to a backend at another address, and can go back to the service", async () => {
    render(<BackendModal />);
    fireEvent.click(screen.getByRole("button", { name: "Connect to another backend …" }));
    expect(screen.getByRole("dialog", { name: "No connection to the backend" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Use the RootMind service" }));
    expect(screen.getByRole("dialog", { name: "The RootMind service is not running" })).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Connect to another backend …" }));
    fireEvent.change(screen.getByPlaceholderText("http://127.0.0.1:8000"), {
      target: { value: "http://10.0.0.5:8000" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    await waitFor(() => expect(useStore.getState().kind).toBe("custom"));
    expect(useStore.getState().baseUrl).toBe("http://10.0.0.5:8000");
  });
});

describe("BackendModal while the app sets the service up on its own (Linux)", () => {
  it("shows the set-up in progress, then why it failed, with Set up and Details", async () => {
    useStore.setState({
      serviceProblem: "notInstalled",
      autoService: { action: "setup", status: "running", message: null },
    });
    vi.mocked(backend.serviceInfo).mockResolvedValue(serviceInfoFixture());
    const { rerender } = render(<BackendModal />);
    expect(screen.getByText("Setting up the RootMind service for your account …")).toBeTruthy();
    expect((screen.getByRole("button", { name: "Check again" }) as HTMLButtonElement).disabled).toBe(true);

    vi.mocked(backend.serviceDetails).mockResolvedValue("service status --json (exit code 0): {}");
    useStore.setState({
      autoService: { action: "setup", status: "failed", message: "The port is in use." },
    });
    rerender(<BackendModal />);
    expect(screen.getByText("The port is in use.")).toBeTruthy();
    expect(await screen.findByRole("button", { name: "Set up" })).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Details" }));
    expect(await screen.findByText(/service status --json/)).toBeTruthy();
  });
});

describe("BackendModal for a custom backend", () => {
  it("offers the RootMind service instead of an unreachable address", async () => {
    vi.mocked(backend.serviceConnect).mockResolvedValue({ ok: true, url: SERVICE, token: "v" });
    useStore.setState({ kind: "custom", baseUrl: "http://a", conn: "offline", serviceProblem: null });
    render(<BackendModal />);
    expect(screen.getByRole("dialog", { name: "No connection to the backend" })).toBeTruthy();
    expect(screen.queryByText(/built-in/i)).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Use the RootMind service" }));
    await waitFor(() => expect(useStore.getState().conn).toBe("online"));
    expect(useStore.getState().kind).toBe("service");
    expect(useStore.getState().baseUrl).toBe(SERVICE);
  });
});

describe("ConnBanner", () => {
  it("names the service's problem", () => {
    useStore.setState({ serviceProblem: "notInstalled" });
    render(<ConnBanner />);
    expect(screen.getByText("The RootMind service is not set up yet")).toBeTruthy();
  });

  it("says a custom backend is unreachable", () => {
    useStore.setState({ kind: "custom", baseUrl: "http://a", serviceProblem: null });
    render(<ConnBanner />);
    expect(screen.getByText("The RootMind backend is not reachable.")).toBeTruthy();
  });
});
