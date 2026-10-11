import { create } from "zustand";

export type ConnState = "unknown" | "checking" | "online" | "offline";
export type ThemePref = "system" | "light" | "dark";

const DEFAULT_BASE_URL = "http://127.0.0.1:8000";

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
  const v = localStorage.getItem("rlm.theme");
  return v === "light" || v === "dark" || v === "system" ? v : "system";
})();

// Apply at module load (before first render) to avoid a flash of the wrong theme.
applyTheme(initialTheme);

interface AppState {
  /** Base URL of the RootMind backend REST API. */
  baseUrl: string;
  setBaseUrl: (url: string) => void;

  /** Last known connectivity to the backend. */
  conn: ConnState;
  setConn: (conn: ConnState) => void;

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
  baseUrl: localStorage.getItem("rlm.baseUrl") ?? DEFAULT_BASE_URL,
  setBaseUrl: (url) => {
    localStorage.setItem("rlm.baseUrl", url);
    set({ baseUrl: url });
  },

  conn: "unknown",
  setConn: (conn) => set({ conn }),

  busy: false,
  setBusy: (busy) => set({ busy }),

  theme: initialTheme,
  setTheme: (theme) => {
    localStorage.setItem("rlm.theme", theme);
    applyTheme(theme);
    set({ theme });
  },
}));
