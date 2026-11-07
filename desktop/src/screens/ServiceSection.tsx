import { useEffect, useState } from "react";
import { labels } from "../i18n/labels";
import {
  commandError,
  inTauri,
  serviceDetails,
  serviceMcpToken,
  serviceSetMcp,
  serviceUpdate,
} from "../api/backend";
import type { ServiceResult } from "../api/backend";
import { recheck } from "../hooks/useBackend";
import { useServiceInfo } from "../hooks/useServiceInfo";
import { DEFAULT_BASE_URL, useStore } from "../state/store";
import { connectCustom, connectService, runServiceChange } from "../state/backendActions";
import { canRepair, changeMessage, isOlderVersion, problemFromInfo, repairLabels } from "../state/service";
import type { ServiceProblem } from "../state/service";
import { Badge, Banner, Button, Field, Row, Section, TextInput } from "../components/ui";
import { McpReach } from "./McpReach";
import styles from "./ServiceSection.module.css";

type Busy = "repair" | "mcp" | "check" | null;

/**
 * Settings › RootMind Service › which server the app uses: Local, this
 * computer's RootMind service (its address), or Remote, a RootMind server at
 * another address with an optional token. Local connects at once; Remote
 * connects on Apply. The service's address typed under Remote is the service
 * (state/backendActions.ts).
 */
function ServerChoice({ localUrl, disabled }: { localUrl: string; disabled: boolean }) {
  const kind = useStore((s) => s.kind);
  const baseUrl = useStore((s) => s.baseUrl);
  const token = useStore((s) => s.token);
  const remoteInUse = kind === "custom" && !!baseUrl && baseUrl !== localUrl;
  const remoteUrl = remoteInUse ? baseUrl : "";
  const remoteToken = remoteInUse ? (token ?? "") : "";

  const [choice, setChoice] = useState<"local" | "remote">(remoteInUse ? "remote" : "local");
  useEffect(() => setChoice(remoteInUse ? "remote" : "local"), [remoteInUse]);
  // The fields follow the store: the backend dialog can change the address too.
  const [urlDraft, setUrlDraft] = useState(remoteUrl);
  const [tokenDraft, setTokenDraft] = useState(remoteToken);
  useEffect(() => setUrlDraft(remoteUrl), [remoteUrl]);
  useEffect(() => setTokenDraft(remoteToken), [remoteToken]);

  function chooseLocal() {
    setChoice("local");
    if (!remoteInUse && kind) return;
    void (inTauri() ? connectService() : connectCustom(DEFAULT_BASE_URL, null));
  }

  return (
    <div className={styles.server}>
      <label className={styles.toggle}>
        <input
          type="radio"
          name="server"
          checked={choice === "local"}
          disabled={disabled}
          onChange={chooseLocal}
        />
        {labels.server.local(localUrl)}
      </label>
      <label className={styles.toggle}>
        <input
          type="radio"
          name="server"
          checked={choice === "remote"}
          disabled={disabled}
          onChange={() => setChoice("remote")}
        />
        {labels.server.remote}
      </label>
      {choice === "remote" && (
        <div className={styles.serverFields}>
          <Field label={labels.server.urlLabel}>
            <TextInput
              value={urlDraft}
              onChange={(e) => setUrlDraft(e.currentTarget.value)}
              placeholder={labels.server.urlPlaceholder}
              spellCheck={false}
            />
          </Field>
          <Field label={labels.server.tokenLabel}>
            <TextInput
              type="password"
              value={tokenDraft}
              onChange={(e) => setTokenDraft(e.currentTarget.value)}
              spellCheck={false}
              autoComplete="off"
            />
          </Field>
          <Row>
            <Button
              onClick={() => void connectCustom(urlDraft, tokenDraft)}
              disabled={disabled || !urlDraft.trim()}
            >
              {labels.server.apply}
            </Button>
            <Button onClick={() => void recheck()} disabled={disabled}>
              {labels.server.check}
            </Button>
          </Row>
        </div>
      )}
    </div>
  );
}

/**
 * Settings › "MCP" and "RootMind Service". MCP: MCP over HTTP (on by
 * default; switching it needs administrator approval) with who can reach it:
 * Local, and Remote through ngrok (McpReach). RootMind Service: the service
 * the installer set up, with its state, Set up when it was never set up,
 * Repair when it does not work, and which server the app uses (ServerChoice).
 * On Linux the service is the account's systemd user services; the app sets
 * it up and updates it on its own (state/autoSetup.ts), shown here while it
 * runs and when it failed.
 */
