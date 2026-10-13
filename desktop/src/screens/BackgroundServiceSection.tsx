import { useCallback, useEffect, useRef, useState } from "react";
import type { ReactNode } from "react";
import { labels } from "../i18n/labels";
import {
  commandError,
  inTauri,
  serviceDetails,
  serviceDisable,
  serviceEnable,
  serviceInfo,
  serviceMcpToken,
  serviceSetMcp,
  serviceUpdate,
} from "../api/backend";
import type { ServiceInfo, ServiceResult } from "../api/backend";
import { useStore } from "../state/store";
import type { BackendKind } from "../state/store";
import { runServiceChange } from "../state/backendActions";
import {
  N8N_DOCS_URL,
  changeUnderWay,
  isOlderVersion,
  n8nSnippet,
  serviceResultKey,
} from "../state/service";
import type { ServiceOp } from "../state/service";
import { Badge, Banner, Button, Row, Section, TextInput } from "../components/ui";
import { Dialog, dialogStyles } from "../components/Dialog";
import styles from "./BackgroundServiceSection.module.css";

type Phase =
  | { kind: "idle" }
  | { kind: "busy"; op: ServiceOp; underWay: boolean }
  | { kind: "cancelled" }
  | { kind: "failed"; message: string };

/** How often service_info is read while waiting for administrator approval. */
const WATCH_MS = 1_000;

function stepText(op: ServiceOp, underWay: boolean): string {
  const steps = labels.settings.serviceSteps;
  if (!underWay) return steps.approval;
  if (op === "enable") return steps.starting;
  if (op === "disable") return steps.stopping;
  return steps.updating;
}

async function copyText(text: string): Promise<boolean> {
  try {
    await navigator.clipboard.writeText(text);
    return true;
  } catch {
    return false;
  }
}

