// Pure helpers for remote access (Settings › RootMind service › Remote access).

import type { RemoteAccessChange, RemoteAccessInfo } from "../types/api";

/** How often the settings poll the tunnel's status while remote access is on. */
export const REMOTE_POLL_MS = 3_000;

/** The public MCP endpoint for the tunnel's address: `https://<host>/mcp`. */
export function publicEndpoint(url: string): string {
  return `${url.replace(/\/+$/, "")}/mcp`;
}

/**
 * What Connect (or "Save and try again") sends: turn remote access on, with
 * the authtoken when one was typed and the domain when it differs from the
 * saved one (`domain` null: not edited; "" removes it).
 */
export function connectChange(
  info: RemoteAccessInfo,
  authtoken: string,
  domain: string | null,
): RemoteAccessChange {
  const change: RemoteAccessChange = { enabled: true };
  const token = authtoken.trim();
  if (token) change.authtoken = token;
  if (domain !== null && domain.trim() !== (info.domain ?? "")) change.domain = domain.trim();
  return change;
}
