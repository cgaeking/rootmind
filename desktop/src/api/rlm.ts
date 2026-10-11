import { api } from "./client";
import type {
  HealthResponse,
  RootInfo,
  StatusResponse,
  Statistics,
  SearchResponse,
  QueryResponse,
  ProgressResponse,
  RefreshResponse,
  SaveConfigResponse,
  RecentResponse,
  DocPathResponse,
} from "../types/api";
import type { AppConfigDto } from "../types/config";

// Health/root drive the connectivity badge. Keep them snappy so a hung
// backend resolves to "offline" (and shows the reconnect modal) quickly
// instead of leaving the app stuck on "Unknown".
export const getHealth = () => api<HealthResponse>("/health", undefined, { timeoutMs: 6_000 });
export const getRoot = () => api<RootInfo>("/", undefined, { timeoutMs: 6_000 });

export const getStatus = () => api<StatusResponse>("/status");
export const getStatistics = () => api<Statistics>("/statistics");
export const getProgress = () => api<ProgressResponse>("/index/progress");
export const getRecentIndexed = (limit = 20) =>
  api<RecentResponse>(`/index/recent?limit=${limit}`);

export const getDocumentPath = (docId: string) =>
  api<DocPathResponse>(`/documents/${encodeURIComponent(docId)}/path`);

export const getConfig = () => api<AppConfigDto>("/config");
export const saveConfig = (config: AppConfigDto) =>
  api<SaveConfigResponse>("/config", {
    method: "POST",
    body: JSON.stringify(config),
  });

export const search = (
  query: string,
  opts?: { file_type?: string; limit?: number; with_snippets?: boolean },
) =>
  api<SearchResponse>("/search", {
    method: "POST",
    body: JSON.stringify({ query, ...opts }),
  });

// A question runs the backend's bounded tool loop: up to 12 sequential LLM
// calls (condense, 10 loop calls, a final answer), each with its own timeout.
// That can legitimately take minutes. Bound it at 15 min (long enough for the
// whole loop, short enough that a hung backend still gives up), plus an
// optional abort signal so the user can stop it early.
export const QUERY_TIMEOUT_MS = 15 * 60_000;

/** One prior conversation turn sent for multi-turn context (no snippets). */
export interface QueryTurn {
  role: "user" | "assistant";
  text: string;
}

/** Ask a question about the indexed documents (POST /query, the agent loop). */
export const askQuestion = (
  question: string,
  opts?: { history?: QueryTurn[]; signal?: AbortSignal },
) =>
  api<QueryResponse>(
    "/query",
    {
      method: "POST",
      // Only include `history` when non-empty so a first turn behaves exactly
      // as before. The backend condenses follow-ups into a standalone
      // question and caps the history itself.
      body: JSON.stringify(
        opts?.history?.length ? { question, history: opts.history } : { question },
      ),
      signal: opts?.signal,
    },
    { timeoutMs: QUERY_TIMEOUT_MS },
  );

export const generateChatTitle = (question: string, answer?: string) =>
  api<{ title: string }>("/chat/title", {
    method: "POST",
    body: JSON.stringify({ question, answer }),
  });

export const refreshIndex = (opts?: {
  full?: boolean;
  connector_name?: string;
}) =>
  api<RefreshResponse>("/index/refresh", {
    method: "POST",
    body: JSON.stringify(opts ?? {}),
  });

export const listLlmModels = (body: { provider?: string; api_key?: string | null }) =>
  api<{ provider: string; models: string[] }>("/llm/models", {
    method: "POST",
    body: JSON.stringify(body),
  });
