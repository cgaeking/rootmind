// Editable subset of the backend config, exchanged via GET/POST /config.

export type LlmProvider = "anthropic" | "openai" | "xai";

export interface LlmConfigDto {
  provider: LlmProvider;
  model: string;
  /** Active provider's key (mirror; the engine reads this). */
  api_key?: string | null;
  /** Per-provider API keys (canonical). */
  api_keys: Record<string, string>;
}

export interface ConnectorDto {
  name: string;
  type: string; // "local"
  path: string | null;
  // null/undefined -> inherits the global indexer patterns.
  include?: string[] | null;
  exclude?: string[] | null;
}

export interface IndexerConfigDto {
  sync_schedule: string | null;
  /** Auto re-index interval in hours (default 24). */
  sync_interval_hours: number;
  /** Global file patterns applied to every folder. */
  include: string[];
  exclude: string[];
}

export interface AppConfigDto {
  llm: LlmConfigDto;
  connectors: ConnectorDto[];
  indexer: IndexerConfigDto;
}

