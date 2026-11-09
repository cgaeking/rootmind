import { act, cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import type { AppConfigDto } from "../types/config";
import { ApiError } from "../api/client";
import { useLanguageStore } from "../i18n/labels";
import { useStore } from "../state/store";
import { resetStore } from "../test/helpers";

vi.mock("../api/rlm", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rlm")>()),
  getConfig: vi.fn(),
  saveConfig: vi.fn(),
  listLlmModels: vi.fn(),
  checkLlmModel: vi.fn(),
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
  return vi.mocked(rlm.saveConfig).mock.calls.map((c) => c[0] as AppConfigDto);
}

beforeEach(() => {
  resetStore();
  vi.mocked(rlm.getConfig).mockReset();
  vi.mocked(rlm.saveConfig).mockReset();
  vi.mocked(rlm.listLlmModels).mockReset();
  vi.mocked(rlm.checkLlmModel).mockReset();
  vi.mocked(rlm.checkLlmModel).mockImplementation(async ({ provider, model }) => ({
    provider,
    model,
    ok: true,
    reason: null,
  }));
  vi.mocked(rlm.saveConfig).mockResolvedValue({ ok: true, reloaded: true, restart_required: false });
  useStore.setState({ kind: "custom", baseUrl: A, token: null, conn: "online" });
});

afterEach(() => {
  cleanup();
  useLanguageStore.getState().setPref("en");
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

  it("shows the address of the server in use under RootMind Service › Remote", async () => {
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

describe("SettingsScreen language", () => {
  it("switches every label at once and keeps the choice", async () => {
    serveConfigs();
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");
    expect(screen.getByRole("heading", { name: "Settings" })).toBeTruthy();

    fireEvent.change(screen.getByLabelText(/^Language/), { target: { value: "de" } });

    expect(await screen.findByRole("heading", { name: "Einstellungen" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "Darstellung" })).toBeTruthy();
    expect(screen.getByRole("heading", { name: "RootMind-Dienst" })).toBeTruthy();
    expect(screen.getByRole("button", { name: "Speichern" })).toBeTruthy();
    expect(localStorage.getItem("rlm.language")).toBe("de");
    // The loaded settings stay.
    expect(screen.getByDisplayValue("*.a")).toBeTruthy();
  });
});

describe("SettingsScreen LLM key", () => {
  const KEY = "xai-0123456789abcdef"; // pragma: allowlist secret (test value)

  /** Config A without keys first; after a save, the backend reports the key masked. */
  function serveKeySave(saved: Record<string, string>) {
    vi.mocked(rlm.getConfig)
      .mockResolvedValueOnce(structuredClone(CONFIGS[A]))
      .mockResolvedValue({ ...structuredClone(CONFIGS[A]), llm: { ...CONFIGS[A].llm, api_keys: saved } });
  }

  it("disables the model while the provider has no saved key", async () => {
    serveConfigs();
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");
    expect((screen.getByLabelText(/^Model/) as HTMLSelectElement).disabled).toBe(true);
    expect(screen.getByText("Save an API key for this provider first.")).toBeTruthy();
    expect(screen.queryByRole("button", { name: "Load models" })).toBeNull();
  });

  it("preselects the provider from the key format, with a hint that can be overridden", async () => {
    serveConfigs();
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");

    fireEvent.change(screen.getByPlaceholderText("sk-…"), { target: { value: KEY } });
    const provider = screen.getByLabelText(/^Provider/) as HTMLSelectElement;
    expect(provider.value).toBe("xai");
    expect(
      screen.getByText("Detected from the key: xAI. You can choose another provider."),
    ).toBeTruthy();

    fireEvent.change(provider, { target: { value: "openai" } });
    expect((screen.getByLabelText(/^Provider/) as HTMLSelectElement).value).toBe("openai");
    expect(screen.queryByText(/Detected from the key/)).toBeNull();
    // The typed key stays for the chosen provider.
    expect((screen.getByPlaceholderText("sk-…") as HTMLInputElement).value).toBe(KEY);
  });

  it("saves a valid key with its provider and a model, and fills the model list", async () => {
    serveKeySave({ xai: "…cdef" });
    vi.mocked(rlm.listLlmModels).mockResolvedValue({ provider: "xai", models: ["grok-3", "grok-4"] });
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");

    fireEvent.change(screen.getByPlaceholderText("sk-…"), { target: { value: KEY } });
    fireEvent.click(screen.getByRole("button", { name: "Save API key" }));

    expect(await screen.findByText("API key saved.")).toBeTruthy();
    expect(rlm.listLlmModels).toHaveBeenCalledWith({ provider: "xai", api_key: KEY });
    // One partial save: the detected provider, the first listed model (the
    // selected one is not listed) and the keys; the other sections keep theirs.
    expect(vi.mocked(rlm.saveConfig).mock.calls).toEqual([
      [{ llm: { provider: "xai", model: "grok-3", api_keys: { xai: KEY } } }],
    ]);

    const model = screen.getByLabelText(/^Model/) as HTMLSelectElement;
    expect(model.disabled).toBe(false);
    expect(Array.from(model.options).map((o) => o.value)).toEqual(["grok-3", "grok-4"]);
    expect(model.value).toBe("grok-3");
    // Nothing is left for the main Save.
    expect(screen.queryByText(/Please save/)).toBeNull();
    // The field shows the mask, never the key.
    expect((screen.getByPlaceholderText("sk-…") as HTMLInputElement).value).toBe("…cdef");
    expect(screen.getByText(/Saved: …cdef/)).toBeTruthy();
  });

  it("keeps the selected model when the provider lists it", async () => {
    serveKeySave({ anthropic: "…cdef" });
    vi.mocked(rlm.listLlmModels).mockResolvedValue({
      provider: "anthropic",
      models: ["claude-x", "model-a"],
    });
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");

    fireEvent.change(screen.getByPlaceholderText("sk-…"), { target: { value: "sk-ant-0123456789" } });
    fireEvent.click(screen.getByRole("button", { name: "Save API key" }));

    expect(await screen.findByText("API key saved.")).toBeTruthy();
    expect(vi.mocked(rlm.saveConfig).mock.calls).toEqual([
      [{ llm: { provider: "anthropic", model: "model-a", api_keys: { anthropic: "sk-ant-0123456789" } } }],
    ]);
    expect((screen.getByLabelText(/^Model/) as HTMLSelectElement).value).toBe("model-a");
  });

  it("removes only the key when the field is cleared and saved", async () => {
    const withKey = structuredClone(CONFIGS[A]);
    withKey.llm.api_keys = { anthropic: "…abcd", openai: "…wxyz" };
    vi.mocked(rlm.getConfig)
      .mockResolvedValueOnce(withKey)
      .mockResolvedValue({ ...structuredClone(withKey), llm: { ...withKey.llm, api_keys: { openai: "…wxyz" } } });
    vi.mocked(rlm.listLlmModels).mockResolvedValue({ provider: "anthropic", models: ["model-a"] });
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");
    await waitFor(() => expect(rlm.listLlmModels).toHaveBeenCalledTimes(1));

    fireEvent.change(screen.getByPlaceholderText("sk-…"), { target: { value: "" } });
    fireEvent.click(screen.getByRole("button", { name: "Save API key" }));

    expect(await screen.findByText("API key removed.")).toBeTruthy();
    // No check with the provider, and neither provider nor model is sent.
    expect(rlm.listLlmModels).toHaveBeenCalledTimes(1);
    expect(vi.mocked(rlm.saveConfig).mock.calls).toEqual([
      [{ llm: { api_keys: { anthropic: "", openai: "…wxyz" } } }],
    ]);
    expect((screen.getByLabelText(/^Model/) as HTMLSelectElement).disabled).toBe(true);
  });

  it("shows the provider's error for an invalid key and keeps the previous state", async () => {
    serveConfigs();
    vi.mocked(rlm.listLlmModels).mockRejectedValue(new ApiError(502, "Incorrect API key provided"));
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");

    fireEvent.change(screen.getByPlaceholderText("sk-…"), { target: { value: KEY } });
    fireEvent.click(screen.getByRole("button", { name: "Save API key" }));

    expect(
      await screen.findByText("The key was not saved: Incorrect API key provided"),
    ).toBeTruthy();
    expect(rlm.saveConfig).not.toHaveBeenCalled();
    expect((screen.getByLabelText(/^Model/) as HTMLSelectElement).disabled).toBe(true);
  });
});

describe("SettingsScreen Ollama", () => {
  const URL_FIELD = "http://localhost:11434";

  function serveOllamaConfig(baseUrl?: string) {
    const config = configFor("a", 11);
    config.llm = { provider: "ollama", model: "qwen3:8b", api_keys: {}, base_url: baseUrl };
    vi.mocked(rlm.getConfig).mockResolvedValue(structuredClone(config));
  }

  it("shows a server address instead of an API key and lists models without a key", async () => {
    serveOllamaConfig(URL_FIELD);
    vi.mocked(rlm.listLlmModels).mockResolvedValue({
      provider: "ollama",
      models: ["llama3.2:3b", "qwen3:8b"],
    });
    render(<SettingsScreen />);

    expect(await screen.findByDisplayValue(URL_FIELD)).toBeTruthy();
    expect(screen.queryByPlaceholderText("sk-…")).toBeNull();
    await waitFor(() =>
      expect(rlm.listLlmModels).toHaveBeenCalledWith({ provider: "ollama", base_url: URL_FIELD }),
    );
    const model = (await screen.findByLabelText(/^Model/)) as HTMLSelectElement;
    await waitFor(() => expect(model.disabled).toBe(false));
    expect(Array.from(model.options).map((o) => o.value)).toEqual(["llama3.2:3b", "qwen3:8b"]);
  });

  it("checks the address, then saves it with the provider and a listed model", async () => {
    serveOllamaConfig(URL_FIELD);
    vi.mocked(rlm.listLlmModels).mockResolvedValue({ provider: "ollama", models: ["llama3.2:3b"] });
    render(<SettingsScreen />);
    const input = await screen.findByDisplayValue(URL_FIELD);

    fireEvent.change(input, { target: { value: "http://nas:11434" } });
    fireEvent.click(screen.getByRole("button", { name: "Check and save the Ollama server address" }));

    expect(await screen.findByText("Connected, 1 models found. Saved.")).toBeTruthy();
    expect(rlm.listLlmModels).toHaveBeenLastCalledWith({
      provider: "ollama",
      base_url: "http://nas:11434",
    });
    expect(vi.mocked(rlm.saveConfig).mock.calls).toEqual([
      [{ llm: { provider: "ollama", model: "llama3.2:3b", base_url: "http://nas:11434" } }],
    ]);
  });

  it("does not save a server that has no models", async () => {
    serveOllamaConfig(URL_FIELD);
    vi.mocked(rlm.listLlmModels)
      .mockResolvedValueOnce({ provider: "ollama", models: ["qwen3:8b"] })
      .mockResolvedValue({ provider: "ollama", models: [] });
    render(<SettingsScreen />);
    const input = await screen.findByDisplayValue(URL_FIELD);

    fireEvent.change(input, { target: { value: "http://nas:11434" } });
    fireEvent.click(screen.getByRole("button", { name: "Check and save the Ollama server address" }));

    expect(await screen.findByText(/has no models/)).toBeTruthy();
    expect(rlm.saveConfig).not.toHaveBeenCalled();
  });

  it("shows the server's error and saves nothing when it cannot be reached", async () => {
    serveOllamaConfig(URL_FIELD);
    vi.mocked(rlm.listLlmModels)
      .mockResolvedValueOnce({ provider: "ollama", models: ["qwen3:8b"] })
      .mockRejectedValue(new ApiError(502, "Could not reach Ollama"));
    render(<SettingsScreen />);
    const input = await screen.findByDisplayValue(URL_FIELD);

    fireEvent.change(input, { target: { value: "http://nowhere:11434" } });
    fireEvent.click(screen.getByRole("button", { name: "Check and save the Ollama server address" }));

    expect(await screen.findByText(/Not saved: .*Could not reach Ollama/)).toBeTruthy();
    expect(rlm.saveConfig).not.toHaveBeenCalled();
  });

  it("switching to Ollama loads the models without a key", async () => {
    serveConfigs();
    vi.mocked(rlm.listLlmModels).mockResolvedValue({ provider: "ollama", models: ["qwen3:8b"] });
    render(<SettingsScreen />);
    await screen.findByDisplayValue("*.a");

    fireEvent.change(screen.getByLabelText(/^Provider/), { target: { value: "ollama" } });

    await waitFor(() => expect(rlm.listLlmModels).toHaveBeenCalled());
    expect(screen.queryByPlaceholderText("sk-…")).toBeNull();
    expect(screen.getByPlaceholderText(URL_FIELD)).toBeTruthy();
  });
});

describe("SettingsScreen model check", () => {
  function serveKeyedConfig(effort?: string) {
    const config = configFor("a", 11);
    config.llm = {
      provider: "anthropic",
      model: "claude-a",
      api_keys: { anthropic: "…abcd" },
      ...(effort ? { reasoning_effort: effort } : {}),
    };
    vi.mocked(rlm.getConfig).mockResolvedValue(structuredClone(config));
    vi.mocked(rlm.listLlmModels).mockResolvedValue({
      provider: "anthropic",
      models: ["claude-a", "claude-b"],
    });
  }

  async function modelSelect() {
    const model = (await screen.findByLabelText(/^Model/)) as HTMLSelectElement;
    await waitFor(() => expect(model.disabled).toBe(false));
    return model;
  }

  it("checks the saved model on load and a new one when it is chosen, without a button", async () => {
    serveKeyedConfig();
    vi.mocked(rlm.checkLlmModel).mockImplementation(async ({ provider, model }) => ({
      provider,
      model,
      ok: true,
      reason: null,
      checked_now: model === "claude-b",
    }));
    render(<SettingsScreen />);

    const select = await modelSelect();
    await waitFor(() =>
      expect(rlm.checkLlmModel).toHaveBeenCalledWith({ provider: "anthropic", model: "claude-a" }),
    );
    expect(screen.queryByText("Model checked ✓")).toBeNull(); // known: nothing to say
    expect(screen.queryByRole("button", { name: "Check the model" })).toBeNull();

    fireEvent.change(select, { target: { value: "claude-b" } });

    expect(rlm.checkLlmModel).toHaveBeenCalledWith({ provider: "anthropic", model: "claude-b" });
    expect(await screen.findByText("Model checked ✓")).toBeTruthy();
  });

  it("drops a model that cannot be used and goes back to the previous one", async () => {
    serveKeyedConfig();
    vi.mocked(rlm.checkLlmModel).mockImplementation(async ({ provider, model }) =>
      model === "claude-b"
        ? {
            provider,
            model,
            ok: false,
            reason: "The LLM provider (anthropic) returned HTTP 404; check the model name",
          }
        : { provider, model, ok: true, reason: null },
    );
    render(<SettingsScreen />);
    const select = await modelSelect();

    fireEvent.change(select, { target: { value: "claude-b" } });

    expect(
      await screen.findByText('"claude-b" does not work with RootMind – "claude-a" chosen.'),
    ).toBeTruthy();
    expect(select.value).toBe("claude-a");
    expect(Array.from(select.options).map((o) => o.value)).toEqual(["claude-a"]);
    expect(screen.queryByText(/HTTP 404/)).toBeNull(); // no error
  });

  it("shows a single reasoning effort level as fixed", async () => {
    serveKeyedConfig();
    vi.mocked(rlm.checkLlmModel).mockImplementation(async ({ provider, model }) => ({
      provider,
      model,
      ok: true,
      reason: null,
      efforts: ["none"],
    }));
    render(<SettingsScreen />);

    const effort = (await screen.findByLabelText(/^Reasoning effort/)) as HTMLSelectElement;
    expect(Array.from(effort.options).map((o) => o.value)).toEqual(["none"]);
    expect(effort.value).toBe("none");
    expect(effort.disabled).toBe(true);
  });

  it("says when the check could not run", async () => {
    serveKeyedConfig();
    vi.mocked(rlm.checkLlmModel).mockResolvedValue({
      provider: "anthropic",
      model: "claude-a",
      ok: null,
      reason: "LLM request timed out after 60s",
    });
    render(<SettingsScreen />);

    expect(
      await screen.findByText("Could not check the model: LLM request timed out after 60s"),
    ).toBeTruthy();
  });

  it("offers the reasoning effort levels of the model and saves the choice", async () => {
    serveKeyedConfig("high");
    vi.mocked(rlm.checkLlmModel).mockImplementation(async ({ provider, model }) => ({
      provider,
      model,
      ok: true,
      reason: null,
      efforts: model === "claude-a" ? ["low", "medium", "high"] : [],
    }));
    render(<SettingsScreen />);

    const effort = (await screen.findByLabelText(/^Reasoning effort/)) as HTMLSelectElement;
    expect(Array.from(effort.options).map((o) => o.textContent)).toEqual([
      "Automatic",
      "Low",
      "Medium",
      "High",
    ]);
    expect(effort.value).toBe("high");

    fireEvent.change(effort, { target: { value: "low" } });
    fireEvent.click(screen.getByRole("button", { name: "Save" }));
    await waitFor(() => expect(rlm.saveConfig).toHaveBeenCalled());
    expect(savedConfigs()[0].llm.reasoning_effort).toBe("low");

    // A model without levels: no setting.
    fireEvent.change(await modelSelect(), { target: { value: "claude-b" } });
    await waitFor(() => expect(screen.queryByLabelText(/^Reasoning effort/)).toBeNull());
  });
});
