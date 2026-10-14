import { useEffect } from "react";
import { checkAutoSetup } from "../state/autoSetup";
import { useStore } from "../state/store";

/**
 * Linux: set the RootMind service up on the first start, and update it when
 * it is older than the app, without a click (state/autoSetup.ts). Checked
 * whenever the connection, the problem or the service's version changes.
 * AppLayout only.
 */
export function useAutoServiceSetup(): void {
  const platform = useStore((s) => s.appMode?.platform ?? null);
  const kind = useStore((s) => s.kind);
  const conn = useStore((s) => s.conn);
  const problem = useStore((s) => s.serviceProblem);
  const version = useStore((s) => s.version);

  useEffect(() => {
    if (platform !== "linux") return;
    void checkAutoSetup(null);
  }, [platform, kind, conn, problem, version]);
}
