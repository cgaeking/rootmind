import {
  act,
  cleanup,
  fireEvent,
  render,
  screen,
  waitFor,
  within,
} from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { ApiError } from "../api/client";
import type { RemoteAccessInfo } from "../types/api";
import { resetStore } from "../test/helpers";

vi.mock("../api/rlm", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rlm")>()),
  getRemoteAccess: vi.fn(),
  updateRemoteAccess: vi.fn(),
}));

vi.mock("../utils/openExternal", () => ({ openExternal: vi.fn() }));

const rlm = await import("../api/rlm");
const { openExternal } = await import("../utils/openExternal");
const { McpReach } = await import("./McpReach");

// Obviously fake ngrok authtoken and MCP token.
const AUTHTOKEN = "fake_ngrok_authtoken_0000000000000001"; // pragma: allowlist secret
const TOKEN = "mcp-token-fake"; // pragma: allowlist secret
const LOCAL = "http://127.0.0.1:3000/mcp";
const PUBLIC = "https://rootmind-test.ngrok-free.app";

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

const connected = (over: Partial<RemoteAccessInfo> = {}) =>
  remoteInfo({
    enabled: true,
    authtoken_saved: true,
    domain: "rootmind-test.ngrok-free.app",
    status: { state: "connected", url: PUBLIC, error: null, updated: null },
    ...over,
  });

const writeText = vi.fn(async (text: string) => {
  void text;
});
const readToken = vi.fn(async () => TOKEN);
const onUseService = vi.fn();

beforeEach(() => {
  resetStore();
  vi.mocked(rlm.getRemoteAccess).mockReset();
  vi.mocked(rlm.updateRemoteAccess).mockReset();
  vi.mocked(openExternal).mockClear();
  readToken.mockClear();
  onUseService.mockClear();
  writeText.mockClear();
  Object.defineProperty(navigator, "clipboard", {
    value: { writeText },
    configurable: true,
  });
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
});

function renderReach(serviceInUse = true) {
  return render(
    <McpReach
      endpoint={LOCAL}
      readToken={readToken}
      serviceInUse={serviceInUse}
      onUseService={onUseService}
      disabled={false}
    />,
  );
}

const remoteBox = () =>
  screen.getByRole("checkbox", { name: /^Remote/ }) as HTMLInputElement;

