// Switching between backends: the built-in one, the background service and a
// custom URL. Screens and dialogs call these; the connectivity check
// (hooks/useBackend.ts) takes over after each switch.

import {
  commandError,
  serviceInfo,
  sidecarRestart,
  sidecarStart,
  sidecarStatus,
} from "../api/backend";
import type { BackendInfo, ServiceInfo, ServiceResult } from "../api/backend";
import { recheck, whileStartingBuiltin } from "../hooks/useBackend";
import { decideBackend } from "./bootstrap";
import type { Decision } from "./bootstrap";
import { DEFAULT_BASE_URL, normalizeUrl, persistBackend, useStore } from "./store";
import type { BackendKind } from "./store";

async function readSidecarStatus() {
  try {
    return await sidecarStatus();
  } catch {
    return null;
  }
}

async function runBuiltin(start: () => Promise<BackendInfo>): Promise<boolean> {
  const s = useStore.getState();
  s.setSidecar(null);
  s.setServiceIssue(null);
  s.setBackend({ kind: "builtin", url: "", token: null });
  s.setConn("starting");
  try {
    const info = await whileStartingBuiltin(start);
    useStore.getState().setBackend({ kind: "builtin", url: info.url, token: info.token });
    return true;
  } catch {
    const st = await readSidecarStatus();
    const n = useStore.getState();
    if (n.kind === "builtin" && !n.baseUrl) {
      n.setSidecar(st);
      n.setConn("offline");
    }
    return false;
  }
}

/** Use the built-in backend (start it if needed). Persists the "auto" mode. */
export function startBuiltin(): Promise<boolean> {
  return runBuiltin(sidecarStart);
}

/** Restart the built-in backend and use it. */
export function restartBuiltin(): Promise<boolean> {
  return runBuiltin(sidecarRestart);
}

/** Use the background service; the check asks Rust for its URL and token. */
export async function connectService(): Promise<boolean> {
  const s = useStore.getState();
  s.setServiceIssue(null);
  s.setBackend({ kind: "service", url: "", token: null });
  await recheck();
  return useStore.getState().conn === "online";
}

/** Use a backend at `url` with an optional token, and remember it. */
export async function connectCustom(url: string, token: string | null): Promise<void> {
  useStore.getState().setBackend({
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
      // answer on that port, such as a backend in everyday use.
      const s = useStore.getState();
      s.setBackend({ kind: "custom", url: "", token: d.token }, { persist: false });
      s.setConn("offline");
      return;
    }
    case "builtin":
      await startBuiltin();
      return;
    case "service":
      await connectService();
      return;
  }
}

/**
 * "Use default backend": forget a custom backend and pick the background
 * service (when it is ours and readable) or the built-in backend, as at start.
 */
export async function switchToDefaultBackend(): Promise<void> {
  persistBackend({ kind: "builtin", url: "", token: null }); // mode "auto", no URL or token
  const mode = useStore.getState().appMode;
  const service: ServiceInfo | null =
    mode && !mode.dev ? await serviceInfo().catch(() => null) : null;
  await applyDecision(
    decideBackend({
      dev: mode?.dev ?? true,
      builtinAvailable: mode?.builtinAvailable ?? false,
      storedMode: "auto",
      storedBaseUrl: null,
      storedToken: null,
      service,
    }),
  );
}

/** Which backend `info` is: the built-in one when it matches the running child, else the service. */
async function kindOf(info: BackendInfo): Promise<BackendKind> {
  const st = await readSidecarStatus();
  return st?.state === "running" && st.url === info.url ? "builtin" : "service";
}

/**
 * Use the backend a service change left in place. On success that is
 * `successKind`; after a failure Rust returns the backend that still runs (the
 * service or the built-in one), or none, in which case `fallbackKind` is shown
 * as unreachable.
 */
export async function applyServiceResult(
  r: ServiceResult,
  successKind: BackendKind,
  fallbackKind: BackendKind,
): Promise<void> {
  const s = useStore.getState();
  if (r.ok) {
    s.setServiceIssue(null);
    s.setBackend({ kind: successKind, url: r.backend.url, token: r.backend.token });
    return;
  }
  if (r.backend) {
    const kind = await kindOf(r.backend);
    useStore.getState().setBackend({ kind, url: r.backend.url, token: r.backend.token });
    return;
  }
  s.setBackend({ kind: fallbackKind, url: "", token: null });
}

export type ServiceChange =
  | { kind: "result"; result: ServiceResult }
  | { kind: "error"; message: string };

/**
 * Run an elevated service change and switch to the backend it leaves in
 * place. With `switching` the connection state is "switching" meanwhile, so
 * neither the heartbeat nor the backend dialog reacts to the backend going
 * down on purpose. With `keepBackend` the backend in use stays (a custom
 * backend while the service is updated or MCP over HTTP is switched).
 */
export async function runServiceChange(
  op: () => Promise<ServiceResult>,
  opts: {
    successKind: BackendKind;
    fallbackKind: BackendKind;
    switching: boolean;
    keepBackend?: boolean;
  },
): Promise<ServiceChange> {
  if (opts.switching) useStore.getState().setConn("switching");
  try {
    const result = await op();
    if (!opts.keepBackend) await applyServiceResult(result, opts.successKind, opts.fallbackKind);
    return { kind: "result", result };
  } catch (e) {
    // The command failed before or after the change. If the service is gone
    // (turned off, but the built-in backend did not start), show that one.
    const info = await serviceInfo().catch(() => null);
    const s = useStore.getState();
    if (s.kind === "service" && info && !info.installed) {
      s.setBackend({ kind: "builtin", url: "", token: null });
    }
    return { kind: "error", message: commandError(e) };
  } finally {
    const s = useStore.getState();
    if (s.conn === "switching") s.setConn("checking");
    void recheck();
  }
}
