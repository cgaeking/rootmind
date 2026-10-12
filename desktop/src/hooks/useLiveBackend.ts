import { useStore } from "../state/store";

/**
 * The base URL of the backend while it is reachable, else null.
 *
 * Screens that load data from the backend key their effects on this value:
 * when it changes (another backend, or the connection dropped) they reset
 * what they loaded, so data from one backend is never shown for, or saved to,
 * another one.
 */
export function useLiveBackend(): string | null {
  return useStore((s) => (s.conn === "online" && s.baseUrl ? s.baseUrl : null));
}

/**
 * The save guard: true when data loaded from `loadedFrom` may be saved, i.e.
 * that backend is still the one in use.
 */
export function isCurrentBackend(loadedFrom: string | null): boolean {
  return loadedFrom !== null && useStore.getState().baseUrl === loadedFrom;
}
