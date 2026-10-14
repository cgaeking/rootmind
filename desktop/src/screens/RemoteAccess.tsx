import { useCallback, useEffect, useState } from "react";
import { labels } from "../i18n/labels";
import { getRemoteAccess, updateRemoteAccess } from "../api/rlm";
import type { RemoteAccessChange, RemoteAccessInfo } from "../types/api";
import {
  REMOTE_DOCS_URL,
  REMOTE_POLL_MS,
  canTurnOn,
  publicEndpoint,
  settingsChange,
} from "../state/remoteAccess";
import { openExternal } from "../utils/openExternal";
import { Dialog, dialogStyles } from "../components/Dialog";
import { Banner, Button, Row, TextInput } from "../components/ui";
import styles from "./ServiceSection.module.css";

function message(e: unknown): string {
  return e instanceof Error && e.message ? e.message : String(e);
}

/**
 * Settings › RootMind service › Remote access (ngrok): the switch, the
 * write-only authtoken, the optional reserved domain and the tunnel's status.
 * The RootMind service runs the tunnel (in its MCP over HTTP service); this
 * block only changes the settings through the REST API and polls the status
 * while remote access is on. Turning it on needs a confirmation. Disabled
 * while MCP over HTTP is off.
 */
export function RemoteAccess({ mcpEnabled }: { mcpEnabled: boolean }) {
  const [info, setInfo] = useState<RemoteAccessInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  // The authtoken lives only in this field until it is saved; it is never read back.
  const [authtoken, setAuthtoken] = useState("");
  const [replacing, setReplacing] = useState(false);
  // null: not edited, the saved domain is shown.
  const [domain, setDomain] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [copied, setCopied] = useState(false);

  const load = useCallback(async () => {
    try {
      setInfo(await getRemoteAccess());
      setLoadError(null);
    } catch (e) {
      setLoadError(labels.remote.loadFailed(message(e)));
    }
  }, []);

  useEffect(() => {
    void load();
  }, [load]);

  const enabled = info?.enabled ?? false;
  useEffect(() => {
    if (!enabled) return;
    const timer = setInterval(() => void load(), REMOTE_POLL_MS);
    return () => clearInterval(timer);
  }, [enabled, load]);

  async function change(c: RemoteAccessChange): Promise<boolean> {
    setBusy(true);
    setError(null);
    setNotice(null);
    try {
      setInfo(await updateRemoteAccess(c));
      return true;
    } catch (e) {
      setError(labels.remote.failed(message(e)));
      return false;
    } finally {
      setBusy(false);
    }
  }

  async function save() {
    if (!info) return;
    const c = settingsChange(info, authtoken, domain);
    if (!c) return;
    if (await change(c)) {
      setAuthtoken("");
      setReplacing(false);
      setDomain(null);
      setNotice(labels.remote.saved);
    }
  }

  async function copyEndpoint(url: string) {
    try {
      await navigator.clipboard.writeText(publicEndpoint(url));
    } catch {
      return;
    }
    setCopied(true);
    setTimeout(() => setCopied(false), 1500);
  }

  const disabled = !mcpEnabled || busy;
  const showTokenInput = !!info && (!info.authtoken_saved || replacing);
  const dirty = !!info && settingsChange(info, authtoken, domain) !== null;
  const status = info?.status;

  return (
    <div className={styles.remote}>
      <div className={styles.field}>
        <span className={styles.label}>{labels.remote.title}</span>
        <span className={styles.hint}>{labels.remote.hint}</span>
      </div>
      {!mcpEnabled && <p className={styles.note}>{labels.remote.needsMcp}</p>}
      {loadError && <Banner tone="err">{loadError}</Banner>}

      {info && (
        <>
          <label className={styles.toggle}>
            <input
              type="checkbox"
              checked={info.enabled}
              disabled={disabled || (!info.enabled && !canTurnOn(info, mcpEnabled))}
              onChange={(e) => {
                if (e.currentTarget.checked) setConfirming(true);
                else void change({ enabled: false });
              }}
            />
            {labels.remote.toggle}
          </label>
          {mcpEnabled && !info.sdk.available && <Banner tone="err">{labels.remote.sdkMissing}</Banner>}
          {mcpEnabled && info.sdk.available && !info.enabled && !info.authtoken_saved && (
            <p className={styles.note}>{labels.remote.needsAuthtoken}</p>
          )}

          {info.enabled && status && (
            <dl className={styles.facts}>
              <dt>{labels.remote.statusLabel}</dt>
              <dd>
                {labels.remote.states[status.state]}
                {status.state !== "error" && status.error ? ` (${status.error})` : ""}
              </dd>
            </dl>
          )}
          {info.enabled && status?.state === "error" && status.error && (
            <Banner tone="err">{status.error}</Banner>
          )}
          {info.enabled && status?.state === "connected" && status.url && (
            <div className={styles.field}>
              <span className={styles.label}>{labels.remote.urlLabel}</span>
              <Row>
                <TextInput value={publicEndpoint(status.url)} readOnly spellCheck={false} />
                <Button onClick={() => void copyEndpoint(status.url as string)}>
                  {copied ? labels.common.copied : labels.common.copy}
                </Button>
              </Row>
              <span className={styles.hint}>{labels.remote.urlHint}</span>
            </div>
          )}

          <div className={styles.field}>
            <span className={styles.label}>{labels.remote.authtokenLabel}</span>
            {showTokenInput ? (
              <TextInput
                type="password"
                aria-label={labels.remote.authtokenLabel}
                autoComplete="off"
                spellCheck={false}
                placeholder={labels.remote.authtokenPlaceholder}
                value={authtoken}
                disabled={disabled}
                onChange={(e) => setAuthtoken(e.currentTarget.value)}
              />
            ) : (
              <Row>
                <span className={styles.saved}>{labels.remote.authtokenSaved}</span>
                <Button onClick={() => setReplacing(true)} disabled={disabled}>
                  {labels.remote.replace}
                </Button>
                <Button
                  onClick={() => {
                    setAuthtoken("");
                    void change({ authtoken: "" });
                  }}
                  disabled={disabled}
                >
                  {labels.common.remove}
                </Button>
              </Row>
            )}
            <span className={styles.hint}>{labels.remote.authtokenHint}</span>
          </div>

          <div className={styles.field}>
            <span className={styles.label}>{labels.remote.domainLabel}</span>
            <TextInput
              aria-label={labels.remote.domainLabel}
              spellCheck={false}
              placeholder={labels.remote.domainPlaceholder}
              value={domain ?? info.domain ?? ""}
              disabled={disabled}
              onChange={(e) => setDomain(e.currentTarget.value)}
            />
            <span className={styles.hint}>{labels.remote.domainHint}</span>
          </div>

          <Row>
            <Button variant="primary" onClick={() => void save()} disabled={disabled || !dirty}>
              {labels.common.save}
            </Button>
            {replacing && (
              <Button
                onClick={() => {
                  setAuthtoken("");
                  setReplacing(false);
                }}
                disabled={busy}
              >
                {labels.common.cancel}
              </Button>
            )}
            <button type="button" className={styles.link} onClick={() => void openExternal(REMOTE_DOCS_URL)}>
              {labels.remote.docs}
            </button>
          </Row>
          {notice && <Banner tone="ok">{notice}</Banner>}
          {error && <Banner tone="err">{error}</Banner>}
        </>
      )}

      {confirming && (
        <Dialog
          title={labels.remote.confirmTitle}
          actions={
            <>
              <Button onClick={() => setConfirming(false)}>{labels.common.cancel}</Button>
              <Button
                variant="primary"
                onClick={() => {
                  setConfirming(false);
                  void change({ enabled: true });
                }}
              >
                {labels.remote.confirm}
              </Button>
            </>
          }
        >
          <p className={dialogStyles.text}>{labels.remote.confirmPublic}</p>
          <p className={dialogStyles.text}>{labels.remote.confirmProvider}</p>
          <p className={dialogStyles.text}>{labels.remote.confirmOff}</p>
        </Dialog>
      )}
    </div>
  );
}
