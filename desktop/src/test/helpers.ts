// Shared test helpers (vitest only).

import type { ServiceInfo } from "../api/backend";
import { useStore } from "../state/store";

/** Put the app store back into its start state and clear localStorage. */
export function resetStore() {
  localStorage.clear();
  useStore.setState({
    kind: null,
    baseUrl: "",
    token: null,
    conn: "unknown",
    connSince: 0,
    version: null,
    appMode: null,
    sidecar: null,
    serviceIssue: null,
    busy: false,
  });
}

/** A ServiceInfo for "not installed", with overrides. */
export function serviceInfoFixture(over: Partial<ServiceInfo> = {}): ServiceInfo {
  return {
    supported: true,
    installed: false,
    ours: false,
    state: null,
    port: null,
    url: null,
    tokenReadable: false,
    canElevate: true,
    blockedReason: null,
    mcp: { installed: false, state: null, port: null, url: null },
    ...over,
  };
}

/** A JSON fetch Response. */
export function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}
