import { useState } from "react";
import type { ReactNode } from "react";
import { labels } from "../i18n/labels";
import {
  commandError,
  serviceDetails,
  serviceMcpToken,
  serviceSetMcp,
  serviceUpdate,
} from "../api/backend";
import type { ServiceResult } from "../api/backend";
import { recheck } from "../hooks/useBackend";
import { useServiceInfo } from "../hooks/useServiceInfo";
import { useStore } from "../state/store";
import { runServiceChange } from "../state/backendActions";
import {
  N8N_DOCS_URL,
  canRepair,
  changeMessage,
  isOlderVersion,
  n8nSnippet,
  problemFromInfo,
  repairLabels,
} from "../state/service";
import type { ServiceProblem } from "../state/service";
import { openExternal } from "../utils/openExternal";
import { Badge, Banner, Button, Row, Section, TextInput } from "../components/ui";
import styles from "./ServiceSection.module.css";

type Busy = "repair" | "mcp" | "check" | null;

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

/**
 * A label above a row of controls. Unlike ui's Field it is not a <label>
 * element, which would name its first button after the label text.
 */
function Labeled({ label, hint, children }: { label: string; hint?: string; children: ReactNode }) {
  return (
    <div className={styles.field}>
      <span className={styles.label}>{label}</span>
      {children}
      {hint && <span className={styles.hint}>{hint}</span>}
    </div>
  );
}

/**
 * Settings › "RootMind service": the service the installer set up, read-only
 * (state, address, version), with Set up when it was never set up, Repair
 * when it does not work, and MCP over HTTP (on by default; switching it needs
 * administrator approval).
 */
