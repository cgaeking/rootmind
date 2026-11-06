import { useCallback, useEffect, useState } from "react";
import type { ReactNode } from "react";
import { labels } from "../i18n/labels";
import { getRemoteAccess, updateRemoteAccess } from "../api/rlm";
import type { RemoteAccessChange, RemoteAccessInfo } from "../types/api";
import {
  REMOTE_POLL_MS,
  connectChange,
  looksLikeAuthtoken,
  publicEndpoint,
  tunnelError,
} from "../state/remoteAccess";
import { Dialog, dialogStyles } from "../components/Dialog";
import { Badge, Banner, Button, Row, TextInput } from "../components/ui";
import { copyText } from "../utils/clipboard";
import {
  ClaudeConnectorDialog,
  ClientsDialog,
  NgrokHelpDialog,
} from "./McpHelpDialogs";
import type { TokenReader } from "./McpHelpDialogs";
import styles from "./ServiceSection.module.css";

type HelpDialog = "clients" | "ngrok" | "claude" | null;

function message(e: unknown): string {
  return e instanceof Error && e.message ? e.message : String(e);
}

/**
 * A card with a label and a hint. With `onToggle` it has a checkbox and shows
 * its body only while checked; without, its body always shows.
 */
function Option({
  checked = true,
  label,
  hint,
  disabled = false,
  onToggle,
  children,
}: {
  checked?: boolean;
  label: string;
  hint: string;
  disabled?: boolean;
  onToggle?: (checked: boolean) => void;
  children?: ReactNode;
}) {
  const text = (
    <span className={styles.optionText}>
      <span className={styles.optionLabel}>{label}</span>
      <span className={styles.hint}>{hint}</span>
    </span>
  );
  return (
    <div
      className={
        checked ? `${styles.option} ${styles.optionOn}` : styles.option
      }
    >
      {onToggle ? (
        <label className={styles.optionHead}>
          <input
            type="checkbox"
            checked={checked}
            disabled={disabled}
            onChange={(e) => onToggle(e.currentTarget.checked)}
          />
          {text}
        </label>
      ) : (
        <div className={styles.optionHead}>{text}</div>
      )}
      {checked && children && (
        <div className={styles.optionBody}>{children}</div>
      )}
    </div>
  );
}

/** An endpoint in a read-only field with Copy and Copy token. */
function Endpoint({
  url,
  label,
  readToken,
}: {
  url: string;
  label: string;
  readToken: TokenReader;
}) {
  const [copied, setCopied] = useState<"url" | "token" | null>(null);
  async function copy(what: "url" | "token", text: string | null) {
    if (text !== null && (await copyText(text))) {
      setCopied(what);
      setTimeout(() => setCopied((c) => (c === what ? null : c)), 1500);
    }
  }
  return (
    <Row>
      <TextInput value={url} readOnly spellCheck={false} aria-label={label} />
      <Button onClick={() => void copy("url", url)}>
        {copied === "url" ? labels.common.copied : labels.common.copy}
      </Button>
      <Button
        title={labels.service.copyTokenTitle}
        onClick={() => void (async () => copy("token", await readToken()))()}
      >
        {copied === "token" ? labels.common.copied : labels.service.copyToken}
      </Button>
    </Row>
  );
}

/**
 * Settings › RootMind service › Serve MCP over HTTP: who can reach the MCP
 * endpoint. Local (programs on this computer) is always there, with the
 * endpoint and the MCP token; the Remote checkbox adds a public address
 * through ngrok, which the RootMind service opens (remote access, set
 * through the REST API). Remote needs the app to use the service: the
 * settings belong to it. Each part links to a help dialog with the values a
 * client needs.
 */
