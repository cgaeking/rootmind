import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfigDto } from "../types/config";
import { ApiError } from "../api/client";
import { useStore } from "../state/store";
import { resetStore } from "../test/helpers";

vi.mock("../api/rlm", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rlm")>()),
  getConfig: vi.fn(),
  getConnectors: vi.fn(),
  saveConfig: vi.fn(),
}));

// The native folder picker of the app.
vi.mock("@tauri-apps/plugin-dialog", () => ({ open: vi.fn() }));

const rlm = await import("../api/rlm");
const dialog = await import("@tauri-apps/plugin-dialog");
const { FoldersScreen } = await import("./FoldersScreen");

const URL_A = "http://127.0.0.1:18001";
const WINDOWS_UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Edg/141.0.0.0";

function config(paths: string[]): AppConfigDto {
  return {
    llm: { provider: "anthropic", model: "m", api_keys: {} },
    connectors: paths.map((p, i) => ({ name: `folder_${i}`, type: "local", path: p })),
    indexer: { sync_schedule: null, sync_interval_hours: 24, include: [], exclude: [] },
  };
}

function savedPaths(call = 0): (string | null)[] {
  const body = vi.mocked(rlm.saveConfig).mock.calls[call][0];
  return (body.connectors ?? []).map((c) => c.path);
}

/** The folder rows' text fields, as shown. */
function shownPaths(): string[] {
  return screen
    .queryAllByPlaceholderText(/Documents/)
    .map((el) => (el as HTMLInputElement).value);
}

/** Pretend to run inside the app, so "Add folder" opens the native picker. */
function inApp() {
  Object.defineProperty(window, "__TAURI_INTERNALS__", { value: {}, configurable: true });
}

beforeEach(() => {
  resetStore();
  vi.mocked(rlm.getConfig).mockReset();
  vi.mocked(rlm.saveConfig).mockReset();
  vi.mocked(rlm.getConnectors).mockReset();
  vi.mocked(dialog.open).mockReset();
  vi.mocked(rlm.saveConfig).mockResolvedValue({ ok: true, reloaded: true, restart_required: false });
  vi.mocked(rlm.getConnectors).mockResolvedValue({ connectors: [] });
  vi.spyOn(navigator, "userAgent", "get").mockReturnValue(WINDOWS_UA);
  useStore.setState({ kind: "builtin", baseUrl: URL_A, token: null, conn: "online" });
});

afterEach(() => {
  cleanup();
  delete (window as unknown as Record<string, unknown>).__TAURI_INTERNALS__;
});

describe("FoldersScreen", () => {
  it("has no Save button and shows the folders with backslashes", async () => {
    vi.mocked(rlm.getConfig).mockResolvedValue(config(["C:/data/a"]));
    render(<FoldersScreen />);
    expect(await screen.findByDisplayValue("C:\\data\\a")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();
  });

  it("saves a folder picked in the dialog right away", async () => {
    inApp();
    vi.mocked(rlm.getConfig).mockResolvedValue(config(["C:/data/a"]));
    vi.mocked(dialog.open).mockResolvedValue("C:\\data\\new");
    render(<FoldersScreen />);
    await screen.findByDisplayValue("C:\\data\\a");

    fireEvent.click(screen.getByRole("button", { name: "+ Add folder" }));
    await waitFor(() => expect(rlm.saveConfig).toHaveBeenCalledTimes(1));
    // Stored as before: forward slashes, existing names kept, a name for the new one.
    expect(savedPaths()).toEqual(["C:/data/a", "C:/data/new"]);
    const body = vi.mocked(rlm.saveConfig).mock.calls[0][0];
    expect(body.connectors?.map((c) => c.name)).toEqual(["folder_0", "c_data_new"]);
    expect(await screen.findByDisplayValue("C:\\data\\new")).toBeTruthy();
    expect(await screen.findByText("Applied.")).toBeTruthy();
  });

  it("does not keep a refused folder and shows the reason", async () => {
    inApp();
    vi.mocked(rlm.getConfig).mockResolvedValue(config(["C:/data/a"]));
    vi.mocked(dialog.open).mockResolvedValue("D:\\share");
    vi.mocked(rlm.saveConfig).mockRejectedValue(
      new ApiError(400, "Cannot use D:/share: network drives are not available"),
    );
    render(<FoldersScreen />);
    await screen.findByDisplayValue("C:\\data\\a");

    fireEvent.click(screen.getByRole("button", { name: "+ Add folder" }));
    expect(
      await screen.findByText(
        "The folder was not added: Cannot use D:\\share: network drives are not available",
      ),
    ).toBeTruthy();
    expect(shownPaths()).toEqual(["C:\\data\\a"]);
  });

  it("saves a removal right away", async () => {
    vi.mocked(rlm.getConfig).mockResolvedValue(config(["C:/data/a", "C:/data/b"]));
    render(<FoldersScreen />);
    await screen.findByDisplayValue("C:\\data\\b");

    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    await waitFor(() => expect(rlm.saveConfig).toHaveBeenCalledTimes(1));
    expect(savedPaths()).toEqual(["C:/data/b"]);
    await waitFor(() => expect(shownPaths()).toEqual(["C:\\data\\b"]));
  });

  it("puts a folder back when its removal is refused", async () => {
    vi.mocked(rlm.getConfig).mockResolvedValue(config(["C:/data/a", "C:/data/b"]));
    vi.mocked(rlm.saveConfig).mockRejectedValue(new ApiError(400, "Invalid config: disk full"));
    render(<FoldersScreen />);
    await screen.findByDisplayValue("C:\\data\\b");

    fireEvent.click(screen.getAllByRole("button", { name: "Remove" })[0]);
    expect(
      await screen.findByText("The folder was not removed: Invalid config: disk full"),
    ).toBeTruthy();
    expect(shownPaths()).toEqual(["C:\\data\\a", "C:\\data\\b"]);
  });

  it("accepts a typed path with either separator outside the app", async () => {
    vi.mocked(rlm.getConfig).mockResolvedValue(config([]));
    render(<FoldersScreen />);
    fireEvent.click(await screen.findByRole("button", { name: "+ Add folder" }));

    const input = screen.getByPlaceholderText(/Documents/);
    fireEvent.change(input, { target: { value: "C:\\typed\\folder" } });
    fireEvent.blur(input);
    await waitFor(() => expect(rlm.saveConfig).toHaveBeenCalledTimes(1));
    expect(savedPaths()).toEqual(["C:/typed/folder"]);
  });

  it("does not save an unchanged path", async () => {
    vi.mocked(rlm.getConfig).mockResolvedValue(config(["C:/data/a"]));
    render(<FoldersScreen />);
    const input = await screen.findByDisplayValue("C:\\data\\a");
    fireEvent.change(input, { target: { value: "C:/data/a" } });
    fireEvent.blur(input);
    await act(async () => {});
    expect(rlm.saveConfig).not.toHaveBeenCalled();
  });
});