export function ServiceSection() {
  const kind = useStore((s) => s.kind);
  const conn = useStore((s) => s.conn);
  const baseUrl = useStore((s) => s.baseUrl);
  const version = useStore((s) => s.version);
  const checkedProblem = useStore((s) => s.serviceProblem);
  const appVersion = useStore((s) => s.appMode?.version ?? null);
  const { info, failed: infoFailed, refresh } = useServiceInfo();

  const [busy, setBusy] = useState<Busy>(null);
  const [message, setMessage] = useState<{ tone: "info" | "err"; text: string } | null>(null);
  const [details, setDetails] = useState<string | null>(null);
  const [detailsBusy, setDetailsBusy] = useState(false);
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);

  const custom = kind === "custom";
  const connected = kind === "service" && conn === "online";

  // While the app uses the service, the connectivity check knows what is
  // wrong; with a custom backend only service_info tells.
  let problem: ServiceProblem | null = null;
  if (custom) problem = problemFromInfo(info, infoFailed);
  else if (kind === "service" && conn === "offline") problem = checkedProblem ?? "noAnswer";

  const repairable = !!problem && canRepair(problem, info);
  // "Set up" for a service that was never set up, else "Repair".
  const repairText = repairLabels(problem ?? "noAnswer");
  const needsAdmin =
    !!problem && !repairable && !!info && !info.canElevate && canRepair(problem, { ...info, canElevate: true });

  /** Run a service change and report its outcome. */
  async function run(what: "repair" | "mcp", op: () => Promise<ServiceResult>) {
    setBusy(what);
    setMessage(null);
    setDetails(null);
    try {
      setMessage(changeMessage(await runServiceChange(op)));
    } finally {
      setBusy(null);
      void refresh();
    }
  }

  async function checkAgain() {
    setBusy("check");
    try {
      await recheck();
      await refresh();
    } finally {
      setBusy(null);
    }
  }

  async function loadDetails() {
    setDetailsBusy(true);
    try {
      setDetails(await serviceDetails());
    } catch (e) {
      setDetails(labels.service.detailsFailed(commandError(e)));
    } finally {
      setDetailsBusy(false);
    }
  }

  /** Read the MCP token from Rust; it is never kept in the UI. */
  async function readMcpToken(): Promise<string | null> {
    setTokenError(null);
    try {
      return await serviceMcpToken();
    } catch (e) {
      setTokenError(labels.service.tokenFailed(commandError(e)));
      return null;
    }
  }

  async function copy(what: string, text: string | null) {
    if (text !== null && (await copyText(text))) {
      setCopied(what);
      setTimeout(() => setCopied((c) => (c === what ? null : c)), 1500);
    }
  }

  const updateOffer =
    connected && version && appVersion && isOlderVersion(version, appVersion) && info?.canElevate
      ? { service: version, app: appVersion }
      : null;

  // The service's own status (not that of a custom backend).
  const badge = connected ? (
    <Badge tone="ok">{labels.service.running}</Badge>
  ) : problem ? (
    <Badge tone="err">{labels.conn.offline}</Badge>
  ) : kind === "service" ? (
    <Badge tone="muted">{labels.conn[conn]}</Badge>
  ) : null;

  // Where the service listens: the connected URL, else what service_info reports.
  const address = connected ? baseUrl : !problem ? (info?.url ?? null) : null;
  // MCP over HTTP, for our service when this account may use it.
  const showMcp = !!info && info.installed && info.ours && info.tokenReadable;
  const mcpUrl = showMcp && info.mcp.installed ? info.mcp.url : null;

  let statusText: string | null = null;
  if (busy === "repair") statusText = labels.service[repairText.busy];
  else if (busy === "mcp") statusText = labels.service.applying;
  else if (custom) statusText = labels.service.customInUse;
  else if (kind === "service" && !connected && !problem) statusText = labels.service.connecting;
  else if (!kind) statusText = labels.service.loading;

  return (
    <Section title={labels.service.title} description={labels.service.desc} actions={badge}>
      {statusText && (
        <p className="muted" style={{ margin: 0 }}>
          {statusText}
        </p>
      )}

      {address && !problem && (
        <dl className={styles.facts}>
          <dt>{labels.service.addressLabel}</dt>
          <dd>{address}</dd>
          {connected && version && (
            <>
              <dt>{labels.service.versionLabel}</dt>
              <dd>{version}</dd>
            </>
          )}
        </dl>
      )}

      {problem && (
        <>
          <Banner tone="err">
            <span className={styles.problemTitle}>{labels.service.problems[problem].title}.</span>{" "}
            {labels.service.problems[problem].text}
          </Banner>
          {needsAdmin && <p className="muted">{labels.service.repairNeedsAdmin}</p>}
          <Row>
            {repairable && (
              <Button variant="primary" onClick={() => void run("repair", serviceUpdate)} disabled={!!busy}>
                {labels.service[repairText.action]}
              </Button>
            )}
            <Button onClick={() => void checkAgain()} disabled={!!busy}>
              {busy === "check" ? labels.backendModal.checking : labels.backendModal.checkAgain}
            </Button>
          </Row>
        </>
      )}

      {updateOffer && (
        <>
          <Banner tone="info">{labels.service.updateBanner(updateOffer.service, updateOffer.app)}</Banner>
          <Row>
            <Button onClick={() => void run("repair", serviceUpdate)} disabled={!!busy}>
              {labels.service.updateService}
            </Button>
          </Row>
        </>
      )}

      {message && (
        <>
          <Banner tone={message.tone}>{message.text}</Banner>
          {message.tone === "err" && (
            <Row>
              <Button onClick={() => void loadDetails()} disabled={detailsBusy}>
                {detailsBusy ? labels.service.detailsLoading : labels.service.details}
              </Button>
            </Row>
          )}
          {details && <pre className={styles.pre}>{details}</pre>}
        </>
      )}

      {showMcp && (
        <div className={styles.mcp}>
          <label className={styles.toggle}>
            <input
              type="checkbox"
              checked={info.mcp.installed}
              disabled={!!busy || !info.canElevate}
              onChange={(e) => {
                const enabled = e.currentTarget.checked;
                void run("mcp", () => serviceSetMcp(enabled));
              }}
            />
            {labels.service.mcpToggle}
          </label>

          {mcpUrl && (
            <>
              <Labeled label={labels.service.mcpUrlLabel}>
                <Row>
                  <TextInput value={mcpUrl} readOnly spellCheck={false} />
                  <Button onClick={() => void copy("url", mcpUrl)}>
                    {copied === "url" ? labels.common.copied : labels.common.copy}
                  </Button>
                  <Button onClick={() => void (async () => copy("token", await readMcpToken()))()}>
                    {copied === "token" ? labels.common.copied : labels.service.copyToken}
                  </Button>
                </Row>
              </Labeled>
              {tokenError && <Banner tone="err">{tokenError}</Banner>}

              <Labeled label={labels.service.n8nLabel} hint={labels.service.n8nHint}>
                <pre className={styles.pre}>{n8nSnippet(mcpUrl, "<token>")}</pre>
              </Labeled>
              <Row>
                <Button
                  onClick={() =>
                    void (async () => {
                      const token = await readMcpToken();
                      if (token) await copy("n8n", n8nSnippet(mcpUrl, token));
                    })()
                  }
                >
                  {copied === "n8n" ? labels.common.copied : labels.common.copy}
                </Button>
                <button type="button" className={styles.link} onClick={() => void openExternal(N8N_DOCS_URL)}>
                  {labels.service.mcpDocs}
                </button>
              </Row>
            </>
          )}
        </div>
      )}
    </Section>
  );
}
