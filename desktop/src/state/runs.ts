import { create } from "zustand";
import { askQuestionStream, generateChatTitle, search } from "../api/rlm";
import { AbortedError, ApiError } from "../api/client";
import { labels } from "../i18n/labels";
import type { QuerySource, SearchResult } from "../types/api";
import type { AgentStep, Conversation, StoredMessage } from "../types/chat";
import { applyStepEvent, countCalls, finishOpenSteps } from "./agentSteps";
import { useChatStore } from "./chatStore";
import { useStore } from "./store";

// The requests in flight, one per conversation at most. They live here and not
// in the chat screen, so a question keeps running, and its steps keep coming
// in, while the user opens Settings or another conversation; several
// conversations can ask at the same time. Each answer is written to the
// conversation it was asked in.

export const SEARCH_PAGE = 100;
// How many recent turns to send for multi-turn context (the backend caps it too).
const HISTORY_TURNS = 6;
// The backend's iteration limit, until a question's start event says otherwise.
export const DEFAULT_MAX_ITERATIONS = 10;

/** A request in flight in one conversation. */
export interface Run {
  convId: string;
  /** A question to the agent (with steps and Stop), not a plain search. */
  question: boolean;
  startedAt: number;
  steps: AgentStep[];
  maxIterations: number;
  abort: AbortController;
}

interface RunsStore {
  runs: Record<string, Run>;
  /** Conversations whose answer came in while another one was on screen. */
  unread: Record<string, true>;
  markRead: (convId: string) => void;
}

export const useRuns = create<RunsStore>((set) => ({
  runs: {},
  unread: {},
  markRead: (convId) =>
    set((s) => {
      if (!s.unread[convId]) return s;
      const unread = { ...s.unread };
      delete unread[convId];
      return { unread };
    }),
}));

/** The client clock, for step start times and a question's duration. */
function clockNow(): number {
  return Date.now();
}

function patchRun(convId: string, fn: (run: Run) => Run) {
  useRuns.setState((s) => {
    const run = s.runs[convId];
    return run ? { runs: { ...s.runs, [convId]: fn(run) } } : s;
  });
}

/** Pause the connection heartbeat while any question runs, so a busy backend isn't read as offline. */
function updateBusy() {
  const busy = Object.values(useRuns.getState().runs).some((r) => r.question);
  useStore.getState().setBusy(busy);
}

export function toSources(results: SearchResult[]): QuerySource[] {
  return results.map((r) => ({
    id: r.doc_id,
    file_name: r.file_name,
    file_path: r.file_path,
    // Strip FTS highlight markers (**match**); undefined when no snippet was loaded.
    snippet: r.snippet ? r.snippet.replace(/\*\*/g, "").trim() : undefined,
  }));
}

export function isRunning(convId: string | undefined): boolean {
  return !!convId && convId in useRuns.getState().runs;
}

/** Stop the question in flight in `convId`, in the app and in the service. */
export function cancelRun(convId: string) {
  useRuns.getState().runs[convId]?.abort.abort();
}

/**
 * Ask `q` in conversation `conv`, whose last message is the question itself,
 * and write the answer (or the error or cancel note) there when it arrives.
 * Returns when the request has ended.
 */
export async function runRequest(conv: Conversation, q: string, isQuestion: boolean): Promise<void> {
  const convId = conv.id;
  if (isRunning(convId)) return;
  const earlier = conv.messages.slice(0, -1);
  const assistantId = conv.messages[conv.messages.length - 1].id + 1;
  const startedAt = clockNow();
  const abort = new AbortController();
  useRuns.setState((s) => ({
    runs: {
      ...s.runs,
      [convId]: {
        convId,
        question: isQuestion,
        startedAt,
        steps: [],
        maxIterations: DEFAULT_MAX_ITERATIONS,
        abort,
      },
    },
  }));
  updateBusy();

  const current = () => useRuns.getState().runs[convId];
  // The steps to keep with the answer (or with the error or cancel note).
  const withSteps = (message: StoredMessage): StoredMessage => {
    const run = current();
    const steps = finishOpenSteps(run?.steps ?? [], clockNow());
    return steps.length > 0
      ? { ...message, steps, elapsedMs: clockNow() - startedAt, maxIterations: run.maxIterations }
      : message;
  };

  let assistant: StoredMessage;
  try {
    if (!isQuestion) {
      const res = await search(q, { limit: SEARCH_PAGE });
      assistant =
        res.results.length === 0
          ? { id: assistantId, role: "assistant", text: labels.chat.noResults }
          : {
              // Fast result list (no snippets); descriptions + more pages load on demand.
              id: assistantId,
              role: "assistant",
              text: "",
              sources: toSources(res.results),
              searchMode: true,
              searchQuery: q,
              searchLimit: SEARCH_PAGE,
            };
    } else {
      // Recent turns for multi-turn context: text only (no source lists or
      // error bubbles). The backend caps and condenses.
      const history = earlier
        .filter((m) => m.text.trim() && !m.error)
        .slice(-HISTORY_TURNS)
        .map((m) => ({ role: m.role, text: m.text }));
      const res = await askQuestionStream(q, {
        history,
        signal: abort.signal,
        onStart: (start) => patchRun(convId, (r) => ({ ...r, maxIterations: start.max_iterations })),
        onStep: (event) =>
          patchRun(convId, (r) => ({ ...r, steps: applyStepEvent(r.steps, event, clockNow()) })),
      });
      assistant = withSteps({
        id: assistantId,
        role: "assistant",
        text: res.answer,
        sources: res.sources,
        md: true,
      });
    }
  } catch (e) {
    const calls = countCalls(current()?.steps ?? []);
    assistant = withSteps(
      e instanceof AbortedError
        ? {
            id: assistantId,
            role: "assistant",
            text: calls > 0 ? labels.chat.cancelledAfter(calls) : labels.chat.cancelled,
            error: true,
          }
        : {
            id: assistantId,
            role: "assistant",
            text: labels.chat.errorPrefix(e instanceof ApiError ? e.message : String(e)),
            error: true,
          },
    );
  }

  try {
    await useChatStore.getState().updateConversation(convId, (msgs) => [...msgs, assistant]);
  } finally {
    useRuns.setState((s) => {
      const runs = { ...s.runs };
      delete runs[convId];
      // Answered while another conversation (or another screen's chat) was open.
      const onScreen = useChatStore.getState().active?.id === convId;
      return { runs, unread: onScreen ? s.unread : { ...s.unread, [convId]: true } };
    });
    updateBusy();
  }

  // Refine the auto-title via the LLM after the first real exchange.
  if (earlier.length === 0 && isQuestion && !assistant.error) {
    void refineTitle(convId, q, assistant.text);
  }
}

async function refineTitle(id: string, question: string, answer: string) {
  try {
    const { title } = await generateChatTitle(question, answer);
    if (title) await useChatStore.getState().setTitle(id, title);
  } catch {
    /* keep the derived title */
  }
}
