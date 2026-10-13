// Choosing the backend (the RootMind service or a custom address) and running
// the service changes the app offers (repair, MCP over HTTP). Screens and
// dialogs call these; the connectivity check (hooks/useBackend.ts) takes over
// after each one.

import { commandError } from "../api/backend";
import type { ServiceResult } from "../api/backend";
import { recheck } from "../hooks/useBackend";
import type { Decision } from "./bootstrap";
import { DEFAULT_BASE_URL, normalizeUrl, useStore } from "./store";

/** Use the RootMind service; the check asks Rust for its URL and token. Persists the choice. */
export async function connectService(): Promise<boolean> {
  const s = useStore.getState();
  s.setServiceProblem(null);
  s.setBackend({ kind: "service", url: "", token: null });
  await recheck();
  return useStore.getState().conn === "online";
}

/** Use a backend at `url` with an optional token, and remember it. */
export async function connectCustom(url: string, token: string | null): Promise<void> {
  const s = useStore.getState();
  s.setServiceProblem(null);
  s.setBackend({
    kind: "custom",
    url: normalizeUrl(url) || DEFAULT_BASE_URL,
    token: token?.trim() || null,
  });
  await recheck();
}

/** Carry out a start-up decision (see state/bootstrap.ts). */
export async function applyDecision(d: Decision): Promise<void> {
  switch (d.action) {
    case "custom":
      await connectCustom(d.url, d.token);
      return;
    case "ask_url": {
      // Nothing is contacted and nothing persisted: the backend dialog asks
      // for the URL (it suggests the stored one, else DEFAULT_BASE_URL) and
      // connects only when the user presses Connect. Something else may
      // answer on that port, such as the service in everyday use.
      const s = useStore.getState();
      s.setBackend({ kind: "custom", url: "", token: d.token }, { persist: false });
      s.setConn("offline");
      return;
    }
    case "service":
      await connectService();
      return;
  }
}

export type ServiceChange =
  | { kind: "result"; result: ServiceResult }
  | { kind: "error"; message: string };

/**
 * Run an elevated service change (repair or MCP over HTTP). While the app
 * uses the service, the URL and token Rust returns are taken over, and the
 * connection state is "switching" meanwhile (unless `switching` is false: the
 * backend dialog stays open during its repair), so the heartbeat does not
 * react to the service going down on purpose. A custom backend stays in use.
 */
export async function runServiceChange(
  op: () => Promise<ServiceResult>,
  opts: { switching?: boolean } = {},
): Promise<ServiceChange> {
  const usesService = useStore.getState().kind === "service";
  if (usesService && (opts.switching ?? true)) useStore.getState().setConn("switching");
  try {
    const result = await op();
    // After a failure Rust returns the service if it still answers, else null.
    const s = useStore.getState();
    if (usesService && s.kind === "service" && result.backend) {
      s.setBackend({ kind: "service", url: result.backend.url, token: result.backend.token });
    }
    return { kind: "result", result };
  } catch (e) {
    return { kind: "error", message: commandError(e) };
  } finally {
    const s = useStore.getState();
    if (s.conn === "switching") s.setConn("checking");
    void recheck();
  }
}
