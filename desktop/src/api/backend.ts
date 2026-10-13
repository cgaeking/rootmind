// Typed wrappers for the app's own Tauri commands that reach the RootMind
// service (src-tauri/src/service.rs). The installer always sets the service
// up; the app is a client of it. Field names are camelCase over IPC.
// Unexpected failures reject with a user-facing string.

/** A backend the UI can talk to. */
export interface BackendInfo {
  url: string;
  token: string;
}

export type SvcState = "stopped" | "start_pending" | "stop_pending" | "running" | "other";

export type ServiceBlockedReason = "unsupported" | "not_admin" | "other_account" | "foreign_service";

export interface ServiceInfo {
  /** A Windows build. */
  supported: boolean;
  installed: boolean;
  /** The service program lies under %ProgramFiles%\RootMind Service\. */
  ours: boolean;
  state: SvcState | null;
  port: number | null;
  /** REST base URL, only when the service is ours. */
  url: string | null;
  tokenReadable: boolean;
  /** The app's account is a local administrator. */
  canElevate: boolean;
  blockedReason: ServiceBlockedReason | null;
  mcp: {
    installed: boolean;
    state: SvcState | null;
    port: number | null;
    /** The full MCP endpoint, http://127.0.0.1:<port>/mcp. */
    url: string | null;
  };
}

export type ServiceConnectFailure =
  | "not_installed"
  | "not_running"
  | "listener_mismatch"
  | "token_unreadable";

export type ServiceConnect =
  | { ok: true; url: string; token: string }
  | { ok: false; reason: ServiceConnectFailure };

export type ServiceResult =
  | { ok: true; backend: BackendInfo }
  | { ok: false; cancelled: boolean; code: number; backend: BackendInfo | null };

export interface AppMode {
  /** The app's version. */
  version: string;
  /** A debug build. */
  dev: boolean;
}

/** True inside the Tauri app; false in the plain browser preview (`pnpm dev`). */
export function inTauri(): boolean {
  return typeof window !== "undefined" && "__TAURI_INTERNALS__" in window;
}

async function call<T>(cmd: string, args?: Record<string, unknown>): Promise<T> {
  const { invoke } = await import("@tauri-apps/api/core");
  return invoke<T>(cmd, args);
}

export const appMode = () => call<AppMode>("app_mode");

export const serviceInfo = () => call<ServiceInfo>("service_info");
export const serviceConnect = () => call<ServiceConnect>("service_connect");
export const serviceSetMcp = (enabled: boolean) =>
  call<ServiceResult>("service_set_mcp", { enabled });
export const serviceUpdate = () => call<ServiceResult>("service_update");
export const serviceMcpToken = () => call<string>("service_mcp_token");
export const serviceDetails = () => call<string>("service_details");

/** The message of a rejected command (a string from Rust) or any other error. */
export function commandError(e: unknown): string {
  if (typeof e === "string") return e;
  if (e instanceof Error) return e.message;
  return String(e);
}
