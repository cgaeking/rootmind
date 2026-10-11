import type { QuerySource } from "./api";

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