describe("McpReach", () => {
  it("always shows Local; the Remote setup shows only while Remote is ticked", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo());
    renderReach();
    await waitFor(() => expect(rlm.getRemoteAccess).toHaveBeenCalled());
    expect(screen.queryByRole("radio")).toBeNull();
    expect(screen.getByDisplayValue(LOCAL)).toBeTruthy();
    expect(remoteBox().checked).toBe(false);
    expect(screen.queryByLabelText("ngrok Authtoken")).toBeNull();

    fireEvent.click(remoteBox());
    expect(remoteBox().checked).toBe(true);
    expect(screen.getByLabelText("ngrok Authtoken")).toBeTruthy();
    // The local endpoint and its MCP token stay at hand.
    expect(screen.getByDisplayValue(LOCAL)).toBeTruthy();
    expect(screen.getByRole("button", { name: "Copy MCP token" })).toBeTruthy();
  });

  it("shows Remote when remote access is already on", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(connected());
    renderReach();
    expect(await screen.findByDisplayValue(`${PUBLIC}/mcp`)).toBeTruthy();
    expect(remoteBox().checked).toBe(true);
    expect(screen.getByText("Connected")).toBeTruthy();
    expect(screen.getByDisplayValue(LOCAL)).toBeTruthy();
  });

  it("connects with the typed authtoken and domain after a confirmation", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo());
    vi.mocked(rlm.updateRemoteAccess).mockResolvedValue(connected());
    renderReach();
    await waitFor(() => expect(rlm.getRemoteAccess).toHaveBeenCalled());
    fireEvent.click(remoteBox());

    const connect = screen.getByRole("button", {
      name: "Connect",
    }) as HTMLButtonElement;
    expect(connect.disabled).toBe(true); // no authtoken yet
    const field = screen.getByLabelText("ngrok Authtoken") as HTMLInputElement;
    expect(field.type).toBe("password");
    fireEvent.change(field, { target: { value: `  ${AUTHTOKEN} ` } });
    fireEvent.change(screen.getByLabelText("Domain"), {
      target: { value: "rootmind-test.ngrok-free.app" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));

    const dialog = await screen.findByRole("dialog", {
      name: "Make RootMind reachable from the internet?",
    });
    expect(rlm.updateRemoteAccess).not.toHaveBeenCalled();
    fireEvent.click(within(dialog).getByRole("button", { name: "Turn on" }));
    await waitFor(() =>
      expect(rlm.updateRemoteAccess).toHaveBeenCalledWith({
        enabled: true,
        authtoken: AUTHTOKEN,
        domain: "rootmind-test.ngrok-free.app",
      }),
    );
    expect(await screen.findByDisplayValue(`${PUBLIC}/mcp`)).toBeTruthy();
    expect(screen.queryByLabelText("ngrok Authtoken")).toBeNull();
  });

  it("connects with a saved authtoken without asking for it again", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(
      remoteInfo({ authtoken_saved: true }),
    );
    vi.mocked(rlm.updateRemoteAccess).mockResolvedValue(connected());
    renderReach();
    await waitFor(() => expect(rlm.getRemoteAccess).toHaveBeenCalled());
    fireEvent.click(remoteBox());
    expect(screen.getByText("Saved")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    await waitFor(() =>
      expect(rlm.updateRemoteAccess).toHaveBeenCalledWith({ enabled: true }),
    );
  });

  it("unticking Remote closes the public address", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(connected());
    vi.mocked(rlm.updateRemoteAccess).mockResolvedValue(
      remoteInfo({ authtoken_saved: true }),
    );
    renderReach();
    await screen.findByDisplayValue(`${PUBLIC}/mcp`);
    fireEvent.click(remoteBox());
    await waitFor(() =>
      expect(rlm.updateRemoteAccess).toHaveBeenCalledWith({ enabled: false }),
    );
    await waitFor(() => expect(remoteBox().checked).toBe(false));
    expect(screen.queryByDisplayValue(`${PUBLIC}/mcp`)).toBeNull();
    expect(screen.getByDisplayValue(LOCAL)).toBeTruthy();
  });

  it("copies the public endpoint and the token", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(connected());
    renderReach();
    await screen.findByDisplayValue(`${PUBLIC}/mcp`);
    // Local first, then the public endpoint: each with Copy and Copy MCP token.
    const copies = screen.getAllByRole("button", { name: "Copy" });
    expect(copies).toHaveLength(2);
    fireEvent.click(copies[1]);
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(`${PUBLIC}/mcp`),
    );
    fireEvent.click(
      screen.getAllByRole("button", { name: "Copy MCP token" })[1],
    );
    await waitFor(() => expect(writeText).toHaveBeenCalledWith(TOKEN));
  });

  it("shows the tunnel's error with the setup to fix it", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(
      connected({
        status: {
          state: "error",
          url: null,
          error: "authentication failed",
          updated: null,
        },
      }),
    );
    vi.mocked(rlm.updateRemoteAccess).mockResolvedValue(connected());
    renderReach();
    expect(await screen.findByText("authentication failed")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Replace" }));
    fireEvent.change(screen.getByLabelText("ngrok Authtoken"), {
      target: { value: AUTHTOKEN },
    });
    fireEvent.click(screen.getByRole("button", { name: "Save and try again" }));
    await waitFor(() =>
      expect(rlm.updateRemoteAccess).toHaveBeenCalledWith({
        enabled: true,
        authtoken: AUTHTOKEN,
      }),
    );
  });

  it("reports a refused change and keeps what was typed", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo());
    vi.mocked(rlm.updateRemoteAccess).mockRejectedValue(
      new ApiError(400, "Not a valid ngrok authtoken"),
    );
    renderReach();
    await waitFor(() => expect(rlm.getRemoteAccess).toHaveBeenCalled());
    fireEvent.click(remoteBox());
    fireEvent.change(screen.getByLabelText("ngrok Authtoken"), {
      target: { value: "refused_by_the_backend_000000001" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    fireEvent.click(await screen.findByRole("button", { name: "Turn on" }));
    expect(
      await screen.findByText("The change failed: Not a valid ngrok authtoken"),
    ).toBeTruthy();
    expect(
      (screen.getByLabelText("ngrok Authtoken") as HTMLInputElement).value,
    ).toBe("refused_by_the_backend_000000001");
  });

  it("refuses the MCP token as the ngrok Authtoken, and anything that is not one", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo());
    renderReach();
    await waitFor(() => expect(rlm.getRemoteAccess).toHaveBeenCalled());
    fireEvent.click(remoteBox());
    const field = screen.getByLabelText("ngrok Authtoken");

    fireEvent.change(field, { target: { value: ` ${TOKEN} ` } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(
      await screen.findByText(
        "This is the MCP token, not the ngrok Authtoken.",
      ),
    ).toBeTruthy();

    fireEvent.change(field, { target: { value: "too short" } });
    fireEvent.click(screen.getByRole("button", { name: "Connect" }));
    expect(await screen.findByText("Not an ngrok Authtoken.")).toBeTruthy();

    expect(screen.queryByRole("dialog")).toBeNull();
    expect(rlm.updateRemoteAccess).not.toHaveBeenCalled();
  });

  it("shows a known ngrok error as one sentence with its code", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(
      connected({
        status: {
          state: "error",
          url: null,
          error:
            "The endpoint 'https://x.ngrok-free.dev' is already online. (ERR_NGROK_334)",
          updated: null,
        },
      }),
    );
    renderReach();
    expect(
      await screen.findByText(
        "Another tunnel is already using this domain. (ERR_NGROK_334)",
      ),
    ).toBeTruthy();
  });

  it("polls the status while remote access is on", async () => {
    vi.useFakeTimers();
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(connected());
    renderReach();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(0);
    });
    const calls = vi.mocked(rlm.getRemoteAccess).mock.calls.length;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3_100);
    });
    expect(vi.mocked(rlm.getRemoteAccess).mock.calls.length).toBeGreaterThan(
      calls,
    );
  });

  it("says when the service cannot open tunnels", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(
      remoteInfo({ sdk: { available: false, version: null } }),
    );
    renderReach();
    await waitFor(() => expect(rlm.getRemoteAccess).toHaveBeenCalled());
    fireEvent.click(remoteBox());
    expect(screen.getByText(/the ngrok SDK is missing/)).toBeTruthy();
  });

  it("offers the service instead of Remote while the app uses another backend", async () => {
    renderReach(false);
    fireEvent.click(remoteBox());
    fireEvent.click(
      screen.getByRole("button", { name: "Use the RootMind service" }),
    );
    expect(onUseService).toHaveBeenCalled();
    expect(rlm.getRemoteAccess).not.toHaveBeenCalled();
  });
});

