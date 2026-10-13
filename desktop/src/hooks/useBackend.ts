import { useEffect } from "react";
import { ApiError } from "../api/client";
import { checkAuth, getHealth, getRoot } from "../api/rlm";
import { serviceConnect, sidecarStatus } from "../api/backend";
import type { ServiceConnect, SidecarStatus } from "../api/backend";
import type { RootInfo } from "../types/api";
import { useStore } from "../state/store";
import type { BackendKind, ConnState } from "../state/store";

/** How often the heartbeat re-checks the backend. */
export const HEARTBEAT_MS = 5_000;

// Number of built-in backend starts in flight (sidecar_start can take up to
// 90 s). While one runs, the check leaves the "starting" state alone.
let builtinStarts = 0;

/** Run a start of the built-in backend; the connectivity check waits for it. */
export async function whileStartingBuiltin<T>(start: () => Promise<T>): Promise<T> {
  builtinStarts++;
  try {
    return await start();
  } finally {
    builtinStarts--;
  }
}

/**
 * Read the built-in backend's state after it did not answer (or answered 401).
 * A new URL or token after a crash restart is taken over; otherwise `fallback`
 * becomes the connection state.
 */
async function syncBuiltin(fallback: "offline" | "unauthorized") {
  let st: SidecarStatus | null = null;
  try {
    st = await sidecarStatus();
  } catch {
    /* outside Tauri: nothing to ask */
  }
  const s = useStore.getState();
  if (s.kind !== "builtin" || s.conn === "switching" || builtinStarts > 0) return;
  s.setSidecar(st);
  if (st?.state === "running" && st.url && st.token) {
    if (st.url !== s.baseUrl || st.token !== s.token) {
      s.setBackend({ kind: "builtin", url: st.url, token: st.token });
    } else {
      s.setConn(fallback);
    }
    return;
  }
  if (st?.state === "starting" || st?.state === "restarting") {
    s.setConn("starting");
    return;
  }
  s.setConn("offline");
}

/**
 * Ask Rust for the background service's URL and token. Rust hands out the
 * token only after checking that the service's own process listens on the
 * port. On any failure the URL and token are cleared.
 * Returns true when the service is connected.
 */
async function syncService(): Promise<boolean> {
  let r: ServiceConnect;
  try {
    r = await serviceConnect();
  } catch {
    r = { ok: false, reason: "not_running" };
  }
  const s = useStore.getState();
  if (s.kind !== "service" || s.conn === "switching") return false;
  if (r.ok) {
    s.setServiceIssue(null);
    if (r.url !== s.baseUrl || r.token !== s.token) {
      s.setBackend({ kind: "service", url: r.url, token: r.token });
    }
    return true;
  }
  s.setServiceIssue(r.reason);
  if (s.baseUrl || s.token) s.setBackend({ kind: "service", url: "", token: null });
  s.setConn("offline");
  return false;
}

async function onUnreachable(kind: BackendKind) {
  if (kind === "builtin") await syncBuiltin("offline");
  else useStore.getState().setConn("offline");
}

async function onUnauthorized(kind: BackendKind) {
  if (kind === "builtin") await syncBuiltin("unauthorized");
  else useStore.getState().setConn("unauthorized");
}

async function checkOnce() {
  let s = useStore.getState();
  if (!s.kind || s.conn === "switching") return;
  if (s.kind === "builtin" && builtinStarts > 0) return;
  // Service mode: confirm the listener (and fetch the token) on every check.
  if (s.kind === "service" && !(await syncService())) return;

  s = useStore.getState();
  const { kind, baseUrl, token } = s;
  if (!kind) return;
  // Results for a backend that is no longer in use are dropped.
  const current = () => {
    const n = useStore.getState();
    return n.kind === kind && n.baseUrl === baseUrl && n.token === token && n.conn !== "switching";
  };

  if (!baseUrl) {
    await onUnreachable(kind);
    return;
  }

  try {
    await getHealth();
  } catch {
    if (current()) await onUnreachable(kind);
    return;
  }
  if (!current()) return;

  let root: RootInfo | null = null;
  try {
    root = await getRoot();
  } catch {
    /* the version stays unknown */
  }
  if (!current()) return;

  if (token) {
    // One cheap authenticated request: is the token (still) accepted?
    try {
      await checkAuth();
    } catch (e) {
      if (!current()) return;
      if (e instanceof ApiError && e.status === 401) {
        await onUnauthorized(kind);
        return;
      }
      // /health answered, so a slow /status is not a lost connection.
    }
  } else if (root?.auth === "bearer") {
    // The backend needs a token and we have none.
    await onUnauthorized(kind);
    return;
  }
  if (!current()) return;
  const now = useStore.getState();
  now.setVersion(root?.version ?? null);
  if (kind === "builtin") now.setSidecar(null);
  now.setConn("online");
}

let running: Promise<void> | null = null;
let again = false;

/**
 * Check the backend in use and update `conn` (and the URL and token, when
 * Rust reports new ones). Calls during a check run one more check after it.
 */
export function recheck(): Promise<void> {
  if (running) {
    again = true;
    return running;
  }
  running = (async () => {
    try {
      do {
        again = false;
        try {
          await checkOnce();
        } catch {
          /* an unexpected answer; the next heartbeat checks again */
        }
      } while (again);
    } finally {
      running = null;
    }
  })();
  return running;
}

/** Connectivity to the backend in use, for screens. */
export function useBackend(): {
  conn: ConnState;
  version: string | null;
  kind: BackendKind | null;
  baseUrl: string;
  recheck: () => Promise<void>;
} {
  const conn = useStore((s) => s.conn);
  const version = useStore((s) => s.version);
  const kind = useStore((s) => s.kind);
  const baseUrl = useStore((s) => s.baseUrl);
  return { conn, version, kind, baseUrl, recheck };
}

/**
 * The heartbeat (AppLayout only): check when the backend changes, right after
 * a 401, and every HEARTBEAT_MS. Paused while a question is in flight so a
 * backend busy answering it isn't misreported as offline.
 */
export function useBackendHeartbeat() {
  const kind = useStore((s) => s.kind);
  const baseUrl = useStore((s) => s.baseUrl);
  const token = useStore((s) => s.token);
  const conn = useStore((s) => s.conn);

  useEffect(() => {
    void recheck();
  }, [kind, baseUrl, token]);

  useEffect(() => {
    if (conn === "unauthorized") void recheck();
  }, [conn]);

  useEffect(() => {
    const id = setInterval(() => {
      if (useStore.getState().busy) return;
      void recheck();
    }, HEARTBEAT_MS);
    return () => clearInterval(id);
  }, []);
}
