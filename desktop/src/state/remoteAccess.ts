// Pure helpers for remote access (Settings › RootMind service › Remote access).

import type { RemoteAccessChange, RemoteAccessInfo } from "../types/api";

/** README section about remote access. */
export const REMOTE_DOCS_URL = "https://github.com/cgaeking/rootmind#remote-access";

/** How often the settings poll the tunnel's status while remote access is on. */
export const REMOTE_POLL_MS = 3_000;

/** The public MCP endpoint for the tunnel's address: `https://<host>/mcp`. */
export function publicEndpoint(url: string): string {
  return `${url.replace(/\/+$/, "")}/mcp`;
}

/**
 * What "Save" sends: a new authtoken when one was typed, and the domain when
 * it differs from the saved one (`domain` null: not edited). Null when there
 * is nothing to save.
 */
export function settingsChange(
  info: RemoteAccessInfo,
  authtoken: string,
  domain: string | null,
): RemoteAccessChange | null {
  const change: RemoteAccessChange = {};
  const token = authtoken.trim();
  if (token) change.authtoken = token;
  if (domain !== null && domain.trim() !== (info.domain ?? "")) change.domain = domain.trim();
  return Object.keys(change).length ? change : null;
}

/** Whether the switch may be turned on: MCP over HTTP is on, an authtoken is saved and the SDK is there. */
export function canTurnOn(info: RemoteAccessInfo, mcpEnabled: boolean): boolean {
  return mcpEnabled && info.authtoken_saved && info.sdk.available;
}
