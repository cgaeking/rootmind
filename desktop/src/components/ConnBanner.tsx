import { useEffect, useState } from "react";
import { labels } from "../i18n/labels";
import { useStore } from "../state/store";
import { Banner } from "./ui";

/** After this long in "starting", explain why the first start can be slow. */
const SLOW_START_MS = 10_000;

/** The connection notice at the top of a screen; nothing while connected. */
export function ConnBanner() {
  const conn = useStore((s) => s.conn);
  const since = useStore((s) => s.connSince);
  const [slow, setSlow] = useState(false);

  useEffect(() => {
    setSlow(false);
    if (conn !== "starting") return;
    const left = SLOW_START_MS - (Date.now() - since);
    if (left <= 0) {
      setSlow(true);
      return;
    }
    const timer = setTimeout(() => setSlow(true), left);
    return () => clearTimeout(timer);
  }, [conn, since]);

  switch (conn) {
    case "starting":
      return (
        <Banner tone="info">
          {labels.common.backendStarting}
          {slow && <> {labels.common.backendStartingSlow}</>}
        </Banner>
      );
    case "switching":
      return <Banner tone="info">{labels.common.backendSwitching}</Banner>;
    case "offline":
      return <Banner tone="err">{labels.common.backendUnreachable}</Banner>;
    case "unauthorized":
      return <Banner tone="err">{labels.common.backendUnauthorized}</Banner>;
    default:
      return null;
  }
}
