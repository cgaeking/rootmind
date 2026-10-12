// Which backend the app uses when it starts. decideBackend() is pure and
// unit-tested; hooks/useBackendBootstrap.ts gathers its inputs (app_mode,
// service_info, the stored choice) and applies the result.

import type { ServiceInfo } from "../api/backend";
import { DEFAULT_BASE_URL } from "./store";
import type { BackendMode } from "./store";

export interface BootstrapInputs {
  /** A debug build (app_mode). */
  dev: boolean;
  /** The bundled backend exists (app_mode). */
  builtinAvailable: boolean;
  /** The stored choice; null when nothing is stored yet (the first start). */
  storedMode: BackendMode | null;
  storedBaseUrl: string | null;
  storedToken: string | null;
  /** service_info(); null when it could not be read. */
  service: ServiceInfo | null;
}

export type Decision =
  /** Use this URL and token (persisted as the custom backend). */
  | { action: "custom"; url: string; token: string | null }
  /** Start the built-in backend. */
  | { action: "builtin" }
  /** Connect to the background service. */
  | { action: "service" }
  /** A debug build without a bundled backend: the backend dialog asks for a URL (nothing persisted). */
  | { action: "ask_url"; url: string; token: string | null };

/** The start-up decision table, first match wins. */
export function decideBackend(i: BootstrapInputs): Decision {
  // 1. The user chose a custom backend (Settings > Advanced).
  if (i.storedMode === "custom") {
    return { action: "custom", url: i.storedBaseUrl || DEFAULT_BASE_URL, token: i.storedToken };
  }

  // 2. A debug build never picks the service on its own.
  if (i.dev) {
    if (i.builtinAvailable) return { action: "builtin" };
    return { action: "ask_url", url: i.storedBaseUrl || DEFAULT_BASE_URL, token: i.storedToken };
  }

  // 3. Our background service. If its token is not readable for this account
  //    (it belongs to another account), run the built-in backend; Settings says why.
  const svc = i.service;
  if (svc?.installed && svc.ours) {
    return svc.tokenReadable ? { action: "service" } : { action: "builtin" };
  }

  // 4. Everything else, the first start included: the built-in backend.
  return { action: "builtin" };
}
