import { useEffect, useState } from "react";
import { labels } from "../i18n/labels";
import { isCurrentBackend, useLiveBackend } from "../hooks/useLiveBackend";
import { useStore } from "../state/store";
import { getConfig, getConnectors, saveConfig } from "../api/rlm";
import { ApiError } from "../api/client";
import type { AppConfigDto, ConnectorDto } from "../types/config";
import { Banner, Button, Row, Section, TextInput } from "../components/ui";
import { ConnBanner } from "../components/ConnBanner";

interface Entry {
  name: string; // preserved for existing folders; "" for new ones
  path: string;
}

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

/** Deterministic connector name derived from the path (shown on the status screen). */
function slugify(path: string): string {
  return (
    path
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "_")
      .replace(/^_+|_+$/g, "") || "folder"
  );
}

export function FoldersScreen() {
  // The backend the loaded folders belong to (null while unreachable).
  const live = useLiveBackend();
  const kind = useStore((s) => s.kind);

  const [config, setConfig] = useState<AppConfigDto | null>(null);
  const [configUrl, setConfigUrl] = useState<string | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  // Background service: folder name → why the service cannot use it.
  const [refused, setRefused] = useState<Record<string, string>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);
  // Bumped after a save so the service's folder check is read again.
  const [saveCount, setSaveCount] = useState(0);

  // Another backend (or a lost connection) first clears what was loaded.
  useEffect(() => {
    setConfig(null);
    setConfigUrl(null);
    setEntries([]);
    setLoadError(null);
    setSaveMsg(null);
    setSaveError(null);
    if (!live) return;
    let cancelled = false;
    getConfig()
      .then((c) => {
        if (cancelled) return;
        setConfig(c);
        setConfigUrl(live);
        setEntries(
          c.connectors
            .filter((x) => x.type === "local")
            .map((x) => ({ name: x.name, path: x.path ?? "" })),
        );
      })
      .catch((e) => {
        if (!cancelled) setLoadError(errMsg(e));
      });
    return () => {
      cancelled = true;
    };
  }, [live]);

  // The background service reads only what this account may read: mark the
  // folders it refused (GET /connectors lists them with available: false).
  useEffect(() => {
    setRefused({});
    if (!live || kind !== "service") return;
    let cancelled = false;
    getConnectors()
      .then((res) => {
        if (cancelled) return;
        const out: Record<string, string> = {};
        for (const c of res.connectors) {
          if (c.available === false) out[c.name] = c.error ?? "";
        }
        setRefused(out);
      })
      .catch(() => {
        /* the marks are best-effort */
      });
    return () => {
      cancelled = true;
    };
  }, [live, kind, saveCount]);

  function setPath(i: number, path: string) {
    setEntries((e) => e.map((x, j) => (j === i ? { ...x, path } : x)));
    setSaveMsg(null);
  }

  function removeEntry(i: number) {
    setEntries((e) => e.filter((_, j) => j !== i));
    setSaveMsg(null);
  }

  async function addEntry() {
    setSaveMsg(null);
    // Native folder picker in the app; in the dev browser preview fall back to
    // an editable empty row.
    if (typeof window !== "undefined" && "__TAURI_INTERNALS__" in window) {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const result = await open({
        directory: true,
        multiple: false,
        title: labels.folders.pickerTitle,
      });
      if (typeof result === "string") {
        // Normalize to forward slashes for consistency with existing entries
        // (Windows / pathlib accept both).
        const path = result.replace(/\\/g, "/");
        setEntries((e) => [...e, { name: "", path }]);
      }
    } else {
      setEntries((e) => [...e, { name: "", path: "" }]);
    }
  }

  async function save() {
    if (!config) return;
    setSaveError(null);
    setSaveMsg(null);
    // Never send one backend's folders to another one.
    if (!isCurrentBackend(configUrl)) {
      setSaveError(labels.folders.staleConfig);
      return;
    }
    setSaving(true);
    try {
      const used = new Set<string>();
      const connectors: ConnectorDto[] = entries
        .filter((e) => e.path.trim())
        .map((e) => {
          let name = e.name || slugify(e.path.trim());
          while (used.has(name)) name = `${name}_2`;
          used.add(name);
          // No include/exclude -> the folder inherits the global patterns.
          return { name, type: "local", path: e.path.trim() };
        });
      const res = await saveConfig({ ...config, connectors });
      setSaveMsg(res.restart_required ? labels.folders.savedRestart : labels.folders.saved);
      setSaveCount((n) => n + 1);
    } catch (e) {
      // A folder the background service refuses comes back as 400 with the reason.
      setSaveError(labels.folders.saveFailed(errMsg(e)));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="screen">
      <h1>{labels.nav.folders}</h1>
      <p className="subtitle">{labels.folders.subtitle}</p>

      <ConnBanner />
      {loadError && (
        <Banner tone="err">{labels.folders.loadError(loadError)}</Banner>
      )}

      {config && (
        <Section title={labels.folders.title} description={labels.folders.desc}>
          {entries.length === 0 && (
            <p className="muted">{labels.folders.none}</p>
          )}
          {entries.map((e, i) => (
            <div key={i} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
              <Row>
                <TextInput
                  value={e.path}
                  onChange={(ev) => setPath(i, ev.currentTarget.value)}
                  placeholder={labels.folders.pathPlaceholder}
                />
                <Button onClick={() => removeEntry(i)} aria-label={labels.folders.removeAria}>
                  ✕
                </Button>
              </Row>
              {e.name && e.name in refused && (
                <Banner tone="err">{labels.folders.notAccessible(refused[e.name])}</Banner>
              )}
            </div>
          ))}
          <Row>
            <Button onClick={() => void addEntry()}>{labels.folders.addFolder}</Button>
          </Row>
        </Section>
      )}

      {config && (
        <Row>
          <Button variant="primary" onClick={() => void save()} disabled={saving}>
            {saving ? labels.folders.saving : labels.common.save}
          </Button>
          {saveMsg && <span className="muted">{saveMsg}</span>}
        </Row>
      )}
      {saveError && <Banner tone="err">{saveError}</Banner>}
    </div>
  );
}
