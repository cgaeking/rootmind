import { useState } from "react";
import { labels } from "../i18n/labels";
import { Dialog, dialogStyles } from "../components/Dialog";
import { Button } from "../components/ui";
import {
  CLAUDE_URL,
  MCPB_URL,
  NGROK_DASHBOARD_URL,
  TOKEN_PLACEHOLDER,
  claudeCodeCommand,
  claudeConnector,
} from "../state/mcpClients";
import { n8nSnippet } from "../state/service";
import { copyText } from "../utils/clipboard";
import { openExternal } from "../utils/openExternal";
import styles from "./ServiceSection.module.css";

/** Reads the MCP token from Rust when a copy needs it; null when it cannot be read. */
export type TokenReader = () => Promise<string | null>;

/** A copy button that says "Copied" for a moment; `text` builds what is copied. */
function CopyButton({
  label,
  text,
}: {
  label: string;
  text: () => Promise<string | null>;
}) {
  const [copied, setCopied] = useState(false);
  return (
    <Button
      onClick={() =>
        void (async () => {
          const value = await text();
          if (value !== null && (await copyText(value))) {
            setCopied(true);
            setTimeout(() => setCopied(false), 1500);
          }
        })()
      }
    >
      {copied ? labels.common.copied : label}
    </Button>
  );
}

function CloseButton({ onClose }: { onClose: () => void }) {
  return <Button onClick={onClose}>{labels.reach.close}</Button>;
}

type ClientTab = "desktop" | "code" | "n8n";

/** Local: how Claude Desktop, Claude Code and n8n reach the MCP endpoint. */
export function ClientsDialog({
  endpoint,
  readToken,
  onClose,
}: {
  endpoint: string;
  readToken: TokenReader;
  onClose: () => void;
}) {
  const [tab, setTab] = useState<ClientTab>("desktop");
  const t = labels.reach.clients;
  const tabs: [ClientTab, string][] = [
    ["desktop", t.tabDesktop],
    ["code", t.tabCode],
    ["n8n", t.tabN8n],
  ];
  const dockerEndpoint = endpoint.replace("127.0.0.1", "host.docker.internal");

  return (
    <Dialog
      title={t.title}
      onClose={onClose}
      wide
      actions={<CloseButton onClose={onClose} />}
    >
      <div role="tablist" className={styles.tabs}>
        {tabs.map(([id, label]) => (
          <button
            key={id}
            type="button"
            role="tab"
            aria-selected={tab === id}
            className={
              tab === id ? `${styles.tab} ${styles.tabActive}` : styles.tab
            }
            onClick={() => setTab(id)}
          >
            {label}
          </button>
        ))}
      </div>

      {tab === "desktop" && (
        <div role="tabpanel" className={styles.panel}>
          <ol className={styles.steps}>
            <li>{t.desktop1}</li>
            <li>{t.desktop2}</li>
            <li>{t.desktop3}</li>
          </ol>
          <div>
            <Button onClick={() => void openExternal(MCPB_URL)}>
              {t.desktopDownload}
            </Button>
          </div>
        </div>
      )}

      {tab === "code" && (
        <div role="tabpanel" className={styles.panel}>
          <p className={dialogStyles.text}>{t.codeIntro}</p>
          <pre className={styles.pre}>
            {claudeCodeCommand(endpoint, TOKEN_PLACEHOLDER)}
          </pre>
          <div>
            <CopyButton
              label={labels.reach.copyCommand}
              text={async () => {
                const token = await readToken();
                return token ? claudeCodeCommand(endpoint, token) : null;
              }}
            />
          </div>
        </div>
      )}

      {tab === "n8n" && (
        <div role="tabpanel" className={styles.panel}>
          <p className={dialogStyles.text}>{t.n8nIntro}</p>
          <pre className={styles.pre}>
            {n8nSnippet(endpoint, TOKEN_PLACEHOLDER)}
          </pre>
          <div>
            <CopyButton
              label={labels.reach.copyValues}
              text={async () => {
                const token = await readToken();
                return token ? n8nSnippet(endpoint, token) : null;
              }}
            />
          </div>
          <p className={dialogStyles.text}>{t.n8nDocker(dockerEndpoint)}</p>
        </div>
      )}

      <p className={dialogStyles.text}>{t.other}</p>
    </Dialog>
  );
}

/** Remote: where to get the ngrok authtoken and the free static domain. */
export function NgrokHelpDialog({ onClose }: { onClose: () => void }) {
  const t = labels.reach.ngrok;
  return (
    <Dialog
      title={t.title}
      onClose={onClose}
      wide
      actions={
        <>
          <Button
            variant="primary"
            onClick={() => void openExternal(NGROK_DASHBOARD_URL)}
          >
            {t.open}
          </Button>
          <CloseButton onClose={onClose} />
        </>
      }
    >
      <p className={dialogStyles.text}>{t.intro}</p>
      <ol className={styles.steps}>
        <li>{t.step1}</li>
        <li>{t.step2}</li>
        <li>{t.step3}</li>
        <li>{t.step4}</li>
      </ol>
    </Dialog>
  );
}

/** Remote, connected: the values of a Claude custom connector, ready to copy. */
export function ClaudeConnectorDialog({
  endpoint,
  readToken,
  onClose,
}: {
  endpoint: string;
  readToken: TokenReader;
  onClose: () => void;
}) {
  const t = labels.reach.claude;
  const shown = claudeConnector(endpoint, "••••••••••••");
  return (
    <Dialog
      title={t.title}
      onClose={onClose}
      wide
      actions={
        <>
          <Button onClick={() => void openExternal(CLAUDE_URL)}>
            {t.open}
          </Button>
          <CloseButton onClose={onClose} />
        </>
      }
    >
      <ol className={styles.steps}>
        <li>{t.step1}</li>
        <li>{t.step2}</li>
      </ol>
      <dl className={styles.values}>
        <dt>{t.name}</dt>
        <dd>{shown.name}</dd>
        <dd />
        <dt>{t.url}</dt>
        <dd>
          <code>{shown.url}</code>
        </dd>
        <dd>
          <CopyButton label={labels.common.copy} text={async () => shown.url} />
        </dd>
        <dt>{t.auth}</dt>
        <dd>{t.authValue}</dd>
        <dd />
        <dt>{t.header}</dt>
        <dd>
          <code>{shown.header}</code>
        </dd>
        <dd />
        <dt>{t.value}</dt>
        <dd>
          <code>{shown.value}</code>
        </dd>
        <dd>
          <CopyButton
            label={labels.common.copy}
            text={async () => {
              const token = await readToken();
              return token ? claudeConnector(endpoint, token).value : null;
            }}
          />
        </dd>
      </dl>
      <ol className={styles.steps} start={3}>
        <li>{t.step3}</li>
        <li>{t.step4}</li>
      </ol>
      <p className={dialogStyles.text}>{t.note}</p>
    </Dialog>
  );
}
