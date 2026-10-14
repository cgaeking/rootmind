import { useState } from "react";
import { labels } from "../i18n/labels";
import { commandError, inTauri, serviceDetails, serviceUpdate } from "../api/backend";
import { recheck } from "../hooks/useBackend";
import { useServiceInfo } from "../hooks/useServiceInfo";
import { DEFAULT_BASE_URL, readStoredBackend, useStore } from "../state/store";
import { connectCustom, connectService, runServiceChange } from "../state/backendActions";
import { canRepair, changeMessage, repairLabels } from "../state/service";
import { Button } from "../components/ui";
import { Dialog, dialogStyles as styles } from "../components/Dialog";

/**
 * The RootMind service cannot be used: what is wrong and what to do (Set up
 * for a service that was never set up, Repair when it can help, else the
 * installer), Check again, or another backend.
 */
function ServicePanel({ onOther }: { onOther: () => void }) {
  const problem = useStore((s) => s.serviceProblem) ?? "noAnswer";
  // Linux: the set-up the app runs on its own (state/autoSetup.ts).
  const auto = useStore((s) => s.autoService);
  const { info } = useServiceInfo();
  const [localBusy, setBusy] = useState<"repair" | "check" | null>(null);
  const [localMessage, setMessage] = useState<string | null>(null);
  const [details, setDetails] = useState<string | null>(null);
  const autoRunning = auto?.status === "running";
  const busy = localBusy ?? (autoRunning ? "repair" : null);
  const message = localMessage ?? (auto?.status === "failed" ? auto.message : null);

  const repairable = canRepair(problem, info);
  // "Set up" for a service that was never set up, else "Repair".
  const repairText = repairLabels(problem);
  // Repair would help, but this account cannot approve it.
  const needsAdmin =
    !repairable && !!info && !info.canElevate && canRepair(problem, { ...info, canElevate: true });

  async function repair() {
    setBusy("repair");
    setMessage(null);
    setDetails(null);
    try {
      // The dialog stays open (no "switching") so the outcome can be shown.
      const outcome = await runServiceChange(serviceUpdate, { switching: false });
      setMessage(changeMessage(outcome)?.text ?? null);
    } finally {
      setBusy(null);
    }
  }

  async function loadDetails() {
    try {
      setDetails(await serviceDetails());
    } catch (e) {
      setDetails(labels.service.detailsFailed(commandError(e)));
    }
  }

  async function checkAgain() {
    setBusy("check");
    setMessage(null);
    try {
      await recheck();
    } finally {
      setBusy(null);
    }
  }

  return (
    <Dialog
      title={labels.service.problems[problem].title}
      actions={
        <>
          <button type="button" className={styles.link} onClick={onOther} disabled={!!busy}>
            {labels.backendModal.otherBackend}
          </button>
          <Button
            variant={repairable ? "default" : "primary"}
            onClick={() => void checkAgain()}
            disabled={!!busy}
          >
            {busy === "check" ? labels.backendModal.checking : labels.backendModal.checkAgain}
          </Button>
          {repairable && (
            <Button variant="primary" onClick={() => void repair()} disabled={!!busy}>
              {labels.service[repairText.action]}
            </Button>
          )}
        </>
      }
    >
      <p className={styles.text}>{labels.service.problems[problem].text}</p>
      {needsAdmin && <p className={styles.text}>{labels.service.repairNeedsAdmin}</p>}
      {busy === "repair" && (
        <p className={styles.text}>
          {autoRunning && !localBusy
            ? labels.service[auto?.action === "setup" ? "autoSetup" : "autoUpdate"]
            : labels.service[repairText.busy]}
        </p>
      )}
      {message && <p className={styles.text}>{message}</p>}
      {message && !busy && !details && inTauri() && (
        <button type="button" className={styles.link} onClick={() => void loadDetails()}>
          {labels.service.details}
        </button>
      )}
      {details && <pre className={styles.pre}>{details}</pre>}
    </Dialog>
  );
}

/**
 * A custom backend does not answer or needs a token: URL, token, and the way
 * back to the RootMind service (`onUseService` when the dialog was opened from
 * the service's problem, else it connects to the service).
 */
function CustomPanel({ onUseService }: { onUseService?: () => void }) {
  const conn = useStore((s) => s.conn);
  const kind = useStore((s) => s.kind);
  const baseUrl = useStore((s) => s.baseUrl);
  const token = useStore((s) => s.token);
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

  function backToService() {
    if (onUseService) onUseService();
    else void connectService();
  }

  return (
    <Dialog
      title={unauthorized ? labels.backendModal.unauthorizedTitle : labels.backendModal.customTitle}
      actions={
        <>
          {/* The browser preview has no service. */}
          {inTauri() && (
            <Button onClick={backToService} disabled={busy}>
              {labels.backendModal.useService}
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
 * rejects the token). For the RootMind service it explains the problem and
 * what to do; for a custom backend it asks for the address and token. It
 * closes by itself once the connection is back (the parent renders it only
 * then).
 */
export function BackendModal() {
  const kind = useStore((s) => s.kind);
  const [other, setOther] = useState(false);
  if (kind === "service" && !other) return <ServicePanel onOther={() => setOther(true)} />;
  return <CustomPanel onUseService={kind === "service" ? () => setOther(false) : undefined} />;
}
