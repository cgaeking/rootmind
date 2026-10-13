import { useCallback, useEffect, useState } from "react";
import { inTauri, serviceInfo } from "../api/backend";
import type { ServiceInfo } from "../api/backend";
import { useStore } from "../state/store";

/**
 * service_info for the backend dialog and Settings › Service, read again when
 * the backend in use or its connection state changes. `failed` is true when
 * it could not be read (also in the browser preview, which has no service).
 */
export function useServiceInfo(): {
  info: ServiceInfo | null;
  failed: boolean;
  refresh: () => Promise<void>;
} {
  const kind = useStore((s) => s.kind);
  const conn = useStore((s) => s.conn);
  const [info, setInfo] = useState<ServiceInfo | null>(null);
  const [failed, setFailed] = useState(false);

  const refresh = useCallback(async () => {
    if (!inTauri()) {
      setFailed(true);
      return;
    }
    try {
      setInfo(await serviceInfo());
      setFailed(false);
    } catch {
      setFailed(true);
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh, kind, conn]);

  return { info, failed, refresh };
}
