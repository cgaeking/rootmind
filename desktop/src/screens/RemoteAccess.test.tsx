import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { RemoteAccessInfo } from "../types/api";
import { resetStore } from "../test/helpers";

vi.mock("../api/rlm", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rlm")>()),
  getRemoteAccess: vi.fn(),
  updateRemoteAccess: vi.fn(),
}));

const rlm = await import("../api/rlm");
const { RemoteAccess } = await import("./RemoteAccess");

// Obviously fake ngrok authtoken.
const AUTHTOKEN = "fake_ngrok_authtoken_0000000000000001"; // pragma: allowlist secret
const PUBLIC = "https://rootmind-test.ngrok-free.app";
const TOGGLE = "Reach MCP from the internet through ngrok";

function remoteInfo(over: Partial<RemoteAccessInfo> = {}): RemoteAccessInfo {
  return {
    enabled: false,
    domain: null,
    authtoken_saved: false,
    sdk: { available: true, version: "1.7.0" },
    status: { state: "off", url: null, error: null, updated: null },
    ...over,
  };
}

const writeText = vi.fn(async (text: string) => {
  void text;
});

beforeEach(() => {
  resetStore();
  vi.mocked(rlm.getRemoteAccess).mockReset();
  vi.mocked(rlm.updateRemoteAccess).mockReset();
  writeText.mockClear();
  Object.defineProperty(navigator, "clipboard", { value: { writeText }, configurable: true });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function toggle(): HTMLInputElement {
  return screen.getByRole("checkbox", { name: TOGGLE }) as HTMLInputElement;
}

describe("RemoteAccess", () => {
  it("is disabled while MCP over HTTP is off", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo({ authtoken_saved: true }));
    render(<RemoteAccess mcpEnabled={false} />);
    expect(await screen.findByText("Turn on Serve MCP over HTTP first.")).toBeTruthy();
    expect(toggle().disabled).toBe(true);
    expect((screen.getByLabelText("Reserved domain (optional)") as HTMLInputElement).disabled).toBe(true);
  });

  it("needs a saved authtoken before it can be turned on", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo());
    render(<RemoteAccess mcpEnabled />);
    expect(await screen.findByText("Save your ngrok authtoken first.")).toBeTruthy();
    expect(toggle().disabled).toBe(true);
  });

  it("saves the authtoken write-only: the field is cleared and only 'Saved' is shown", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo());
    vi.mocked(rlm.updateRemoteAccess).mockResolvedValue(remoteInfo({ authtoken_saved: true }));
    const { container } = render(<RemoteAccess mcpEnabled />);
    const field = (await screen.findByLabelText("ngrok authtoken")) as HTMLInputElement;
    expect(field.type).toBe("password");
    fireEvent.change(field, { target: { value: `  ${AUTHTOKEN} ` } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(rlm.updateRemoteAccess).toHaveBeenCalledWith({ authtoken: AUTHTOKEN }));
    expect(await screen.findByText("Saved.")).toBeTruthy();
    expect(screen.getByText("Saved")).toBeTruthy();
    expect(screen.queryByLabelText("ngrok authtoken")).toBeNull();
    expect(container.innerHTML).not.toContain(AUTHTOKEN);
    expect(screen.queryByDisplayValue(AUTHTOKEN)).toBeNull();
    expect(toggle().disabled).toBe(false);
  });

  it("asks for confirmation before turning it on", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo({ authtoken_saved: true }));
    vi.mocked(rlm.updateRemoteAccess).mockResolvedValue(
      remoteInfo({ enabled: true, authtoken_saved: true, status: { state: "connecting", url: null, error: null, updated: null } }),
    );
    render(<RemoteAccess mcpEnabled />);
    fireEvent.click(await screen.findByRole("checkbox", { name: TOGGLE }));
    const dialog = await screen.findByRole("dialog", { name: "Make RootMind reachable from the internet?" });
    expect(dialog.textContent).toContain("Anyone who has the MCP token can read every indexed document");
    expect(dialog.textContent).toContain("goes to that assistant's provider");
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).toBeNull();
    expect(rlm.updateRemoteAccess).not.toHaveBeenCalled();
    expect(toggle().checked).toBe(false);

    fireEvent.click(toggle());
    fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    await waitFor(() => expect(rlm.updateRemoteAccess).toHaveBeenCalledWith({ enabled: true }));
    expect(await screen.findByText("Connecting …")).toBeTruthy();
    expect(toggle().checked).toBe(true);
  });

  it("turns it off without asking", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(
      remoteInfo({ enabled: true, authtoken_saved: true, status: { state: "connected", url: PUBLIC, error: null, updated: null } }),
    );
    vi.mocked(rlm.updateRemoteAccess).mockResolvedValue(remoteInfo({ authtoken_saved: true }));
    render(<RemoteAccess mcpEnabled />);
    await screen.findByText("Connected");
    fireEvent.click(toggle());
    await waitFor(() => expect(rlm.updateRemoteAccess).toHaveBeenCalledWith({ enabled: false }));
    expect(screen.queryByRole("dialog")).toBeNull();
    await waitFor(() => expect(screen.queryByText("Connected")).toBeNull());
  });

  it("shows the public MCP endpoint while connected and copies it", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(
      remoteInfo({ enabled: true, authtoken_saved: true, status: { state: "connected", url: PUBLIC, error: null, updated: null } }),
    );
    render(<RemoteAccess mcpEnabled />);
    expect(await screen.findByDisplayValue(`${PUBLIC}/mcp`)).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Copy" }));
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(`${PUBLIC}/mcp`));
    expect(await screen.findByRole("button", { name: "Copied" })).toBeTruthy();
  });

  it("shows the tunnel's error", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(
      remoteInfo({
        enabled: true,
        authtoken_saved: true,
        status: { state: "error", url: null, error: "ERR_NGROK_105: authentication failed", updated: null },
      }),
    );
    render(<RemoteAccess mcpEnabled />);
    expect(await screen.findByText("ERR_NGROK_105: authentication failed")).toBeTruthy();
    expect(screen.getByText("Error")).toBeTruthy();
  });

  it("polls the status while remote access is on", async () => {
    vi.useFakeTimers();
    vi.mocked(rlm.getRemoteAccess)
      .mockResolvedValueOnce(
        remoteInfo({ enabled: true, authtoken_saved: true, status: { state: "connecting", url: null, error: null, updated: null } }),
      )
      .mockResolvedValue(
        remoteInfo({ enabled: true, authtoken_saved: true, status: { state: "connected", url: PUBLIC, error: null, updated: null } }),
      );
    render(<RemoteAccess mcpEnabled />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(screen.getByText("Connecting …")).toBeTruthy();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_000);
    });
    expect(screen.getByDisplayValue(`${PUBLIC}/mcp`)).toBeTruthy();
  });

  it("does not poll while remote access is off", async () => {
    vi.useFakeTimers();
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo({ authtoken_saved: true }));
    render(<RemoteAccess mcpEnabled />);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(10_000);
    });
    expect(rlm.getRemoteAccess).toHaveBeenCalledTimes(1);
  });

  it("saves and removes the domain, removes the authtoken and replaces it", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(
      remoteInfo({ authtoken_saved: true, domain: "old.ngrok-free.app" }),
    );
    vi.mocked(rlm.updateRemoteAccess).mockResolvedValue(remoteInfo({ authtoken_saved: true }));
    render(<RemoteAccess mcpEnabled />);
    const domain = (await screen.findByLabelText("Reserved domain (optional)")) as HTMLInputElement;
    expect(domain.value).toBe("old.ngrok-free.app");
    expect((screen.getByRole("button", { name: "Save" }) as HTMLButtonElement).disabled).toBe(true);
    fireEvent.change(domain, { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(rlm.updateRemoteAccess).toHaveBeenCalledWith({ domain: "" }));

    fireEvent.click(await screen.findByRole("button", { name: "Remove" }));
    await waitFor(() => expect(rlm.updateRemoteAccess).toHaveBeenLastCalledWith({ authtoken: "" }));

    fireEvent.click(screen.getByRole("button", { name: "Replace" }));
    expect(screen.getByLabelText("ngrok authtoken")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByLabelText("ngrok authtoken")).toBeNull();
  });

  it("reports a refused change and keeps what was typed", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo());
    vi.mocked(rlm.updateRemoteAccess).mockRejectedValue(
      new ApiError(400, "This does not look like an ngrok authtoken"),
    );
    render(<RemoteAccess mcpEnabled />);
    const field = (await screen.findByLabelText("ngrok authtoken")) as HTMLInputElement;
    fireEvent.change(field, { target: { value: "bad token" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    expect(
      await screen.findByText("The change failed: This does not look like an ngrok authtoken"),
    ).toBeTruthy();
    expect((screen.getByLabelText("ngrok authtoken") as HTMLInputElement).value).toBe("bad token");
  });

  it("says when the service cannot open tunnels", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(
      remoteInfo({ authtoken_saved: true, sdk: { available: false, version: null } }),
    );
    render(<RemoteAccess mcpEnabled />);
    expect(await screen.findByText(/the ngrok SDK is missing/)).toBeTruthy();
    expect(toggle().disabled).toBe(true);
  });

  it("reports when the settings cannot be read", async () => {
    vi.mocked(rlm.getRemoteAccess).mockRejectedValue(new ApiError(0, "Timed out after 15s"));
    render(<RemoteAccess mcpEnabled />);
    expect(
      await screen.findByText("Could not read the remote access settings: Timed out after 15s"),
    ).toBeTruthy();
    expect(screen.queryByRole("checkbox")).toBeNull();
  });
});
