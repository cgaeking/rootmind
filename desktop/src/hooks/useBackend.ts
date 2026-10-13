import { useEffect } from "react";
import { ApiError } from "../api/client";
import { checkAuth, getHealth, getRoot } from "../api/rlm";
import { inTauri, serviceConnect, serviceInfo } from "../api/backend";
import type { ServiceConnect, ServiceInfo } from "../api/backend";
import type { RootInfo } from "../types/api";
import { serviceProblem } from "../state/service";
import { useStore } from "../state/store";
import type { BackendKind, ConnState } from "../state/store";

/** How often the heartbeat re-checks the backend. */
export const HEARTBEAT_MS = 5_000;

async function readServiceInfo(): Promise<ServiceInfo | null> {
  try {
    return await serviceInfo();
  } catch {
    return null;
  }
}

/**
 * Ask Rust for the RootMind service's URL and token. Rust hands out the token
 * only after checking that the service's own process listens on the port. On
 * a failure the URL and token are cleared and the problem is recorded (with
 * service_info for the cases service_connect cannot tell apart).
 * Returns true when the service is connected.
 */
async function syncService(): Promise<boolean> {
  if (!inTauri()) {
    // The browser preview (`pnpm dev`) has no service to reach.
    const s = useStore.getState();
    if (s.kind !== "service" || s.conn === "switching") return false;
    s.setServiceProblem("unsupported");
    s.setConn("offline");
    return false;
  }
  let r: ServiceConnect;
  try {
    r = await serviceConnect();
  } catch {
    r = { ok: false, reason: "not_running" };
  }
  const info = r.ok ? null : await readServiceInfo();
  const s = useStore.getState();
  if (s.kind !== "service" || s.conn === "switching") return false;
  if (r.ok) {
    if (r.url !== s.baseUrl || r.token !== s.token) {
      s.setBackend({ kind: "service", url: r.url, token: r.token });
    }
    return true;
  }
  s.setServiceProblem(serviceProblem(r.reason, info));
  if (s.baseUrl || s.token) s.setBackend({ kind: "service", url: "", token: null });
  s.setConn("offline");
  return false;
}

function onUnreachable(kind: BackendKind) {
  const s = useStore.getState();
  // The service runs and its port is its own, but the REST API does not answer.
  if (kind === "service") s.setServiceProblem("noAnswer");
  s.setConn("offline");
}

async function checkOnce() {
  let s = useStore.getState();
  if (!s.kind || s.conn === "switching") return;
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
    onUnreachable(kind);
    return;
  }

  try {
    await getHealth();
  } catch {
    if (current()) onUnreachable(kind);
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
        useStore.getState().setConn("unauthorized");
        return;
      }
      // /health answered, so a slow /status is not a lost connection.
    }
  } else if (root?.auth === "bearer") {
    // The backend needs a token and we have none.
    useStore.getState().setConn("unauthorized");
    return;
  }
  if (!current()) return;
  const now = useStore.getState();
  now.setVersion(root?.version ?? null);
  now.setServiceProblem(null);
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
