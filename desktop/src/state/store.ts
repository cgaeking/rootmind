import { create } from "zustand";
import type { AppMode } from "../api/backend";
import type { ServiceProblem } from "./service";

/**
 * Connectivity to the backend the UI uses.
 * - `switching`: a service repair or MCP change is running (the service may
 *   be down on purpose for a while).
 * - `unauthorized`: the backend answered 401.
 * Only `offline` (and `unauthorized` for a custom backend) opens the backend
 * dialog.
 */
export type ConnState =
  | "unknown"
  | "checking"
  | "switching"
  | "online"
  | "offline"
  | "unauthorized";
export type ThemePref = "system" | "light" | "dark";

/**
 * Which backend the UI uses: the RootMind service the installer set up
 * (`service`), or a backend at an address the user entered under Settings ›
 * Advanced (`custom`).
 */
export type BackendKind = "service" | "custom";
/** The stored choice: `auto` is the RootMind service (the value predates the service-only app). */
export type BackendMode = "auto" | "custom";

export interface BackendTarget {
  kind: BackendKind;
  url: string;
  token: string | null;
}

export const DEFAULT_BASE_URL = "http://127.0.0.1:8000";

export const STORAGE_KEYS = {
  mode: "rlm.backendMode",
  baseUrl: "rlm.baseUrl",
  token: "rlm.token",
} as const;

export interface StoredBackend {
  mode: BackendMode | null;
  baseUrl: string | null;
  token: string | null;
}

/** localStorage can throw (blocked storage); treat that as empty. */
function readKey(key: string): string | null {
  try {
    return localStorage.getItem(key);
  } catch {
    return null;
  }
}

function writeKey(key: string, value: string | null) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, value);
  } catch {
    /* storage unavailable: the choice lasts for this session only */
  }
}

export function readStoredBackend(): StoredBackend {
  const mode = readKey(STORAGE_KEYS.mode);
  return {
    mode: mode === "auto" || mode === "custom" ? mode : null,
    baseUrl: readKey(STORAGE_KEYS.baseUrl) || null,
    token: readKey(STORAGE_KEYS.token) || null,
  };
}

/**
 * Persist the backend choice. For the service only the mode is stored (Rust
 * hands out its URL and token on every check, and the token must never be
 * written to disk); the URL and token are stored for a custom backend only.
 */
export function persistBackend(target: BackendTarget) {
  if (target.kind === "custom") {
    writeKey(STORAGE_KEYS.mode, "custom");
    writeKey(STORAGE_KEYS.baseUrl, target.url);
    writeKey(STORAGE_KEYS.token, target.token || null);
  } else {
    writeKey(STORAGE_KEYS.mode, "auto");
    writeKey(STORAGE_KEYS.baseUrl, null);
    writeKey(STORAGE_KEYS.token, null);
  }
}

/** Remove surrounding whitespace and trailing slashes from a URL the user typed. */
export function normalizeUrl(url: string): string {
  return url.trim().replace(/\/+$/, "");
}

/** The connection state after switching to `next`: a different backend (URL or kind) is checked first. */
export function connAfterSwitch(
  prev: { kind: BackendKind | null; baseUrl: string; conn: ConnState },
  next: BackendTarget,
): ConnState {
  if (prev.kind === next.kind && prev.baseUrl === next.url) return prev.conn;
  return "checking";
}

// Match the native window title bar (Windows) to the chosen theme. Only works
// inside the Tauri app; the dev browser preview has no such API.
async function applyNativeTheme(theme: ThemePref) {
  try {
    const { getCurrentWindow } = await import("@tauri-apps/api/window");
    await getCurrentWindow().setTheme(theme === "system" ? null : theme);
  } catch {
    /* not running in Tauri */
  }
}

// "system" lets the OS (prefers-color-scheme) decide; "light"/"dark" force it
// via a data-theme attribute that theme.css keys off. Also retints the native
// title bar.
function applyTheme(theme: ThemePref) {
  const root = document.documentElement;
  if (theme === "system") root.removeAttribute("data-theme");
  else root.setAttribute("data-theme", theme);
  void applyNativeTheme(theme);
}

const initialTheme: ThemePref = ((): ThemePref => {
  const v = readKey("rlm.theme");
  return v === "light" || v === "dark" || v === "system" ? v : "system";
})();

// Apply at module load (before first render) to avoid a flash of the wrong theme.
applyTheme(initialTheme);

interface AppState {
  /** The backend in use; null until the start-up choice is made. */
  kind: BackendKind | null;
  /** Base URL of the backend REST API ("" while none is known). */
  baseUrl: string;
  /** Bearer token for the backend, if it needs one. */
  token: string | null;
  /**
   * Switch to another backend. A different URL or kind sets `conn` to
   * "checking". Persists the choice unless `persist` is false.
   */
  setBackend: (target: BackendTarget, opts?: { persist?: boolean }) => void;

  /** Last known connectivity to the backend. */
  conn: ConnState;
  /** When `conn` last changed (ms since epoch). */
  connSince: number;
  setConn: (conn: ConnState) => void;

  /** Version reported by the backend's GET / (null while unknown). */
  version: string | null;
  setVersion: (version: string | null) => void;

  /** What the app reported about itself (app_mode); null outside Tauri. */
  appMode: AppMode | null;
  setAppMode: (mode: AppMode | null) => void;

  /** Why the RootMind service cannot be used right now; null while it works (or is not in use). */
  serviceProblem: ServiceProblem | null;
  setServiceProblem: (problem: ServiceProblem | null) => void;

  /**
   * True while a long-running question is in flight. The heartbeat pauses
   * while busy so a backend that is tied up answering the query isn't
   * misreported as "offline" mid-request.
   */
  busy: boolean;
  setBusy: (busy: boolean) => void;

  /** Light/dark theme preference (persisted). */
  theme: ThemePref;
  setTheme: (theme: ThemePref) => void;
}

export const useStore = create<AppState>()((set) => ({
  kind: null,
  baseUrl: "",
  token: null,
  setBackend: (target, opts) => {
    const next: BackendTarget = { ...target, url: normalizeUrl(target.url) };
    if (opts?.persist ?? true) persistBackend(next);
    set((s) => {
      const switched = s.kind !== next.kind || s.baseUrl !== next.url;
      const conn = connAfterSwitch(s, next);
      return {
        kind: next.kind,
        baseUrl: next.url,
        token: next.token || null,
        conn,
        connSince: conn !== s.conn ? Date.now() : s.connSince,
        version: switched ? null : s.version,
      };
    });
  },

  conn: "unknown",
  connSince: Date.now(),
  setConn: (conn) => set((s) => (s.conn === conn ? s : { conn, connSince: Date.now() })),

  version: null,
  setVersion: (version) => set({ version }),

  appMode: null,
  setAppMode: (appMode) => set({ appMode }),

  serviceProblem: null,
  setServiceProblem: (serviceProblem) =>
    set((s) => (s.serviceProblem === serviceProblem ? s : { serviceProblem })),

  busy: false,
  setBusy: (busy) => set({ busy }),

  theme: initialTheme,
  setTheme: (theme) => {
    writeKey("rlm.theme", theme);
    applyTheme(theme);
    set({ theme });
  },
}));
