// Pure helpers for the RootMind service: what is wrong with it, the messages
// of a failed repair, versions and the n8n snippet.

import type { ServiceConnectFailure, ServiceInfo, ServiceResult } from "../api/backend";
import { labels } from "../i18n/labels";
import type { ServiceChange } from "./backendActions";

/** Label keys (labels.service.codes) for the outcome of a failed service change. */
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
  | "programFolderUnsafe"
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
  10: "programFolderUnsafe",
};

/** The message for a failed service change. */
export function serviceResultKey(r: Extract<ServiceResult, { ok: false }>): ServiceMessageKey {
  if (r.cancelled || r.code === 1223) return "cancelled";
  if (isBlockedCode(r.code)) return "blocked";
  return CODE_KEYS[r.code] ?? "unexpected";
}

/**
 * Why the app cannot use the RootMind service, for the backend dialog, the
 * connection banner and Settings › Service. Label keys: labels.service.problems.
 */
export type ServiceProblem =
  /** Not Windows, or the browser preview: there is no service to reach. */
  | "unsupported"
  /** No RootMind service is installed. */
  | "notInstalled"
  /** A service named RootMind exists that the installer did not set up. */
  | "foreign"
  /** The service is set up for another Windows account (its token is unreadable). */
  | "otherAccount"
  /** The service is installed but stopped. */
  | "notRunning"
  /** Another program listens on the service's port. */
  | "portBusy"
  /** The service runs, but its REST API does not answer. */
  | "noAnswer";

/**
 * The problem from service_connect's failure (`reason`, null when it
 * succeeded but the REST API did not answer) and service_info (`info`, null
 * when it could not be read). service_info names the cases service_connect
 * cannot tell apart: another program's service and a non-Windows build.
 */
export function serviceProblem(
  reason: ServiceConnectFailure | null,
  info: ServiceInfo | null,
): ServiceProblem {
  if (info && (!info.supported || info.blockedReason === "unsupported")) return "unsupported";
  if (info?.blockedReason === "foreign_service" || (info?.installed && !info.ours)) return "foreign";
  switch (reason) {
    case "not_installed":
      return "notInstalled";
    case "token_unreadable":
      return "otherAccount";
    case "not_running":
      return "notRunning";
    case "listener_mismatch":
      return "portBusy";
    default:
      return "noAnswer";
  }
}

/**
 * The problem service_info alone shows, for Settings › Service while the app
 * uses a custom backend (and so does not connect to the service): null while
 * it is unknown (`info` null and not `failed`) or the service runs.
 */
export function problemFromInfo(info: ServiceInfo | null, failed: boolean): ServiceProblem | null {
  if (failed) return "unsupported";
  if (!info) return null;
  if (!info.supported || info.blockedReason === "unsupported") return "unsupported";
  if (!info.installed) return "notInstalled";
  if (!info.ours || info.blockedReason === "foreign_service") return "foreign";
  if (!info.tokenReadable || info.blockedReason === "other_account") return "otherAccount";
  if (info.state !== "running") return "notRunning";
  return null;
}

/**
 * Whether "Repair" (service_update: set the service up again from this app
 * version and start it, keeping its ports, tokens and data) can fix the
 * problem: a stopped, missing or unresponsive service. It cannot free a port
 * another program holds, remove a foreign service, or take over another
 * account's service (it refuses that, code 8). Repairing needs an
 * administrator account.
 */
export function canRepair(problem: ServiceProblem, info: ServiceInfo | null): boolean {
  if (info && !info.canElevate) return false;
  return problem === "notRunning" || problem === "notInstalled" || problem === "noAnswer";
}

/** What to tell the user after a service change; null when it succeeded. */
export function changeMessage(outcome: ServiceChange): { tone: "info" | "err"; text: string } | null {
  if (outcome.kind === "error") return { tone: "err", text: labels.service.failed(outcome.message) };
  if (outcome.result.ok) return null;
  const key = serviceResultKey(outcome.result);
  return { tone: key === "cancelled" ? "info" : "err", text: labels.service.codes[key] };
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