export function McpReach({
  endpoint,
  readToken,
  serviceInUse,
  onUseService,
  disabled,
}: {
  /** The local MCP endpoint, http://127.0.0.1:<port>/mcp. */
  endpoint: string;
  readToken: TokenReader;
  /** The app uses this computer's RootMind service (Remote can be set). */
  serviceInUse: boolean;
  onUseService: () => void;
  disabled: boolean;
}) {
  const [info, setInfo] = useState<RemoteAccessInfo | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // Remote ticked but not turned on yet (the setup shows).
  const [wanted, setWanted] = useState(false);
  // The authtoken lives only in this field until it is saved; it is never read back.
  const [authtoken, setAuthtoken] = useState("");
  const [replacing, setReplacing] = useState(false);
  // null: not edited, the saved domain is shown.
  const [domain, setDomain] = useState<string | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [help, setHelp] = useState<HelpDialog>(null);

  const load = useCallback(async () => {
    try {
      setInfo(await getRemoteAccess());
      setLoadError(null);
    } catch (e) {
      setLoadError(labels.remote.loadFailed(message(e)));
    }
  }, []);

  useEffect(() => {
    if (serviceInUse) void load();
  }, [serviceInUse, load]);

  const enabled = serviceInUse && (info?.enabled ?? false);
  useEffect(() => {
    if (!enabled) return;
    const timer = setInterval(() => void load(), REMOTE_POLL_MS);
    return () => clearInterval(timer);
  }, [enabled, load]);

  const remoteShown = enabled || wanted;

  async function change(c: RemoteAccessChange): Promise<boolean> {
    setBusy(true);
    setError(null);
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

  /**
   * Why the typed ngrok Authtoken cannot be right, or null: it is the MCP
   * token (the usual mix-up) or does not have the form of an ngrok Authtoken.
   */
  async function authtokenProblem(): Promise<string | null> {
    const typed = authtoken.trim();
    if (!typed) return null;
    if (typed === (await readToken())) return labels.remote.isMcpToken;
    if (!looksLikeAuthtoken(typed)) return labels.remote.notAuthtoken;
    return null;
  }

  /** Check the typed authtoken first; `next` runs only when it can be right. */
  async function checked(next: () => void) {
    const problem = await authtokenProblem();
    setError(problem);
    if (!problem) next();
  }

  async function connect() {
    if (!info) return;
    if (await change(connectChange(info, authtoken, domain))) {
      setAuthtoken("");
      setReplacing(false);
      setDomain(null);
      setWanted(false);
    }
  }

  async function toggleRemote(on: boolean) {
    setError(null);
    setWanted(on);
    // Unticking closes the public address.
    if (!on && enabled) await change({ enabled: false });
  }

  const off = disabled || busy;
  const status = info?.status;
  const showTokenInput = !!info && (!info.authtoken_saved || replacing);
  const canConnect =
    !!info &&
    info.sdk.available &&
    (info.authtoken_saved || authtoken.trim() !== "");
  const publicUrl =
    enabled && status?.state === "connected" && status.url
      ? publicEndpoint(status.url)
      : null;
  // The setup shows until the tunnel is on, and again to fix a failed one.
  const showSetup = !!info && (!enabled || status?.state === "error");

  const setup = info && (
    <>
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
            disabled={off}
            onChange={(e) => setAuthtoken(e.currentTarget.value)}
          />
        ) : (
          <Row>
            <span className={styles.saved}>{labels.remote.authtokenSaved}</span>
            <Button onClick={() => setReplacing(true)} disabled={off}>
              {labels.remote.replace}
            </Button>
            {!enabled && (
              <Button
                onClick={() => void change({ authtoken: "" })}
                disabled={off}
              >
                {labels.common.remove}
              </Button>
            )}
          </Row>
        )}
      </div>
      <div className={styles.field}>
        <span className={styles.label}>{labels.remote.domainLabel}</span>
        <TextInput
          aria-label={labels.remote.domainLabel}
          spellCheck={false}
          placeholder={labels.remote.domainPlaceholder}
          value={domain ?? info.domain ?? ""}
          disabled={off}
          onChange={(e) => setDomain(e.currentTarget.value)}
        />
        <span className={styles.hint}>{labels.remote.domainHint}</span>
      </div>
      <button
        type="button"
        className={styles.link}
        onClick={() => setHelp("ngrok")}
      >
        {labels.reach.ngrokLink}
      </button>
      <Row>
        {enabled ? (
          <Button
            variant="primary"
            onClick={() => void checked(() => void connect())}
            disabled={off}
          >
            {labels.reach.saveRetry}
          </Button>
        ) : (
          <Button
            variant="primary"
            onClick={() => void checked(() => setConfirming(true))}
            disabled={off || !canConnect}
          >
            {labels.reach.connect}
          </Button>
        )}
      </Row>
      {!enabled && <p className={styles.note}>{labels.reach.connectNote}</p>}
    </>
  );

  return (
    <div className={styles.reach}>
      <Option label={labels.reach.local} hint={labels.reach.localHint}>
        <Endpoint
          url={endpoint}
          label={labels.service.mcpUrlLabel}
          readToken={readToken}
        />
        <button
          type="button"
          className={styles.link}
          onClick={() => setHelp("clients")}
        >
          {labels.reach.clientsLink}
        </button>
      </Option>

      <Option
        checked={remoteShown}
        label={labels.reach.remote}
        hint={labels.reach.remoteHint}
        disabled={off}
        onToggle={(on) => void toggleRemote(on)}
      >
        {!serviceInUse ? (
          <>
            <p className={styles.note}>{labels.reach.needsService}</p>
            <Row>
              <Button onClick={onUseService}>{labels.reach.useService}</Button>
            </Row>
          </>
        ) : (
          <>
            {loadError && <Banner tone="err">{loadError}</Banner>}
            {info && !info.sdk.available && (
              <Banner tone="err">{labels.remote.sdkMissing}</Banner>
            )}

            {enabled && status && (
              <Row>
                <Badge
                  tone={
                    status.state === "connected"
                      ? "ok"
                      : status.state === "error"
                        ? "err"
                        : "muted"
                  }
                >
                  {labels.remote.states[status.state]}
                </Badge>
                {info?.domain && (
                  <span className={styles.saved}>{info.domain}</span>
                )}
              </Row>
            )}
            {enabled && status?.state === "error" && status.error && (
              <Banner tone="err">
                {tunnelError(status.error, labels.remote.errors)}
              </Banner>
            )}

            {publicUrl && (
              <>
                <div className={styles.field}>
                  <span className={styles.label}>{labels.remote.urlLabel}</span>
                  <Endpoint
                    url={publicUrl}
                    label={labels.remote.urlLabel}
                    readToken={readToken}
                  />
                </div>
                <button
                  type="button"
                  className={styles.link}
                  onClick={() => setHelp("claude")}
                >
                  {labels.reach.claudeLink}
                </button>
                <p className={styles.note}>{labels.reach.connectedNote}</p>
              </>
            )}

            {showSetup && setup}
            {error && <Banner tone="err">{error}</Banner>}
          </>
        )}
      </Option>

      {confirming && (
        <Dialog
          title={labels.remote.confirmTitle}
          onClose={() => setConfirming(false)}
          actions={
            <>
              <Button onClick={() => setConfirming(false)}>
                {labels.common.cancel}
              </Button>
              <Button
                variant="primary"
                onClick={() => {
                  setConfirming(false);
                  void connect();
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

      {help === "clients" && (
        <ClientsDialog
          endpoint={endpoint}
          readToken={readToken}
          onClose={() => setHelp(null)}
        />
      )}
      {help === "ngrok" && <NgrokHelpDialog onClose={() => setHelp(null)} />}
      {help === "claude" && publicUrl && (
        <ClaudeConnectorDialog
          endpoint={publicUrl}
          readToken={readToken}
          onClose={() => setHelp(null)}
        />
      )}
    </div>
  );
}
