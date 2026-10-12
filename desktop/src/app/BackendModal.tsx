import { useState } from "react";
import { labels } from "../i18n/labels";
import { backendLogDir, commandError, serviceUpdate } from "../api/backend";
import type { SidecarStatus } from "../api/backend";
import { DEFAULT_BASE_URL, readStoredBackend, useStore } from "../state/store";
import {
  connectCustom,
  restartBuiltin,
  runServiceChange,
  startBuiltin,
} from "../state/backendActions";
import { serviceResultKey } from "../state/service";
import { Button } from "../components/ui";
import { Dialog, dialogStyles as styles } from "../components/Dialog";

/** How many log lines the built-in backend dialog shows. */
const LOG_LINES = 12;

function sidecarMessage(st: SidecarStatus | null): string {
  const m = labels.backendModal.sidecarErrors;
  switch (st?.error) {
    case "missing":
      return m.missing;
    case "blocked":
      return m.blocked;
    case "access_denied":
      return m.accessDenied;
    case "exited":
      return st.exitCode !== null ? m.exitedWithCode(st.exitCode) : m.exited;
    case "timeout":
      return m.timeout;
    default:
      return m.unknown;
  }
}

async function showLog(): Promise<string | null> {
  try {
    const dir = await backendLogDir();
    const { revealItemInDir } = await import("@tauri-apps/plugin-opener");
    await revealItemInDir(dir);
    return null;
  } catch (e) {
    return labels.backendModal.showLogFailed(commandError(e));
  }
}

/** The built-in backend is not running: Restart, Show log. */
function BuiltinPanel({ onOther }: { onOther: () => void }) {
  const sidecar = useStore((s) => s.sidecar);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const tail = (sidecar?.logTail ?? []).slice(-LOG_LINES);

  async function restart() {
    setBusy(true);
    setError(null);
    try {
      await restartBuiltin();
    } finally {
      setBusy(false);
    }
  }

  async function openLog() {
    setError(await showLog());
  }

  return (
    <Dialog
      title={labels.backendModal.builtinTitle}
      actions={
        <>
          <button type="button" className={styles.link} onClick={onOther} disabled={busy}>
            {labels.backendModal.otherBackend}
          </button>
          <Button onClick={() => void openLog()}>{labels.backendModal.showLog}</Button>
          <Button variant="primary" onClick={() => void restart()} disabled={busy}>
            {busy ? labels.backendModal.restarting : labels.backendModal.restart}
          </Button>
        </>
      }
    >
      <p className={styles.text}>{labels.backendModal.builtinText}</p>
      <p className={styles.text}>{sidecarMessage(sidecar)}</p>
      {tail.length > 0 && (
        <div className={styles.field}>
          <span className={styles.label}>{labels.backendModal.logTail}</span>
          <pre className={styles.log}>{tail.join("\n")}</pre>
        </div>
      )}
      {error && <p className={styles.text}>{error}</p>}
    </Dialog>
  );
}

/** The background service does not answer: Repair, Use built-in for now. */
function ServicePanel() {
  const issue = useStore((s) => s.serviceIssue);
  const builtinAvailable = useStore((s) => s.appMode?.builtinAvailable ?? false);
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState<string | null>(null);

  const issues = labels.backendModal.serviceIssues;
  const text =
    issue === "not_installed"
      ? issues.notInstalled
      : issue === "not_running"
        ? issues.notRunning
        : issue === "listener_mismatch"
          ? issues.listenerMismatch
          : issue === "token_unreadable"
            ? issues.tokenUnreadable
            : issues.noAnswer;

  async function repair() {
    setBusy(true);
    setMessage(null);
    try {
      const outcome = await runServiceChange(serviceUpdate, {
        successKind: "service",
        fallbackKind: "service",
        switching: false,
      });
      if (outcome.kind === "error") {
        setMessage(labels.settings.serviceFailed(outcome.message));
      } else if (!outcome.result.ok) {
        setMessage(labels.settings.serviceCodes[serviceResultKey(outcome.result)]);
      }
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={labels.backendModal.serviceTitle}
      actions={
        <>
          {builtinAvailable && (
            <Button onClick={() => void startBuiltin()} disabled={busy}>
              {labels.backendModal.useBuiltinForNow}
            </Button>
          )}
          <Button variant="primary" onClick={() => void repair()} disabled={busy}>
            {busy ? labels.backendModal.repairing : labels.backendModal.repair}
          </Button>
        </>
      }
    >
      <p className={styles.text}>{text}</p>
      {message && <p className={styles.text}>{message}</p>}
    </Dialog>
  );
}

/** A custom backend does not answer or needs a token: URL, token, Use built-in backend. */
function CustomPanel() {
  const conn = useStore((s) => s.conn);
  const kind = useStore((s) => s.kind);
  const baseUrl = useStore((s) => s.baseUrl);
  const token = useStore((s) => s.token);
  const builtinAvailable = useStore((s) => s.appMode?.builtinAvailable ?? false);
  // A development build without a backend URL yet suggests the stored one.
  const [url, setUrl] = useState(
    kind === "custom" && baseUrl ? baseUrl : readStoredBackend().baseUrl || DEFAULT_BASE_URL,
  );
  const [tokenDraft, setTokenDraft] = useState(kind === "custom" ? (token ?? "") : "");
  const [busy, setBusy] = useState(false);

  const unauthorized = conn === "unauthorized";

  async function submit() {
    if (busy) return;
    setBusy(true);
    try {
      await connectCustom(url, tokenDraft);
    } finally {
      setBusy(false);
    }
  }

  return (
    <Dialog
      title={unauthorized ? labels.backendModal.unauthorizedTitle : labels.backendModal.customTitle}
      actions={
        <>
          {builtinAvailable && (
            <Button onClick={() => void startBuiltin()} disabled={busy}>
              {labels.backendModal.useBuiltin}
            </Button>
          )}
          <Button variant="primary" onClick={() => void submit()} disabled={busy}>
            {busy ? labels.backendModal.connecting : labels.backendModal.connect}
          </Button>
        </>
      }
    >
      <p className={styles.text}>
        {unauthorized ? labels.backendModal.unauthorizedText : labels.backendModal.customText}
      </p>
      <label className={styles.field}>
        <span className={styles.label}>{labels.backendModal.urlLabel}</span>
        <input
          className={styles.input}
          value={url}
          onChange={(e) => setUrl(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void submit();
          }}
          placeholder={labels.backendModal.urlPlaceholder}
          spellCheck={false}
          autoFocus={!unauthorized}
        />
      </label>
      <label className={styles.field}>
        <span className={styles.label}>{labels.backendModal.tokenLabel}</span>
        <input
          className={styles.input}
          type="password"
          value={tokenDraft}
          onChange={(e) => setTokenDraft(e.currentTarget.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void submit();
          }}
          spellCheck={false}
          autoComplete="off"
          autoFocus={unauthorized}
        />
      </label>
    </Dialog>
  );
}

/**
 * Blocking dialog while the backend is unreachable (or a custom backend
 * rejects the token). What it offers depends on the kind of backend; it closes
 * by itself once the connection is back (the parent renders it only then).
 */
export function BackendModal() {
  const kind = useStore((s) => s.kind);
  const [other, setOther] = useState(false);
  if (kind === "custom" || other) return <CustomPanel />;
  if (kind === "service") return <ServicePanel />;
  return <BuiltinPanel onOther={() => setOther(true)} />;
}
