import { useEffect } from "react";
import { appMode, inTauri } from "../api/backend";
import type { AppMode } from "../api/backend";
import { applyDecision } from "../state/backendActions";
import { decideBackend } from "../state/bootstrap";
import { readStoredBackend, useStore } from "../state/store";

async function readAppMode(): Promise<AppMode | null> {
  if (!inTauri()) return null;
  try {
    return await appMode();
  } catch {
    return null;
  }
}

/** Gather the inputs of decideBackend() and apply the result. */
async function bootstrap() {
  const mode = await readAppMode();
  useStore.getState().setAppMode(mode);
  const stored = readStoredBackend();
  await applyDecision(
    decideBackend({
      // Outside Tauri (the browser preview) there is no service.
      dev: mode?.dev ?? true,
      storedMode: stored.mode,
      storedBaseUrl: stored.baseUrl,
      storedToken: stored.token,
    }),
  );
}

let started = false;

/** Choose the backend once when the app starts (before the heartbeat has anything to check). */
export function useBackendBootstrap(): void {
  useEffect(() => {
    // Once per app run, also under React's StrictMode double effects.
    if (started) return;
    started = true;
    void bootstrap();
  }, []);
}
