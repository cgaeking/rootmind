import { useCallback, useEffect, useRef, useState } from "react";
import { labels } from "../i18n/labels";
import { useBackend } from "../hooks/useBackend";
import { getStatus, getStatistics, getProgress, getRecentIndexed, refreshIndex } from "../api/rlm";
import { ApiError } from "../api/client";
import type { ProgressResponse, RecentDoc, Statistics, StatusResponse } from "../types/api";
import { Badge, Banner, Button, Row, Section } from "../components/ui";

function fmtNum(n: number): string {
  return n.toLocaleString(labels.locale);
}

// Compact form for very large counts so they fit the stat tile, e.g.
// 3.013.235.096 -> "3.013 M". Smaller values stay readable (plain / "K").
function fmtCompact(n: number): string {
  if (n < 1_000) return fmtNum(n);
  if (n < 1_000_000) return `${Math.round(n / 1_000)} K`;
  return `${fmtNum(Math.round(n / 1_000_000))} M`;
}

function fmtBytes(n: number): string {
  if (!n) return "0 B";
  const units = ["B", "KB", "MB", "GB", "TB"];
  const i = Math.floor(Math.log(n) / Math.log(1024));
  return `${(n / Math.pow(1024, i)).toFixed(i === 0 ? 0 : 1)} ${units[i]}`;
}

function fmtDate(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime()) ? "—" : d.toLocaleString(labels.locale);
}

function fmtTime(iso: string | null): string {
  if (!iso) return "—";
  const d = new Date(iso);
  return Number.isNaN(d.getTime())
    ? "—"
    : d.toLocaleTimeString(labels.locale, { hour: "2-digit", minute: "2-digit", second: "2-digit" });
}

function Stat({ value, label, title }: { value: string; label: string; title?: string }) {
  return (
    <div className="stat">
      <div className="stat-value" title={title}>
        {value}
      </div>
      <div className="stat-label">{label}</div>
    </div>
  );
}

