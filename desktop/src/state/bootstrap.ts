// Which backend the app uses when it starts. decideBackend() is pure and
// unit-tested; hooks/useBackendBootstrap.ts gathers its inputs (app_mode and
// the stored choice) and applies the result.
//
// The installer always sets up the RootMind service, so a release build uses
// it unless the user chose a backend at another address (Settings ›
// Advanced). Whether the service works is found out by the connectivity
// check (hooks/useBackend.ts), which also explains a problem.

import { DEFAULT_BASE_URL } from "./store";
import type { BackendMode } from "./store";

export interface BootstrapInputs {
  /** A debug build, or the browser preview (app_mode). */
  dev: boolean;
  /** The stored choice; null when nothing is stored yet (the first start). */
  storedMode: BackendMode | null;
  storedBaseUrl: string | null;
  storedToken: string | null;
}

export type Decision =
  /** Use this URL and token (persisted as the custom backend). */
  | { action: "custom"; url: string; token: string | null }
  /** Connect to the RootMind service. */
  | { action: "service" }
  /** A debug build: the backend dialog asks for a URL (nothing persisted). */
  | { action: "ask_url"; url: string; token: string | null };

/** The start-up decision table, first match wins. */
export function decideBackend(i: BootstrapInputs): Decision {
  // 1. The user chose a backend at another address (Settings › Advanced).
  if (i.storedMode === "custom") {
    return { action: "custom", url: i.storedBaseUrl || DEFAULT_BASE_URL, token: i.storedToken };
  }

  // 2. A debug build never connects to the installed service on its own: it
  //    holds the user's real index and settings.
  if (i.dev) {
    return { action: "ask_url", url: i.storedBaseUrl || DEFAULT_BASE_URL, token: i.storedToken };
  }

  // 3. Everything else, the first start included: the RootMind service.
  return { action: "service" };
}
