import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfigDto } from "../types/config";
import { useStore } from "../state/store";
import { resetStore } from "../test/helpers";

vi.mock("../api/rlm", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rlm")>()),
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  listLlmModels: vi.fn(),
}));

const rlm = await import("../api/rlm");
const { SettingsScreen } = await import("./SettingsScreen");

const A = "http://127.0.0.1:18001";
const B = "http://127.0.0.1:18002";

/** A config that can be told apart by its indexer settings. */
function configFor(tag: string, hours: number): AppConfigDto {
  return {
    llm: { provider: "anthropic", model: `model-${tag}`, api_keys: {} },
    connectors: [{ name: `folder_${tag}`, type: "local", path: `C:/data/${tag}` }],
    indexer: { sync_schedule: null, sync_interval_hours: hours, include: [`*.${tag}`], exclude: [] },
  };
}

const CONFIGS: Record<string, AppConfigDto> = { [A]: configFor("a", 11), [B]: configFor("b", 22) };

/** Answer GET /config for whichever backend is in use when the request starts. */
function serveConfigs() {
  vi.mocked(rlm.getConfig).mockImplementation(async () =>
    structuredClone(CONFIGS[useStore.getState().baseUrl]),
  );
}

/** Switch to another backend the way the app does: checking first, then online. */
async function switchTo(url: string) {
  await act(async () => {
    useStore.getState().setBackend({ kind: "custom", url, token: null });
  });
  expect(useStore.getState().conn).toBe("checking");
  await act(async () => {
    useStore.getState().setConn("online");
  });
}

function savedConfigs(): AppConfigDto[] {
  return vi.mocked(rlm.saveConfig).mock.calls.map((c) => c[0]);
}

beforeEach(() => {
  resetStore();
  vi.mocked(rlm.getConfig).mockReset();
  vi.mocked(rlm.saveConfig).mockReset();
  vi.mocked(rlm.listLlmModels).mockReset();
  vi.mocked(rlm.saveConfig).mockResolvedValue({ ok: true, reloaded: true, restart_required: false });
  useStore.setState({ kind: "custom", baseUrl: A, token: null, conn: "online" });
});

afterEach(() => {
  cleanup();
});

describe("SettingsScreen", () => {
  it("shows the new backend's config after a switch and saves only that one", async () => {
    serveConfigs();
    render(<SettingsScreen />);
    expect(await screen.findByDisplayValue("*.a")).toBeTruthy();
    expect(screen.getByDisplayValue("11")).toBeTruthy();

    await switchTo(B);

    expect(await screen.findByDisplayValue("*.b")).toBeTruthy();
    expect(screen.getByDisplayValue("22")).toBeTruthy();
    expect(screen.queryByDisplayValue("*.a")).toBeNull();
    expect(screen.queryByDisplayValue("11")).toBeNull();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(rlm.saveConfig).toHaveBeenCalledTimes(1));
    expect(savedConfigs()[0].indexer.include).toEqual(["*.b"]);
    expect(savedConfigs()[0].llm.model).toBe("model-b");
  });

  it("reloads even when both backends stay online (the original bug)", async () => {
    serveConfigs();
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");

    // The URL changes while `conn` stays "online" the whole time.
    await act(async () => {
      useStore.setState({ baseUrl: B });
    });
    expect(useStore.getState().conn).toBe("online");

    expect(await screen.findByDisplayValue("*.b")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(rlm.saveConfig).toHaveBeenCalledTimes(1));
    expect(savedConfigs()[0].indexer.include).toEqual(["*.b"]);
  });

  it("clears the old config while the new backend is loading", async () => {
    serveConfigs();
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");

    // B's config does not arrive yet.
    let releaseB: (c: AppConfigDto) => void = () => {};
    vi.mocked(rlm.getConfig).mockImplementation(
      () => new Promise<AppConfigDto>((resolve) => (releaseB = resolve)),
    );
    await switchTo(B);

    // Nothing of A is left, and there is nothing to save.
    expect(screen.queryByDisplayValue("*.a")).toBeNull();
    expect(screen.queryByRole("button", { name: "Save" })).toBeNull();

    await act(async () => releaseB(structuredClone(CONFIGS[B])));
    expect(await screen.findByDisplayValue("*.b")).toBeTruthy();
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(rlm.saveConfig).toHaveBeenCalledTimes(1));
    expect(savedConfigs()[0].indexer.include).toEqual(["*.b"]);
  });

  it("drops A's config when it arrives after the switch to B", async () => {
    let releaseA: (c: AppConfigDto) => void = () => {};
    vi.mocked(rlm.getConfig).mockImplementationOnce(
      () => new Promise<AppConfigDto>((resolve) => (releaseA = resolve)),
    );
    render(<SettingsScreen />);

    serveConfigs();
    await switchTo(B);
    expect(await screen.findByDisplayValue("*.b")).toBeTruthy();

    // A's late answer must not replace B's config.
    await act(async () => releaseA(structuredClone(CONFIGS[A])));
    expect(screen.queryByDisplayValue("*.a")).toBeNull();
    expect(screen.getByDisplayValue("*.b")).toBeTruthy();

    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(rlm.saveConfig).toHaveBeenCalledTimes(1));
    expect(savedConfigs().every((c) => c.indexer.include[0] === "*.b")).toBe(true);
  });

  it("shows the Advanced URL of the custom backend in use", async () => {
    serveConfigs();
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");
    expect(screen.getByDisplayValue(A)).toBeTruthy();

    await switchTo(B);
    // The field follows the store (the backend dialog can change it too).
    expect(await screen.findByDisplayValue(B)).toBeTruthy();
    expect(screen.queryByDisplayValue(A)).toBeNull();
  });
});
