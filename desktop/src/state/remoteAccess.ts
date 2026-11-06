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
  if (domain !== null && domain.trim() !== (info.domain ?? ""))
    change.domain = domain.trim();
  return change;
}

/** The form of an ngrok Authtoken the backend accepts (remote_access.check_authtoken). */
const AUTHTOKEN = /^[A-Za-z0-9_.-]{16,256}$/;

/** Whether `value` has the form of an ngrok Authtoken. */
export function looksLikeAuthtoken(value: string): boolean {
  return AUTHTOKEN.test(value.trim());
}

/** Label keys (labels.remote.errors) for the ngrok error codes users run into. */
const NGROK_ERRORS: Record<
  string,
  "domainInUse" | "badAuthtoken" | "agentLimit"
> = {
  ERR_NGROK_334: "domainInUse",
  ERR_NGROK_105: "badAuthtoken",
  ERR_NGROK_107: "badAuthtoken",
  ERR_NGROK_108: "agentLimit",
};

/**
 * The tunnel's error as the settings show it: one sentence for a known ngrok
 * code, with the code in parentheses; else the backend's text (already one
 * line, see remote_access.ngrok_message).
 */
export function tunnelError(
  error: string,
  texts: Record<"domainInUse" | "badAuthtoken" | "agentLimit", string>,
): string {
  const code = /ERR_NGROK_\d+/.exec(error)?.[0];
  const key = code ? NGROK_ERRORS[code] : undefined;
  return key ? `${texts[key]} (${code})` : error;
}
