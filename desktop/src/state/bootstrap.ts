// Which backend the app uses when it starts. decideBackend() is pure and
// unit-tested; hooks/useBackendBootstrap.ts gathers its inputs (app_mode,
// service_info, the 8000 probe, the first-run question) and applies the result.

import type { ServiceInfo } from "../api/backend";
import { DEFAULT_BASE_URL } from "./store";
import type { BackendMode } from "./store";

/** Where a RootMind server from version 1.1 usually runs. */
export const PROBE_URL = "http://127.0.0.1:8000";

/** What answered on PROBE_URL: an unauthenticated RootMind API, or anything else (or nothing). */
export type Probe8000 = "rootmind_open" | "other";
/** The answer to the first-run question: the server found on PROBE_URL, or the built-in backend. */
export type FirstRunChoice = "custom" | "builtin";

export interface BootstrapInputs {
  /** A debug build (app_mode). */
  dev: boolean;
  /** The bundled backend exists (app_mode). */
  builtinAvailable: boolean;
  storedMode: BackendMode | null;
  storedBaseUrl: string | null;
  storedToken: string | null;
  /** service_info(); null when it could not be read. */
  service: ServiceInfo | null;
  /** The probe result; absent until probed. */
  probe8000?: Probe8000;
  /** The user's answer to the first-run question; absent until asked. */
  firstRunChoice?: FirstRunChoice;
}

export type Decision =
  /** Use this URL and token (persisted as the custom backend). */
  | { action: "custom"; url: string; token: string | null }
  /** Start the built-in backend. */
  | { action: "builtin" }
  /** Connect to the background service. */
  | { action: "service" }
  /** A debug build without a bundled backend: the backend dialog asks for a URL (nothing persisted). */
  | { action: "ask_url"; url: string; token: string | null }
  /** First start: probe PROBE_URL, then decide again with `probe8000`. */
  | { action: "probe" }
  /** First start with an open RootMind server on `url`: ask, then decide again with `firstRunChoice`. */
  | { action: "ask_choice"; url: string };

/** The start-up decision table, first match wins. */
export function decideBackend(i: BootstrapInputs): Decision {
  // 1. The user chose a custom backend.
  if (i.storedMode === "custom") {
    return { action: "custom", url: i.storedBaseUrl || DEFAULT_BASE_URL, token: i.storedToken };
  }

  // 2. A debug build never probes 8000 and never picks the service on its own.
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

  // 4. First start of this version.
  if (i.storedMode === null) {
    // A URL typed into version 1.1 keeps working.
    if (i.storedBaseUrl) return { action: "custom", url: i.storedBaseUrl, token: i.storedToken };
    if (i.probe8000 === undefined) return { action: "probe" };
    if (i.probe8000 === "rootmind_open") {
      if (i.firstRunChoice === undefined) return { action: "ask_choice", url: PROBE_URL };
      if (i.firstRunChoice === "custom") return { action: "custom", url: PROBE_URL, token: null };
    }
    return { action: "builtin" };
  }

  // 5. auto
  return { action: "builtin" };
}

/** Classify the body of GET PROBE_URL/: an unauthenticated RootMind API, or something else. */
export function classifyProbe(body: unknown): Probe8000 {
  if (typeof body !== "object" || body === null) return "other";
  const b = body as { name?: unknown; auth?: unknown };
  if (b.name !== "RootMind API") return "other";
  return b.auth === undefined || b.auth === "none" ? "rootmind_open" : "other";
}

/** GET PROBE_URL/ with a 2 s timeout. Anything but an open RootMind API is "other". */
export async function probe8000(): Promise<Probe8000> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), 2_000);
  try {
    const res = await fetch(`${PROBE_URL}/`, { cache: "no-store", signal: ctrl.signal });
    if (!res.ok) return "other";
    return classifyProbe(await res.json());
  } catch {
    return "other";
  } finally {
    clearTimeout(timer);
  }
}
