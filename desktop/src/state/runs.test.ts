import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

vi.mock("../api/rlm", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../api/rlm")>()),
  askQuestionStream: vi.fn(),
  search: vi.fn(),
  generateChatTitle: vi.fn(),
}));

import * as rlm from "../api/rlm";
import type { AskStreamOptions } from "../api/rlm";
import { AbortedError } from "../api/client";
import { useLanguageStore } from "../i18n/labels";
import { resetStore } from "../test/helpers";
import type { QueryResponse } from "../types/api";
import type { Conversation } from "../types/chat";
import { useChatStore } from "./chatStore";
import { cancelRun, runRequest, useRuns } from "./runs";
import { useStore } from "./store";

/** A question in flight in the fake backend: its options, and how to end it. */
interface Pending {
  opts: AskStreamOptions;
  answer: (text: string) => void;
}

const asked = new Map<string, Pending>();

function result(answer: string): QueryResponse {
  return { answer, sources: [], tokens_used: 1, tool_calls: [], stop_reasons: ["end_turn"] };
}

beforeEach(() => {
  resetStore();
  useLanguageStore.getState().setPref("en");
  useChatStore.setState({ list: [], active: null });
  useRuns.setState({ runs: {}, unread: {} });
  asked.clear();
  vi.mocked(rlm.generateChatTitle).mockResolvedValue({ title: "" });
  vi.mocked(rlm.askQuestionStream).mockImplementation(
    (question, opts = {}) =>
      new Promise<QueryResponse>((resolve, reject) => {
        opts.signal?.addEventListener("abort", () => reject(new AbortedError()));
        asked.set(question, { opts, answer: (text) => resolve(result(text)) });
      }),
  );
});

afterEach(() => {
  vi.mocked(rlm.askQuestionStream).mockReset();
});

/** A new conversation whose last message is the question `q`; it becomes the active one. */
async function ask(q: string): Promise<{ conv: Conversation; done: Promise<void> }> {
  const conv = await useChatStore.getState().startConversation([{ id: 1, role: "user", text: q }]);
  const done = runRequest(conv, q, true);
  await vi.waitFor(() => expect(asked.has(q)).toBe(true));
  return { conv, done };
}

async function messages(convId: string) {
  await useChatStore.getState().open(convId);
  return useChatStore.getState().active!.messages;
}

describe("runRequest", () => {
  it("runs questions in two conversations at once, each with its own steps and answer", async () => {
    const a = await ask("A?");
    const b = await ask("B?");
    expect(Object.keys(useRuns.getState().runs).sort()).toEqual([a.conv.id, b.conv.id].sort());
    expect(useStore.getState().busy).toBe(true);

    asked.get("A?")!.opts.onStep?.({ type: "model_start", step: 1, purpose: "loop", iteration: 1 });
    asked.get("B?")!.opts.onStep?.({
      type: "tool_start",
      step: 1,
      iteration: 1,
      name: "search_documents",
      arguments: { query: "b" },
    });
    expect(useRuns.getState().runs[a.conv.id].steps.map((s) => s.kind)).toEqual(["model"]);
    expect(useRuns.getState().runs[b.conv.id].steps.map((s) => s.name)).toEqual(["search_documents"]);

    asked.get("B?")!.answer("Answer B");
    await b.done;
    expect(useRuns.getState().runs[b.conv.id]).toBeUndefined();
    expect(useRuns.getState().runs[a.conv.id]).toBeDefined();
    expect(useStore.getState().busy).toBe(true); // A still runs

    asked.get("A?")!.answer("Answer A");
    await a.done;
    expect(useStore.getState().busy).toBe(false);
    expect((await messages(a.conv.id)).map((m) => m.text)).toEqual(["A?", "Answer A"]);
    const answerB = (await messages(b.conv.id))[1];
    expect(answerB.text).toBe("Answer B");
    expect(answerB.steps?.map((s) => s.name)).toEqual(["search_documents"]);
  });

  it("stops only the conversation whose run is cancelled", async () => {
    const a = await ask("A?");
    const b = await ask("B?");
    asked.get("A?")!.opts.onStep?.({ type: "model_start", step: 1, purpose: "loop", iteration: 1 });

    cancelRun(a.conv.id);
    await a.done;
    expect(asked.get("A?")!.opts.signal?.aborted).toBe(true);
    expect(asked.get("B?")!.opts.signal?.aborted).toBe(false);
    const note = (await messages(a.conv.id))[1];
    expect(note).toMatchObject({ error: true, text: "Cancelled after 1 step." });
    expect(note.steps?.[0]).toMatchObject({ interrupted: true });

    asked.get("B?")!.answer("Answer B");
    await b.done;
    expect((await messages(b.conv.id))[1].text).toBe("Answer B");
  });

  it("marks an answer that came in while another conversation was open as unread", async () => {
    const a = await ask("A?");
    const b = await ask("B?"); // B is now on screen
    asked.get("A?")!.answer("Answer A");
    await a.done;
    asked.get("B?")!.answer("Answer B");
    await b.done;
    expect(useRuns.getState().unread).toEqual({ [a.conv.id]: true });

    useRuns.getState().markRead(a.conv.id);
    expect(useRuns.getState().unread).toEqual({});
  });

  it("does not start a second request in a conversation that already runs one", async () => {
    const a = await ask("A?");
    await runRequest(a.conv, "A?", true);
    expect(rlm.askQuestionStream).toHaveBeenCalledTimes(1);
    asked.get("A?")!.answer("Answer A");
    await a.done;
  });

  it("writes a plain search's hits without steps or busy", async () => {
    vi.mocked(rlm.search).mockResolvedValue({
      results: [{ doc_id: "d1", file_name: "a.pdf", file_path: "/a.pdf", snippet: "**x** y" }],
    } as Awaited<ReturnType<typeof rlm.search>>);
    const conv = await useChatStore.getState().startConversation([{ id: 1, role: "user", text: "x" }]);
    const done = runRequest(conv, "x", false);
    expect(useRuns.getState().runs[conv.id]).toMatchObject({ question: false });
    expect(useStore.getState().busy).toBe(false);
    await done;
    const hit = (await messages(conv.id))[1];
    expect(hit).toMatchObject({ searchMode: true, searchQuery: "x" });
    expect(hit.sources?.[0]).toMatchObject({ file_name: "a.pdf", snippet: "x y" });
  });
});
