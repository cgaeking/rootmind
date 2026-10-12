import { useEffect } from "react";
import { create } from "zustand";
import { appMode, inTauri, serviceInfo } from "../api/backend";
import type { AppMode, ServiceInfo } from "../api/backend";
import { applyDecision } from "../state/backendActions";
import { decideBackend, probe8000 } from "../state/bootstrap";
import type { BootstrapInputs, FirstRunChoice } from "../state/bootstrap";
import { readStoredBackend, useStore } from "../state/store";

/** The one-time question on the first start (FirstRunChoice dialog). */
interface FirstRunPrompt {
  /** The URL of the RootMind server that was found, or null when nothing is asked. */
  url: string | null;
  answer: ((choice: FirstRunChoice) => void) | null;
}

export const useFirstRunPrompt = create<FirstRunPrompt>()(() => ({ url: null, answer: null }));

function askFirstRun(url: string): Promise<FirstRunChoice> {
  return new Promise((resolve) => {
    useFirstRunPrompt.setState({
      url,
      answer: (choice) => {
        useFirstRunPrompt.setState({ url: null, answer: null });
        resolve(choice);
      },
    });
  });
}

async function readAppMode(): Promise<AppMode | null> {
  if (!inTauri()) return null;
  try {
    return await appMode();
  } catch {
    return null;
  }
}

async function readServiceInfo(): Promise<ServiceInfo | null> {
  try {
    return await serviceInfo();
  } catch {
    return null;
  }
}

/** Gather the inputs of decideBackend(), ask what has to be asked, and apply the result. */
async function bootstrap() {
  const mode = await readAppMode();
  useStore.getState().setAppMode(mode);
  const stored = readStoredBackend();
  // Outside Tauri (the browser preview) there is no built-in backend and no service.
  const dev = mode?.dev ?? true;
  const inputs: BootstrapInputs = {
    dev,
    builtinAvailable: mode?.builtinAvailable ?? false,
    storedMode: stored.mode,
    storedBaseUrl: stored.baseUrl,
    storedToken: stored.token,
    service: stored.mode !== "custom" && !dev ? await readServiceInfo() : null,
  };
  let decision = decideBackend(inputs);
  if (decision.action === "probe") {
    inputs.probe8000 = await probe8000();
    decision = decideBackend(inputs);
  }
  if (decision.action === "ask_choice") {
    inputs.firstRunChoice = await askFirstRun(decision.url);
    decision = decideBackend(inputs);
  }
  await applyDecision(decision);
}

let started = false;

/**
 * Choose the backend once when the app starts (before the heartbeat has
 * anything to check). Returns the pending first-run question, if any.
 */
export function useBackendBootstrap(): FirstRunPrompt {
  useEffect(() => {
    // Once per app run, also under React's StrictMode double effects.
    if (started) return;
    started = true;
    void bootstrap();
  }, []);
  return useFirstRunPrompt();
}