export function ServiceSection() {
  const kind = useStore((s) => s.kind);
  const conn = useStore((s) => s.conn);
  const version = useStore((s) => s.version);
  const checkedProblem = useStore((s) => s.serviceProblem);
  const appVersion = useStore((s) => s.appMode?.version ?? null);
  const auto = useStore((s) => s.autoService);
  const { info, failed: infoFailed, refresh } = useServiceInfo();

  const [busy, setBusy] = useState<Busy>(null);
  const [localMessage, setMessage] = useState<{
    tone: "info" | "err";
    text: string;
    from: "repair" | "mcp";
  } | null>(null);
  const [details, setDetails] = useState<string | null>(null);
  const [detailsBusy, setDetailsBusy] = useState(false);
  const [tokenError, setTokenError] = useState<string | null>(null);

  // A failed change the app made on its own (Linux) shows like a failed click.
  const message =
    localMessage ??
    (auto?.status === "failed" && auto.message
      ? { tone: "err" as const, text: auto.message, from: "repair" as const }
      : null);
  const autoRunning = auto?.status === "running";

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
      const msg = changeMessage(await runServiceChange(op));
      setMessage(msg && { ...msg, from: what });
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

  const updateOffer =
    connected && version && appVersion && isOlderVersion(version, appVersion) && info?.canElevate
      ? { service: version, app: appVersion }
      : null;

  // The status of the server in use: the service, or one at another address.
  const badge = custom && conn !== "offline" ? (
    <Badge tone={conn === "online" ? "ok" : "muted"}>{labels.conn[conn]}</Badge>
  ) : connected ? (
    <Badge tone="ok">{labels.service.running}</Badge>
  ) : problem ? (
    <Badge tone="err">{labels.conn.offline}</Badge>
  ) : kind === "service" ? (
    <Badge tone="muted">{labels.conn[conn]}</Badge>
  ) : null;

  // MCP over HTTP, for our service when this account may use it.
  const showMcp = !!info && info.installed && info.ours && info.tokenReadable;
  const mcpUrl = showMcp && info.mcp.installed ? info.mcp.url : null;

  let statusText: string | null = null;
  if (busy === "repair") statusText = labels.service[repairText.busy];
  else if (autoRunning) statusText = labels.service[auto?.action === "setup" ? "autoSetup" : "autoUpdate"];
  else if (kind === "service" && !connected && !problem) statusText = labels.service.connecting;
  else if (!kind) statusText = labels.service.loading;

  const messageView = (from: "repair" | "mcp") =>
    message?.from === from && (
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
    );

  return (
    <>
      {showMcp && (
        <Section title={labels.mcp.title}>
          <label className={styles.toggle}>
            <input
              type="checkbox"
              checked={info.mcp.installed}
              disabled={!!busy || autoRunning || !info.canElevate}
              onChange={(e) => {
                const enabled = e.currentTarget.checked;
                void run("mcp", () => serviceSetMcp(enabled));
              }}
            />
            {labels.service.mcpToggle}
          </label>
          {busy === "mcp" && (
            <p className="muted" style={{ margin: 0 }}>
              {labels.service.applying}
            </p>
          )}
          {messageView("mcp")}

          {mcpUrl && (
            <McpReach
              endpoint={mcpUrl}
              readToken={readMcpToken}
              serviceInUse={connected}
              onUseService={() => void connectService()}
              disabled={!!busy || autoRunning}
            />
          )}
          {tokenError && <Banner tone="err">{tokenError}</Banner>}
        </Section>
      )}

      <Section title={labels.service.title} actions={badge}>
        {statusText && (
          <p className="muted" style={{ margin: 0 }}>
            {statusText}
          </p>
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
                <Button variant="primary" onClick={() => void run("repair", serviceUpdate)} disabled={!!busy || autoRunning}>
                  {labels.service[repairText.action]}
                </Button>
              )}
              <Button onClick={() => void checkAgain()} disabled={!!busy || autoRunning}>
                {busy === "check" ? labels.backendModal.checking : labels.backendModal.checkAgain}
              </Button>
            </Row>
          </>
        )}

        {updateOffer && (
          <>
            <Banner tone="info">{labels.service.updateBanner(updateOffer.service, updateOffer.app)}</Banner>
            <Row>
              <Button onClick={() => void run("repair", serviceUpdate)} disabled={!!busy || autoRunning}>
                {labels.service.updateService}
              </Button>
            </Row>
          </>
        )}

        {messageView("repair")}

        <ServerChoice localUrl={info?.url ?? DEFAULT_BASE_URL} disabled={!!busy || autoRunning} />
      </Section>
    </>
  );
}