async function openDocs() {
  try {
    const { openUrl } = await import("@tauri-apps/plugin-opener");
    await openUrl(N8N_DOCS_URL);
  } catch {
    window.open(N8N_DOCS_URL, "_blank", "noopener");
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

/** Settings → "Background service": the toggle, its states, MCP over HTTP and updates. */
export function BackgroundServiceSection() {
  const kind = useStore((s) => s.kind);
  const conn = useStore((s) => s.conn);
  const baseUrl = useStore((s) => s.baseUrl);
  const version = useStore((s) => s.version);
  const appVersion = useStore((s) => s.appMode?.version ?? null);

  const [info, setInfo] = useState<ServiceInfo | null>(null);
  const [infoFailed, setInfoFailed] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: "idle" });
  const [dialog, setDialog] = useState<"on" | "off" | null>(null);
  const [withMcp, setWithMcp] = useState(false);
  const [purge, setPurge] = useState(false);
  const [details, setDetails] = useState<string | null>(null);
  const [detailsBusy, setDetailsBusy] = useState(false);
  const [mcpToken, setMcpToken] = useState<string | null>(null);
  const [tokenError, setTokenError] = useState<string | null>(null);
  const [copied, setCopied] = useState<string | null>(null);
  const watching = useRef(0);

  const refresh = useCallback(async () => {
    if (!inTauri()) {
      setInfoFailed(true);
      return;
    }
    try {
      setInfo(await serviceInfo());
      setInfoFailed(false);
    } catch {
      setInfoFailed(true);
    }
  }, []);

  // Re-read when the backend in use or its state changes (also after a change).
  useEffect(() => {
    void refresh();
  }, [refresh, kind, conn]);

  // Stop watching for approval when the screen closes.
  useEffect(
    () => () => {
      watching.current++;
    },
    [],
  );

  // A token shown for an MCP service that is gone must not linger.
  const mcpInstalled = info?.mcp.installed ?? false;
  useEffect(() => {
    if (!mcpInstalled) setMcpToken(null);
  }, [mcpInstalled]);

  const busy = phase.kind === "busy";

  /** Watch service_info to tell "waiting for approval" from the next step. */
  function watchApproval(op: ServiceOp) {
    const id = ++watching.current;
    const before = info;
    void (async () => {
      while (watching.current === id) {
        await new Promise((r) => setTimeout(r, WATCH_MS));
        if (watching.current !== id) return;
        try {
          if (changeUnderWay(op, await serviceInfo(), before)) {
            if (watching.current === id) {
              setPhase((p) => (p.kind === "busy" ? { ...p, underWay: true } : p));
            }
            return;
          }
        } catch {
          /* keep waiting */
        }
      }
    })();
  }

  /**
   * Run a service change. With `keepBackend` the backend in use (a custom
   * one) stays; otherwise the app switches to what the change leaves running.
   */
  async function run(
    op: ServiceOp,
    call: () => Promise<ServiceResult>,
    kinds: { successKind: BackendKind; fallbackKind: BackendKind },
    keepBackend = false,
  ) {
    setPhase({ kind: "busy", op, underWay: false });
    setDetails(null);
    watchApproval(op);
    const outcome = await runServiceChange(call, {
      ...kinds,
      switching: !keepBackend,
      keepBackend,
    });
    watching.current++;
    if (outcome.kind === "error") {
      setPhase({ kind: "failed", message: labels.settings.serviceFailed(outcome.message) });
    } else if (outcome.result.ok) {
      setPhase({ kind: "idle" });
    } else {
      const key = serviceResultKey(outcome.result);
      setPhase(
        key === "cancelled"
          ? { kind: "cancelled" }
          : { kind: "failed", message: labels.settings.serviceCodes[key] },
      );
    }
    void refresh();
  }

  function turnOn() {
    setDialog(null);
    void run("enable", () => serviceEnable(withMcp), {
      successKind: "service",
      fallbackKind: "builtin",
    });
  }

  function turnOff() {
    setDialog(null);
    void run("disable", () => serviceDisable(purge), {
      successKind: "builtin",
      fallbackKind: "service",
    });
  }

  // A custom backend stays in use while the service is updated or MCP over
  // HTTP is switched; turning the service on or off says that it switches.
  const custom = kind === "custom";

  function update() {
    void run(
      "update",
      serviceUpdate,
      { successKind: "service", fallbackKind: kind ?? "service" },
      custom,
    );
  }

  function setMcp(enabled: boolean) {
    void run(
      enabled ? "mcpOn" : "mcpOff",
      () => serviceSetMcp(enabled),
      { successKind: "service", fallbackKind: kind ?? "service" },
      custom,
    );
  }

  async function loadDetails() {
    setDetailsBusy(true);
    try {
      setDetails(await serviceDetails());
    } catch (e) {
      setDetails(labels.settings.detailsFailed(commandError(e)));
    } finally {
      setDetailsBusy(false);
    }
  }

  async function readMcpToken(): Promise<string | null> {
    if (mcpToken) return mcpToken;
    setTokenError(null);
    try {
      const token = await serviceMcpToken();
      setMcpToken(token);
      return token;
    } catch (e) {
      setTokenError(labels.settings.tokenFailed(commandError(e)));
      return null;
    }
  }

  async function copy(what: string, text: string | null) {
    if (text !== null && (await copyText(text))) {
      setCopied(what);
      setTimeout(() => setCopied((c) => (c === what ? null : c)), 1500);
    }
  }

  const s = labels.settings.serviceStatus;
  const ours = !!info && info.installed && info.ours;
  const blocked = info?.blockedReason ?? null;
  const unsupported = infoFailed || (!!info && (!info.supported || blocked === "unsupported"));
  const connected = kind === "service" && conn === "online";
  // Our service, and this account may manage it (not another account's).
  const manageable = ours && blocked !== "other_account" && blocked !== "foreign_service";

  let status: string;
  if (unsupported) status = s.unsupported;
  else if (!info) status = s.loading;
  else if (blocked === "foreign_service") status = s.foreignService;
  else if (blocked === "other_account") status = s.otherAccount;
  else if (ours && connected) {
    const url = baseUrl || info.url || "";
    status = version ? s.runningVersion(url, version) : s.running(url);
  } else if (ours && kind === "service") status = s.notResponding;
  else if (ours && custom) status = s.customForNow;
  else if (ours) status = s.builtinForNow;
  else if (!info.canElevate) status = s.notAdmin;
  else status = s.off;

  const toggleDisabled =
    busy ||
    unsupported ||
    !info ||
    blocked === "foreign_service" ||
    blocked === "other_account" ||
    !info.canElevate;

  const updateOffer =
    manageable && connected && version && appVersion && isOlderVersion(version, appVersion)
      ? { service: version, app: appVersion }
      : null;

  const connBadge =
    conn === "online" ? (
      <Badge tone="ok">{labels.conn.online}</Badge>
    ) : conn === "offline" || conn === "unauthorized" ? (
      <Badge tone="err">{labels.conn[conn]}</Badge>
    ) : (
      <Badge tone="muted">{labels.conn[conn]}</Badge>
    );

  const mcpUrl = info?.mcp.installed ? info.mcp.url : null;

  return (
    <Section
      title={labels.settings.serviceTitle}
      description={labels.settings.serviceHelp}
      actions={connBadge}
    >
      <label className={styles.toggle}>
        <input
          type="checkbox"
          role="switch"
          checked={ours}
          disabled={toggleDisabled}
          onChange={(e) => {
            if (e.currentTarget.checked) {
              setWithMcp(false);
              setDialog("on");
            } else {
              setPurge(false);
              setDialog("off");
            }
          }}
        />
        {labels.settings.serviceToggle}
      </label>

      <p className="muted" style={{ margin: 0 }}>
        {busy ? stepText(phase.op, phase.underWay) : status}
      </p>

      {phase.kind === "cancelled" && <Banner tone="info">{labels.settings.serviceCodes.cancelled}</Banner>}
      {phase.kind === "failed" && (
        <>
          <Banner tone="err">{phase.message}</Banner>
          <Row>
            <Button onClick={() => void loadDetails()} disabled={detailsBusy}>
              {detailsBusy ? labels.settings.detailsLoading : labels.settings.details}
            </Button>
          </Row>
          {details && <pre className={styles.pre}>{details}</pre>}
        </>
      )}

      {updateOffer && (
        <>
          <Banner tone="info">{labels.settings.updateBanner(updateOffer.service, updateOffer.app)}</Banner>
          <Row>
            <Button variant="primary" onClick={update} disabled={busy || !info?.canElevate}>
              {labels.settings.updateService}
            </Button>
          </Row>
        </>
      )}

      {manageable && !connected && !busy && !custom && (
        <Row>
          <Button onClick={update} disabled={!info?.canElevate}>
            {labels.settings.repair}
          </Button>
        </Row>
      )}

      {manageable && info && (
        <div className={styles.mcp}>
          <label className={styles.toggle}>
            <input
              type="checkbox"
              checked={info.mcp.installed}
              disabled={busy || !info.canElevate}
              onChange={(e) => setMcp(e.currentTarget.checked)}
            />
            {labels.settings.mcpToggle}
          </label>

          {mcpUrl && (
            <>
              <Labeled label={labels.settings.mcpUrlLabel}>
                <Row>
                  <TextInput value={mcpUrl} readOnly spellCheck={false} />
                  <Button onClick={() => void copy("url", mcpUrl)}>
                    {copied === "url" ? labels.common.copied : labels.common.copy}
                  </Button>
                </Row>
              </Labeled>

              <Labeled label={labels.settings.mcpTokenLabel}>
                <Row>
                  {mcpToken ? (
                    <>
                      <TextInput value={mcpToken} readOnly spellCheck={false} />
                      <Button onClick={() => void copy("token", mcpToken)}>
                        {copied === "token" ? labels.common.copied : labels.common.copy}
                      </Button>
                      <Button onClick={() => setMcpToken(null)}>{labels.settings.hideToken}</Button>
                    </>
                  ) : (
                    <Button onClick={() => void readMcpToken()}>{labels.settings.showToken}</Button>
                  )}
                </Row>
              </Labeled>
              {tokenError && <Banner tone="err">{tokenError}</Banner>}

              <Labeled label={labels.settings.n8nLabel} hint={labels.settings.n8nHint}>
                <pre className={styles.pre}>{n8nSnippet(mcpUrl, mcpToken ?? "<token>")}</pre>
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
                <button type="button" className={styles.link} onClick={() => void openDocs()}>
                  {labels.settings.mcpDocs}
                </button>
              </Row>
            </>
          )}
        </div>
      )}

      {dialog === "on" && (
        <Dialog
          title={labels.settings.turnOnTitle}
          actions={
            <>
              <Button onClick={() => setDialog(null)}>{labels.common.cancel}</Button>
              <Button variant="primary" onClick={turnOn}>
                {labels.settings.turnOn}
              </Button>
            </>
          }
        >
          <p className={dialogStyles.text}>{labels.settings.serviceHelp}</p>
          {custom && <p className={dialogStyles.text}>{labels.settings.turnOnLeavesCustom}</p>}
          <label className={dialogStyles.check}>
            <input
              type="checkbox"
              checked={withMcp}
              onChange={(e) => setWithMcp(e.currentTarget.checked)}
            />
            {labels.settings.turnOnMcp}
          </label>
        </Dialog>
      )}

      {dialog === "off" && (
        <Dialog
          title={labels.settings.turnOffTitle}
          actions={
            <>
              <Button onClick={() => setDialog(null)}>{labels.common.cancel}</Button>
              <Button variant="primary" onClick={turnOff}>
                {labels.settings.turnOff}
              </Button>
            </>
          }
        >
          <p className={dialogStyles.text}>{labels.settings.turnOffText}</p>
          {custom && <p className={dialogStyles.text}>{labels.settings.turnOffLeavesCustom}</p>}
          <label className={dialogStyles.check}>
            <input
              type="checkbox"
              checked={purge}
              onChange={(e) => setPurge(e.currentTarget.checked)}
            />
            {labels.settings.turnOffPurge}
          </label>
        </Dialog>
      )}
    </Section>
  );
}
