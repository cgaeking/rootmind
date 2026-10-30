import type { QuerySource, StepRead } from "./api";

export type Role = "user" | "assistant";

export interface StoredMessage {
  id: number;
  role: Role;
  text: string;
  sources?: QuerySource[];
  error?: boolean;
  /** Render the text as markdown (LLM answers only). */
  md?: boolean;
  /** Search-only result list: show all sources, not collapsed. */
  searchMode?: boolean;
  /** The search query, so more hits / descriptions can be loaded on demand. */
  searchQuery?: string;
  /** How many hits were requested for the current list (grows by page on "load more"). */
  searchLimit?: number;
  /** True once content snippets ("descriptions") have been loaded for the list. */
  descriptionsLoaded?: boolean;
  /** The agent's steps for this answer, shown collapsed below it. */
  steps?: AgentStep[];
  /** How long the question took, in ms (with `steps`). */
  elapsedMs?: number;
  /** The loop's iteration limit when the question ran (with `steps`). */
  maxIterations?: number;
}

/** One step of the agent loop as the chat shows it (and keeps with the answer). */
export interface AgentStep {
  step: number;
  kind: "model" | "tool" | "note";
  /** Model calls: "loop", "condense" or "fallback". */
  purpose?: string | null;
  iteration?: number | null;
  /** Tool calls: the tool and its arguments. */
  name?: string;
  arguments?: Record<string, unknown> | string;
  /** Notes: what the loop decided (for example "iteration_limit"). */
  note?: string;
  /** When the step started, in ms on the client's clock (for the running timer). */
  startedAt: number;
  /** Set when the step ended. */
  durationMs?: number;
  hits?: number;
  files?: string[];
  reads?: StepRead[];
  skipped?: string;
  error?: string;
  /** The question ended (stopped or failed) while this step was running. */
  interrupted?: boolean;
}

export interface Conversation {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  messages: StoredMessage[];
}

export interface ConversationMeta {
  id: string;
  title: string;
  createdAt: number;
  updatedAt: number;
  messageCount: number;
}