export function StatusScreen() {
  const { conn } = useBackend();

  const [status, setStatus] = useState<StatusResponse | null>(null);
  const [stats, setStats] = useState<Statistics | null>(null);
  const [progress, setProgress] = useState<ProgressResponse | null>(null);
  const [recent, setRecent] = useState<RecentDoc[]>([]);
  const [error, setError] = useState<string | null>(null);
  const [actionMsg, setActionMsg] = useState<string | null>(null);

  const prevRunning = useRef(false);

  // Statistics are heavier (full DB scan) -> only on mount and after a sync finishes.
  const loadStats = useCallback(async () => {
    try {
      setStats(await getStatistics());
    } catch {
      /* keep last value */
    }
  }, []);

  // Status is the light poll; progress only while a sync runs.
  const loadStatus = useCallback(async () => {
    try {
      const s = await getStatus();
      setStatus(s);
      setError(null);
      if (s.is_running) {
        try {
          setProgress(await getProgress());
        } catch {
          /* progress is best-effort */
        }
      } else {
        setProgress(null);
      }
      try {
        setRecent((await getRecentIndexed(15)).documents);
      } catch {
        /* recent list is best-effort */
      }
      if (prevRunning.current && !s.is_running) void loadStats();
      prevRunning.current = s.is_running;
    } catch (e) {
      setError(e instanceof ApiError ? e.message : String(e));
    }
  }, [loadStats]);

  // Poll while the screen is open and the backend is reachable.
  useEffect(() => {
    if (conn !== "online") return;
    void loadStats();
    void loadStatus();
    const id = setInterval(() => void loadStatus(), 3500);
    return () => clearInterval(id);
  }, [conn, loadStatus, loadStats]);

  async function startSync() {
    setActionMsg(null);
    try {
      await refreshIndex();
      setActionMsg(labels.status.incSyncStarted);
      void loadStatus();
    } catch (e) {
      if (e instanceof ApiError && e.status === 409) {
        setActionMsg(labels.status.syncAlreadyRunning);
      } else {
        setActionMsg(e instanceof ApiError ? e.message : String(e));
      }
    }
  }

  const syncing = status?.is_running ?? false;
  const pct =
    progress && progress.total > 0
      ? Math.min(100, Math.round((progress.processed / progress.total) * 100))
      : 0;

  return (
    <div className="screen">
      <h1>{labels.nav.status}</h1>
      <p className="subtitle">{labels.status.subtitle}</p>

      {conn !== "online" && <Banner tone="err">{labels.common.backendUnreachable}</Banner>}
      {error && conn === "online" && (
        <Banner tone="err">{labels.status.loadError(error)}</Banner>
      )}

      {status && (
        <>
          <Section
            title={labels.status.overviewTitle}
            description={labels.status.overviewDesc}
            actions={
              syncing ? <Badge tone="ok">{labels.status.syncing}</Badge> : <Badge tone="muted">{labels.status.ready}</Badge>
            }
          >
            {stats && (
              <div className="stat-grid">
                <Stat value={fmtNum(stats.total_documents)} label={labels.status.statDocuments} />
                <Stat value={fmtNum(stats.indexed_documents)} label={labels.status.statIndexed} />
                <Stat value={fmtNum(stats.error_documents)} label={labels.status.statErrors} />
                <Stat value={fmtBytes(stats.total_size_bytes)} label={labels.status.statFileSize} />
                <Stat
                  value={fmtCompact(stats.total_content_chars)}
                  label={labels.status.statChars}
                  title={labels.status.statCharsTitle(fmtNum(stats.total_content_chars))}
                />
              </div>
            )}

            {syncing && progress && progress.total > 0 && (
              <div style={{ marginTop: 4 }}>
                <div className="progress">
                  <div className="progress-bar" style={{ width: `${pct}%` }} />
                </div>
                <p className="muted" style={{ marginTop: 6 }}>
                  {fmtNum(progress.processed)} / {fmtNum(progress.total)} ({pct}%)
                  {progress.current_file ? ` · ${progress.current_file}` : ""}
                </p>
              </div>
            )}

            {syncing && (!progress || progress.total === 0) && (
              <p className="muted" style={{ marginTop: 4 }}>
                {labels.status.scanning}
              </p>
            )}

            <Row>
              <Button onClick={() => void startSync()} disabled={syncing}>
                {syncing ? labels.status.indexingRunning : labels.status.updateIndex}
              </Button>
              {actionMsg && <span className="muted">{actionMsg}</span>}
            </Row>
            <p className="muted" style={{ marginTop: 8, fontSize: 12 }}>
              {labels.status.updateHint}
            </p>
          </Section>

          <Section title={labels.status.recentTitle} actions={<Badge tone="muted">{recent.length}</Badge>}>
            {recent.length === 0 && <p className="muted">{labels.status.noDocuments}</p>}
            {recent.map((d) => (
              <Row key={d.doc_id}>
                <span
                  style={{
                    flex: 1,
                    overflow: "hidden",
                    textOverflow: "ellipsis",
                    whiteSpace: "nowrap",
                  }}
                  title={d.file_path}
                >
                  📄 {d.file_name}
                </span>
                <Badge tone="muted">{d.file_type}</Badge>
                <span className="muted">{fmtTime(d.indexed_at)}</span>
              </Row>
            ))}
          </Section>

          <Section title={labels.status.foldersTitle} actions={<Badge tone="muted">{Object.keys(status.connectors).length}</Badge>}>
            {Object.keys(status.connectors).length === 0 && (
              <p className="muted">{labels.status.noFolders}</p>
            )}
            {Object.values(status.connectors).map((c) => (
              <div key={c.connector_name} className="screen-card">
                <Row>
                  <strong>{c.connector_name}</strong>
                  {c.is_syncing ? (
                    <Badge tone="ok">{labels.status.running}</Badge>
                  ) : c.documents_error > 0 ? (
                    <Badge tone="err">{labels.status.errorsCount(c.documents_error)}</Badge>
                  ) : (
                    <Badge tone="muted">{labels.status.ok}</Badge>
                  )}
                </Row>
                <span className="muted">
                  {labels.status.syncSummary(fmtNum(c.documents_indexed), fmtNum(c.documents_total), fmtDate(c.last_sync_at))}
                </span>
                {c.error_message && <Banner tone="err">{c.error_message}</Banner>}
              </div>
            ))}
          </Section>

          {stats && Object.keys(stats.file_types).length > 0 && (
            <Section title={labels.status.fileTypesTitle}>
              <div className="stat-grid">
                {Object.entries(stats.file_types)
                  .sort((a, b) => b[1] - a[1])
                  .map(([type, count]) => (
                    <Stat key={type} value={fmtNum(count)} label={type} />
                  ))}
              </div>
            </Section>
          )}
        </>
      )}
    </div>
  );
}
