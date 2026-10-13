import { useStore } from "../state/store";
import { labels } from "../i18n/labels";

export class ApiError extends Error {
  status: number;
  constructor(status: number, message: string) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

/** ApiError.status === 0 with this flag means the caller aborted the request. */
export class AbortedError extends ApiError {
  constructor() {
    super(0, labels.common.cancelled);
    this.name = "AbortedError";
  }
}

// Default per-request timeout. Without this a hung backend (accepts the TCP
// connection but never responds) leaves the UI spinning forever: the health
// check never resolves and the app stays on "Unknown".
const DEFAULT_TIMEOUT_MS = 15_000;

/** Abort when either input signal aborts. Works without AbortSignal.any(). */
function combineSignals(a: AbortSignal | undefined, b: AbortSignal): AbortSignal {
  if (!a) return b;
  const ctrl = new AbortController();
  const forward = (from: AbortSignal) => ctrl.abort(from.reason);
  for (const s of [a, b]) {
    if (s.aborted) {
      forward(s);
      break;
    }
    s.addEventListener("abort", () => forward(s), { once: true });
  }
  return ctrl.signal;
}

export interface ApiOptions {
  /** Per-request timeout in ms. Pass 0 to disable. Defaults to 15 s. */
  timeoutMs?: number;
  /**
   * Send no token: for the routes that need none (GET /health and GET /),
   * which the connectivity check calls before it knows who answers.
   */
  anonymous?: boolean;
}

/**
 * The request headers: the JSON content type, the caller's headers, and the
 * bearer token when the backend has one. Without a token no Authorization
 * header is sent.
 */
export function requestHeaders(token: string | null, extra?: HeadersInit): Headers {
  const headers = new Headers({ "Content-Type": "application/json" });
  new Headers(extra ?? {}).forEach((value, key) => headers.set(key, value));
  if (token) headers.set("Authorization", `Bearer ${token}`);
  else headers.delete("Authorization");
  return headers;
}

/**
 * Thin typed fetch wrapper around the RootMind backend REST API.
 * The base URL and token come from the app store (the backend chosen at start
 * or in Settings). Every request is bounded by a timeout and can be aborted via
 * `init.signal`. A 401 answer sets the connection state to "unauthorized", so
 * the app fetches a fresh token (built-in, service) or asks for one (custom).
 */
export async function api<T>(path: string, init?: RequestInit, opts?: ApiOptions): Promise<T> {
  const { baseUrl, token } = useStore.getState();
  const timeoutMs = opts?.timeoutMs ?? DEFAULT_TIMEOUT_MS;

  const timeoutCtrl = new AbortController();
  const timer =
    timeoutMs > 0
      ? setTimeout(() => timeoutCtrl.abort(new DOMException("timeout", "TimeoutError")), timeoutMs)
      : undefined;
  const signal = combineSignals(init?.signal ?? undefined, timeoutCtrl.signal);

  let res: Response;
  try {
    // `init` first, so its own headers cannot replace the merged ones: the
    // backend relies on the JSON content type (a non-JSON body gets 422).
    res = await fetch(`${baseUrl}${path}`, {
      cache: "no-store",
      ...init,
      headers: requestHeaders(opts?.anonymous ? null : token, init?.headers),
      signal,
    });
  } catch (e) {
    const name = (e as Error).name;
    if (name === "AbortError" || name === "TimeoutError") {
      // The caller's own signal firing means an intentional cancel; otherwise
      // it was our timeout.
      if (init?.signal?.aborted) throw new AbortedError();
      throw new ApiError(0, labels.common.timeoutAfter(Math.round(timeoutMs / 1000)));
    }
    throw new ApiError(0, (e as Error).message || labels.common.connectionFailed);
  } finally {
    if (timer) clearTimeout(timer);
  }

  if (res.status === 401) {
    // Only when the answer came from the backend that is still in use.
    const now = useStore.getState();
    if (now.baseUrl === baseUrl && now.token === token) now.setConn("unauthorized");
  }

  if (!res.ok) {
    let detail = res.statusText;
    try {
      const body = await res.json();
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      // ignore non-JSON error bodies
    }
    throw new ApiError(res.status, detail);
  }

  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}
