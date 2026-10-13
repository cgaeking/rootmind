// Settings › LLM: which provider an API key belongs to.

import type { LlmProvider } from "../types/config";

export const PROVIDERS: LlmProvider[] = ["anthropic", "openai", "xai"];

/** Display names of the providers. */
export const PROVIDER_NAMES: Record<LlmProvider, string> = {
  anthropic: "Anthropic",
  openai: "OpenAI",
  xai: "xAI",
};

/**
 * The provider a key belongs to, judged by its prefix: `sk-ant-` is Anthropic,
 * `xai-` is xAI, any other `sk-` (including `sk-proj-`) is OpenAI. Null when
 * the format says nothing (the user then picks the provider).
 */
export function detectProvider(key: string): LlmProvider | null {
  const k = key.trim();
  if (k.startsWith("sk-ant-")) return "anthropic";
  if (k.startsWith("xai-")) return "xai";
  if (k.startsWith("sk-")) return "openai";
  return null;
}

/**
 * The display mask of a saved key, the same as the backend's `mask_secret`
 * (backend/src/rootmind/config.py): an ellipsis and the last four characters,
 * or just the ellipsis for a key of eight characters or fewer. Used only when
 * the backend's own masks cannot be read after a save.
 */
export function maskKey(key: string): string {
  if (!key) return "";
  return key.length <= 8 ? "…" : `…${key.slice(-4)}`;
}
