import { labels } from "../i18n/labels";
import { useStore } from "../state/store";
import { Banner } from "./ui";

/** The connection notice at the top of a screen; nothing while connected. */
export function ConnBanner() {
  const conn = useStore((s) => s.conn);
  const kind = useStore((s) => s.kind);
  const problem = useStore((s) => s.serviceProblem);

  switch (conn) {
    case "switching":
      return <Banner tone="info">{labels.common.backendSwitching}</Banner>;
    case "offline":
      return (
        <Banner tone="err">
          {kind === "service" && problem
            ? labels.service.problems[problem].title
            : labels.common.backendUnreachable}
        </Banner>
      );
    case "unauthorized":
      return <Banner tone="err">{labels.common.backendUnauthorized}</Banner>;
    default:
      return null;
  }
}
