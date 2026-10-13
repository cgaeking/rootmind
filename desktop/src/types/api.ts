// Response shapes of the RootMind backend REST API.

export interface HealthResponse {
  status: string; // "healthy"
}

export interface RootInfo {
  name: string;
  version: string;
  status: string;
  features: string[];
  /** Whether requests need a bearer token. */
  auth: "bearer" | "none";
}

/** One entry of GET /connectors. */
export interface ConnectorInfo {
  name: string;
  type: string;
  root_path: string | null;
  exists?: boolean;
  /** false for a folder the backend could not use (see `error`). */
  available?: boolean;
  /** Why the folder could not be used, e.g. refused by the RootMind service. */
  error?: string;
}

export interface ConnectorsResponse {
  connectors: ConnectorInfo[];
}

export interface ConnectorStatus {
  connector_name: string;
  last_sync_at: string | null;
  last_full_sync_at: string | null;
  documents_total: number;
  documents_indexed: number;
  documents_error: number;
  is_syncing: boolean;
  error_message: string | null;
}

export interface StatusResponse {
  is_running: boolean;
  total_documents: number;
  indexed_documents: number;
  error_documents: number;
  connectors: Record<string, ConnectorStatus>;
}

export interface Statistics {
  total_documents: number;
  indexed_documents: number;
  error_documents: number;
  total_size_bytes: number;
  total_content_chars: number;
  file_types: Record<string, number>;
}

export interface SearchResult {
  doc_id: string;
  file_name: string;
  file_path: string;
  file_type: string;
  snippet: string;
  score: number;
  content_length?: number;
  page_count?: number;
}

export interface SearchResponse {
  query: string;
  results: SearchResult[];
  count: number;
}

export interface QuerySource {
  id?: string;
  file_name: string;
  file_path: string;
  /** Location such as "Page 2", "Slide 3" or "Sheet: Costs", when known. */
  page?: string | null;
  /** Search-only results: the text around the hit, when loaded. */
  snippet?: string;
}

/** One tool call of the agent loop; `input` is a string when it was not valid JSON. */
export interface ToolCall {
  tool: string;
  input: Record<string, unknown> | string;
}

export interface DocPathResponse {
  doc_id: string;
  abs_path: string;
}

export interface QueryResponse {
  answer: string;
  sources: QuerySource[];
  tokens_used: number;
  tool_calls: ToolCall[];
  /** Why each LLM call stopped, in order (e.g. "tool_use", "end_turn", "max_tokens"). */
  stop_reasons: string[];
}

export interface RecentDoc {
  doc_id: string;
  file_name: string;
  file_path: string;
  file_type: string;
  size_bytes: number;
  connector_name: string;
  indexed_at: string | null;
}

export interface RecentResponse {
  documents: RecentDoc[];
}

export interface ProgressResponse {
  total: number;
  processed: number;
  indexed: number;
  skipped: number;
  errors: number;
  current_file: string;
}

export interface RefreshResponse {
  status: string;
  connector: string;
}

export interface SaveConfigResponse {
  ok: boolean;
  reloaded?: boolean;
  restart_required: boolean;
}
