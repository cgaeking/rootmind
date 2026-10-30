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
 * the app fetches a fresh token (service) or asks for one (custom).
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

/** One server-sent event: its name (`message` when the block names none) and parsed JSON data. */
export interface SseEvent {
  event: string;
  data: unknown;
}

/**
 * Split `buffer` into complete server-sent events and the incomplete rest.
 * Comment lines (keepalives, starting with ":") produce no event. Blocks are
 * separated by a blank line; `data` is parsed as JSON.
 */
export function parseSse(buffer: string): { events: SseEvent[]; rest: string } {
  const normalized = buffer.replace(/\r\n/g, "\n");
  const blocks = normalized.split("\n\n");
  const rest = blocks.pop() ?? "";
  const events: SseEvent[] = [];
  for (const block of blocks) {
    let event = "message";
    const data: string[] = [];
    for (const line of block.split("\n")) {
      if (!line || line.startsWith(":")) continue;
      const colon = line.indexOf(":");
      const field = colon === -1 ? line : line.slice(0, colon);
      const value = colon === -1 ? "" : line.slice(colon + 1).replace(/^ /, "");
      if (field === "event") event = value;
      else if (field === "data") data.push(value);
    }
    if (data.length > 0) events.push({ event, data: JSON.parse(data.join("\n")) });
  }
  return { events, rest };
}

export interface EventStreamOptions {
  /**
   * Give up when nothing (not even a keepalive comment) arrives for this
   * long, in ms. Pass 0 to disable.
   */
  idleTimeoutMs: number;
  /** Called for every event, in order. */
  onEvent: (event: SseEvent) => void;
}

/**
 * POST to a server-sent events endpoint of the backend and hand every event
 * to `onEvent` until the stream ends. Headers, 401 handling and error bodies
 * work like {@link api}. There is no overall timeout: a stream may run as long
 * as the backend keeps sending, and gives up only after `idleTimeoutMs` of
 * silence. Aborting `init.signal` closes the connection (AbortedError).
 */
export async function apiEvents(
  path: string,
  init: RequestInit,
  opts: EventStreamOptions,
): Promise<void> {
  const { baseUrl, token } = useStore.getState();
  const idleCtrl = new AbortController();
  let timer: ReturnType<typeof setTimeout> | undefined;
  const touch = () => {
    if (timer) clearTimeout(timer);
    if (opts.idleTimeoutMs > 0) {
      timer = setTimeout(
        () => idleCtrl.abort(new DOMException("timeout", "TimeoutError")),
        opts.idleTimeoutMs,
      );
    }
  };
  const signal = combineSignals(init.signal ?? undefined, idleCtrl.signal);
  const failed = (e: unknown): ApiError => {
    const name = (e as Error).name;
    if (name === "AbortError" || name === "TimeoutError") {
      if (init.signal?.aborted) return new AbortedError();
      return new ApiError(0, labels.common.timeoutAfter(Math.round(opts.idleTimeoutMs / 1000)));
    }
    return new ApiError(0, (e as Error).message || labels.common.connectionFailed);
  };

  touch();
  try {
    let res: Response;
    try {
      res = await fetch(`${baseUrl}${path}`, {
        cache: "no-store",
        ...init,
        headers: requestHeaders(token, init.headers),
        signal,
      });
    } catch (e) {
      throw failed(e);
    }

    if (res.status === 401) {
      const now = useStore.getState();
      if (now.baseUrl === baseUrl && now.token === token) now.setConn("unauthorized");
    }
    if (!res.ok || !res.body) {
      let detail = res.statusText;
      try {
        const body = await res.json();
        if (body && typeof body.detail === "string") detail = body.detail;
      } catch {
        // ignore non-JSON error bodies
      }
      throw new ApiError(res.status, detail);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      let chunk: ReadableStreamReadResult<Uint8Array>;
      try {
        chunk = await reader.read();
      } catch (e) {
        throw failed(e);
      }
      if (chunk.done) break;
      touch();
      buffer += decoder.decode(chunk.value, { stream: true });
      const parsed = parseSse(buffer);
      buffer = parsed.rest;
      for (const event of parsed.events) opts.onEvent(event);
    }
    buffer += decoder.decode();
    for (const event of parseSse(buffer + "\n\n").events) opts.onEvent(event);
  } finally {
    if (timer) clearTimeout(timer);
  }
}
