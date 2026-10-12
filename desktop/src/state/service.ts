// Pure helpers for the "Background service" section in Settings.

import type { ServiceInfo, ServiceResult } from "../api/backend";

/** Label keys (labels.settings.serviceCodes) for the outcome of a failed service change. */
export type ServiceMessageKey =
  | "cancelled"
  | "unexpected"
  | "unsupported"
  | "foreignService"
  | "portBusy"
  | "notElevated"
  | "dataFolderRefused"
  | "healthCheckFailed"
  | "accountMismatch"
  | "accessContext"
  | "blocked";

/**
 * Whether a launch error means Windows security blocked the program: 225, 226
 * (antivirus), 1260 (policy) and the ERROR_SYSTEM_INTEGRITY_* range 4550-4599
 * (Smart App Control and WDAC). The same list is in backend.rs.
 */
export function isBlockedCode(code: number): boolean {
  return code === 225 || code === 226 || code === 1260 || (code >= 4550 && code <= 4599);
}

/** The service exit codes (ARCHITECTURE.md, "Background service"), and 1223 for a cancelled UAC prompt. */
const CODE_KEYS: Record<number, ServiceMessageKey> = {
  1: "unexpected",
  2: "unsupported",
  3: "foreignService",
  4: "portBusy",
  5: "notElevated",
  6: "dataFolderRefused",
  7: "healthCheckFailed",
  8: "accountMismatch",
  9: "accessContext",
};

/** The message for a failed service change. */
export function serviceResultKey(r: Extract<ServiceResult, { ok: false }>): ServiceMessageKey {
  if (r.cancelled || r.code === 1223) return "cancelled";
  if (isBlockedCode(r.code)) return "blocked";
  return CODE_KEYS[r.code] ?? "unexpected";
}

/** The service changes the Settings section can start. */
export type ServiceOp = "enable" | "disable" | "mcpOn" | "mcpOff" | "update";

/**
 * True once service_info shows that the administrator approved the change
 * and it is under way; the UI then switches from "Waiting for administrator
 * approval" to the next step. `before` is service_info from before the change.
 */
export function changeUnderWay(
  op: ServiceOp,
  info: ServiceInfo,
  before: ServiceInfo | null,
): boolean {
  switch (op) {
    case "enable":
      return info.installed && info.ours;
    case "disable":
      return !info.installed;
    case "mcpOn":
      return info.mcp.installed;
    case "mcpOff":
      return !info.mcp.installed;
    case "update":
      // The update stops the service (or starts a stopped one).
      return info.state !== (before?.state ?? "running");
  }
}

interface ParsedVersion {
  nums: number[];
  pre: boolean;
}

function parseVersion(v: string): ParsedVersion | null {
  const m = /^v?(\d+(?:\.\d+)*)(.*)$/i.exec(v.trim());
  if (!m) return null;
  // 1.2.0rc1 (Python), 1.2.0-rc.1 (Cargo), 1.2.0.dev3: before 1.2.0.
  const pre = /^[-.]?(a|alpha|b|beta|c|rc|pre|preview|dev)/i.test(m[2]);
  return { nums: m[1].split(".").map(Number), pre };
}

/** True when version `a` is older than `b`. Unparseable versions are never older. */
export function isOlderVersion(a: string, b: string): boolean {
  const pa = parseVersion(a);
  const pb = parseVersion(b);
  if (!pa || !pb) return false;
  const len = Math.max(pa.nums.length, pb.nums.length);
  for (let i = 0; i < len; i++) {
    const x = pa.nums[i] ?? 0;
    const y = pb.nums[i] ?? 0;
    if (x !== y) return x < y;
  }
  return pa.pre && !pb.pre;
}

/** The values for n8n's MCP Client Tool node. */
export function n8nSnippet(endpoint: string, token: string): string {
  return [
    `Endpoint: ${endpoint}`,
    "Server Transport: HTTP Streamable",
    "Authentication: Header Auth",
    "Header Name: Authorization",
    `Header Value: Bearer ${token}`,
  ].join("\n");
}

/** README section about connecting n8n to the MCP endpoint. */
export const N8N_DOCS_URL = "https://github.com/cgaeking/rootmind#reach-mcp-from-n8n";