describe("McpReach help dialogs", () => {
  it("shows Claude Desktop, Claude Code and n8n, and copies with the real token", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo());
    renderReach();
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Reach MCP from Claude, n8n and other clients",
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Reach MCP from Claude, n8n and other clients",
    });

    fireEvent.click(
      within(dialog).getByRole("button", { name: "Download rootmind.mcpb" }),
    );
    expect(openExternal).toHaveBeenCalledWith(
      "https://github.com/cgaeking/rootmind/releases/latest/download/rootmind.mcpb",
    );

    fireEvent.click(within(dialog).getByRole("tab", { name: "Claude Code" }));
    expect(
      within(dialog).getByText(/claude mcp add --transport http rootmind/)
        .textContent,
    ).toContain("Bearer <token>");
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Copy command" }),
    );
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(
        `claude mcp add --transport http rootmind ${LOCAL} --header "Authorization: Bearer ${TOKEN}"`,
      ),
    );

    fireEvent.click(within(dialog).getByRole("tab", { name: "n8n" }));
    expect(
      within(dialog).getByText(/host\.docker\.internal:3000\/mcp/),
    ).toBeTruthy();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Copy values" }),
    );
    await waitFor(() =>
      expect(writeText).toHaveBeenLastCalledWith(
        expect.stringContaining(`Bearer ${TOKEN}`),
      ),
    );

    fireEvent.click(within(dialog).getByRole("button", { name: "Close" }));
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("explains where the ngrok authtoken and domain come from", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(remoteInfo());
    renderReach();
    await waitFor(() => expect(rlm.getRemoteAccess).toHaveBeenCalled());
    fireEvent.click(remoteBox());
    fireEvent.click(
      screen.getByRole("button", {
        name: "Get your free ngrok Authtoken and domain",
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Get your ngrok Authtoken and domain",
    });
    expect(within(dialog).getByText(/free static domain/)).toBeTruthy();
    fireEvent.click(
      within(dialog).getByRole("button", { name: "Open the ngrok dashboard" }),
    );
    expect(openExternal).toHaveBeenCalledWith("https://dashboard.ngrok.com/");
    fireEvent.keyDown(window, { key: "Escape" });
    expect(screen.queryByRole("dialog")).toBeNull();
  });

  it("gives the Claude connector values, the header with the real token on copy", async () => {
    vi.mocked(rlm.getRemoteAccess).mockResolvedValue(connected());
    renderReach();
    fireEvent.click(
      await screen.findByRole("button", {
        name: "Use RootMind in Claude on the web and on your phone",
      }),
    );
    const dialog = screen.getByRole("dialog", {
      name: "Use RootMind in Claude on the web and on your phone",
    });
    expect(within(dialog).getByText(`${PUBLIC}/mcp`)).toBeTruthy();
    expect(within(dialog).getByText("No sign in")).toBeTruthy();
    expect(within(dialog).getByText("Authorization")).toBeTruthy();
    expect(within(dialog).queryByText(new RegExp(TOKEN))).toBeNull();

    const copies = within(dialog).getAllByRole("button", { name: "Copy" });
    fireEvent.click(copies[0]);
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(`${PUBLIC}/mcp`),
    );
    fireEvent.click(copies[1]);
    await waitFor(() =>
      expect(writeText).toHaveBeenCalledWith(`Bearer ${TOKEN}`),
    );
  });
});
