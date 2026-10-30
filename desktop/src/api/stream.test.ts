import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { AbortedError, ApiError, apiEvents, parseSse, type SseEvent } from "./client";
import { askQuestionStream } from "./rlm";
import { useStore } from "../state/store";
import { jsonResponse, resetStore } from "../test/helpers";
import type { QueryStepEvent } from "../types/api";

const fetchMock = vi.fn<typeof fetch>();

beforeEach(() => {
  resetStore();
  useStore.setState({ kind: "service", baseUrl: "http://b", token: "tok", conn: "online" });
  fetchMock.mockReset();
  vi.stubGlobal("fetch", fetchMock);
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

/** A response whose body arrives in the given chunks. */
function streamResponse(chunks: string[], status = 200): Response {
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(body, { status, headers: { "Content-Type": "text/event-stream" } });
}

const ev = (event: string, data: unknown) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

const RESULT = {
  answer: "21 days.",
  sources: [],
  tokens_used: 30,
  tool_calls: [],
  stop_reasons: ["tool_use", "end_turn"],
};

describe("parseSse", () => {
  it("parses complete blocks, skips keepalives and keeps the incomplete rest", () => {
    const { events, rest } = parseSse(
      'event: start\ndata: {"max_iterations":10}\n\n: keepalive\n\nevent: step\ndata: {"st',
    );
    expect(events).toEqual([{ event: "start", data: { max_iterations: 10 } }]);
    expect(rest).toBe('event: step\ndata: {"st');
  });

  it("accepts CRLF line ends and unnamed events", () => {
    expect(parseSse('data: {"a":1}\r\n\r\n').events).toEqual([{ event: "message", data: { a: 1 } }]);
  });
});

describe("apiEvents", () => {
  it("hands every event over in order, also when chunks split them", async () => {
    const whole = ev("start", { n: 1 }) + ": keepalive\n\n" + ev("step", { n: 2 }) + ev("result", { n: 3 });
    fetchMock.mockResolvedValue(streamResponse([whole.slice(0, 17), whole.slice(17, 50), whole.slice(50)]));
    const seen: SseEvent[] = [];

    await apiEvents("/query/stream", { method: "POST", body: "{}" }, { idleTimeoutMs: 0, onEvent: (e) => seen.push(e) });

    expect(seen.map((e) => e.event)).toEqual(["start", "step", "result"]);
    expect(fetchMock.mock.calls[0][0]).toBe("http://b/query/stream");
    expect(new Headers(fetchMock.mock.calls[0][1]?.headers).get("Authorization")).toBe("Bearer tok");
  });

  it("throws the backend's error for a non-OK answer", async () => {
    fetchMock.mockResolvedValue(jsonResponse({ detail: "Field required" }, 422));
    await expect(
      apiEvents("/query/stream", { method: "POST" }, { idleTimeoutMs: 0, onEvent: () => {} }),
    ).rejects.toMatchObject({ status: 422, message: "Field required" });
  });

  it("closes the connection when the caller aborts", async () => {
    let seenSignal: AbortSignal | undefined;
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          seenSignal = init?.signal ?? undefined;
          seenSignal?.addEventListener("abort", () => reject(new DOMException("aborted", "AbortError")));
        }),
    );
    const ctrl = new AbortController();
    const pending = apiEvents("/query/stream", { method: "POST", signal: ctrl.signal }, {
      idleTimeoutMs: 0,
      onEvent: () => {},
    });
    ctrl.abort();

    await expect(pending).rejects.toBeInstanceOf(AbortedError);
    expect(seenSignal?.aborted).toBe(true);
  });

  it("gives up after the idle timeout without any data", async () => {
    vi.useFakeTimers();
    fetchMock.mockImplementation(
      (_url, init) =>
        new Promise((_resolve, reject) => {
          init?.signal?.addEventListener("abort", () => reject(new DOMException("timeout", "TimeoutError")));
        }),
    );
    const pending = apiEvents("/query/stream", { method: "POST" }, { idleTimeoutMs: 1000, onEvent: () => {} });
    const check = expect(pending).rejects.toSatisfy(
      (e: unknown) => e instanceof ApiError && !(e instanceof AbortedError) && e.status === 0,
    );
    await vi.advanceTimersByTimeAsync(1001);
    await check;
  });
});

describe("askQuestionStream", () => {
  it("reports start and steps, then resolves with the result", async () => {
    const step: QueryStepEvent = { type: "model_start", step: 1, purpose: "loop", iteration: 1 };
    fetchMock.mockResolvedValue(
      streamResponse([ev("start", { max_iterations: 10, provider: "anthropic", model: "m" }), ev("step", step), ev("result", RESULT)]),
    );
    const onStart = vi.fn();
    const onStep = vi.fn();

    const res = await askQuestionStream("PTO?", { history: [{ role: "user", text: "hi" }], onStart, onStep });

    expect(res).toEqual(RESULT);
    expect(onStart).toHaveBeenCalledWith({ max_iterations: 10, provider: "anthropic", model: "m" });
    expect(onStep).toHaveBeenCalledWith(step);
    expect(JSON.parse(String(fetchMock.mock.calls[0][1]?.body))).toEqual({
      question: "PTO?",
      history: [{ role: "user", text: "hi" }],
    });
  });

  it("throws the error event", async () => {
    fetchMock.mockResolvedValue(streamResponse([ev("error", { status: 504, detail: "LLM request timed out" })]));
    await expect(askQuestionStream("Q?")).rejects.toMatchObject({ status: 504, message: "LLM request timed out" });
  });

  it("fails clearly when the stream ends without a result", async () => {
    fetchMock.mockResolvedValue(streamResponse([ev("step", { type: "note", kind: "x" })]));
    await expect(askQuestionStream("Q?")).rejects.toMatchObject({ status: 0 });
  });

  it("falls back to POST /query on a backend without the stream endpoint", async () => {
    fetchMock
      .mockResolvedValueOnce(jsonResponse({ detail: "Not Found" }, 404))
      .mockResolvedValueOnce(jsonResponse(RESULT));

    expect(await askQuestionStream("Q?")).toEqual(RESULT);
    expect(fetchMock.mock.calls.map((c) => c[0])).toEqual(["http://b/query/stream", "http://b/query"]);
  });
});
