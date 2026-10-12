import { labels } from "../i18n/labels";
import type { FirstRunChoice as Choice } from "../state/bootstrap";
import { Button } from "../components/ui";
import { Dialog, dialogStyles as styles } from "../components/Dialog";

/**
 * Asked once, on the first start of this version, when an unauthenticated
 * RootMind server (typically one set up for version 1.1) answers on `url`.
 * The answer is remembered.
 */
export function FirstRunChoice({ url, onAnswer }: { url: string; onAnswer: (c: Choice) => void }) {
  return (
    <Dialog
      title={labels.firstRun.title}
      actions={
        <>
          <Button onClick={() => onAnswer("builtin")}>{labels.firstRun.useBuiltin}</Button>
          <Button variant="primary" onClick={() => onAnswer("custom")}>
            {labels.firstRun.useServer}
          </Button>
        </>
      }
    >
      <p className={styles.text}>{labels.firstRun.text(url)}</p>
    </Dialog>
  );
}
