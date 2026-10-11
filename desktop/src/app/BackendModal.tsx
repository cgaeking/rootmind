import { useState } from "react";
import { labels } from "../i18n/labels";
import styles from "./BackendModal.module.css";

/**
 * Blocking dialog shown when the backend REST API is unreachable. Lets the
 * user fix the backend URL and reconnect; auto-closes once the connection is
 * back (the parent only renders it while offline).
 */
export function BackendModal({
  baseUrl,
  onConnect,
}: {
  baseUrl: string;
  onConnect: (url: string) => Promise<void>;
}) {
  const [draft, setDraft] = useState(baseUrl);
  const [busy, setBusy] = useState(false);

  async function submit() {
    if (busy) return;
    setBusy(true);
    try {
      await onConnect(draft);
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className={styles.overlay}>
      <div className={styles.card}>
        <h2 className={styles.title}>{labels.backendModal.title}</h2>
        <p className={styles.text}>{labels.backendModal.text}</p>

        <label className={styles.label}>{labels.backendModal.urlLabel}</label>
        <input
          className={styles.input}
          value={draft}
          onChange={(e) => setDraft(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void submit();
          }}
          placeholder={labels.backendModal.urlPlaceholder}
          spellCheck={false}
          autoFocus
        />

        <div className={styles.actions}>
          <button className={styles.button} onClick={() => void submit()} disabled={busy}>
            {busy ? labels.backendModal.connecting : labels.backendModal.connect}
          </button>
        </div>

        <p className={styles.hint}>
          {labels.backendModal.hintBefore} <code>backend\deploy\windows\install-backend-service.bat</code>{" "}
          {labels.backendModal.hintAfter}
        </p>
      </div>
    </div>
  );
}
