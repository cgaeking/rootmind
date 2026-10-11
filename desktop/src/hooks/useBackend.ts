import { useCallback, useEffect, useState } from "react";
import { getHealth, getRoot } from "../api/rlm";
import { useStore } from "../state/store";

/**
 * Tracks connectivity to the RootMind backend REST API. Re-checks on mount and
 * whenever the base URL changes. A heartbeat (see AppLayout) re-checks every 5s.
 */
export function useBackend() {
  const baseUrl = useStore((s) => s.baseUrl);
  const conn = useStore((s) => s.conn);
  const setConn = useStore((s) => s.setConn);
  const [version, setVersion] = useState<string | null>(null);

  const recheck = useCallback(async () => {
    try {
      await getHealth();
      setConn("online");
      try {
        const root = await getRoot();
        setVersion(root.version);
      } catch {
        setVersion(null);
      }
    } catch {
      setConn("offline");
      setVersion(null);
    }
  }, [setConn]);

  useEffect(() => {
    void recheck();
  }, [recheck, baseUrl]);

  return { conn, version, recheck, baseUrl };
}
