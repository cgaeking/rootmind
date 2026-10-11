import { useEffect, useState } from "react";
import { labels } from "../i18n/labels";
import { useBackend } from "../hooks/useBackend";
import { getConfig, saveConfig } from "../api/rlm";
import { ApiError } from "../api/client";
import type { AppConfigDto, ConnectorDto } from "../types/config";
import { Banner, Button, Row, Section, TextInput } from "../components/ui";

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
  const { conn } = useBackend();

  const [config, setConfig] = useState<AppConfigDto | null>(null);
  const [entries, setEntries] = useState<Entry[]>([]);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  useEffect(() => {
    if (conn !== "online") return;
    let cancelled = false;
    getConfig()
      .then((c) => {
        if (cancelled) return;
        setConfig(c);
        setEntries(
          c.connectors
            .filter((x) => x.type === "local")
            .map((x) => ({ name: x.name, path: x.path ?? "" })),
        );
        setLoadError(null);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(errMsg(e));
      });
    return () => {
      cancelled = true;
    };
  }, [conn]);

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
    setSaving(true);
    setSaveError(null);
    setSaveMsg(null);
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
    } catch (e) {
      setSaveError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }

  return (
    <div className="screen">
      <h1>{labels.nav.folders}</h1>
      <p className="subtitle">{labels.folders.subtitle}</p>

      {conn !== "online" && <Banner tone="err">{labels.common.backendUnreachable}</Banner>}
      {loadError && (
        <Banner tone="err">{labels.folders.loadError(loadError)}</Banner>
      )}

      {config && (
        <Section title={labels.folders.title} description={labels.folders.desc}>
          {entries.length === 0 && (
            <p className="muted">{labels.folders.none}</p>
          )}
          {entries.map((e, i) => (
            <Row key={i}>
              <TextInput
                value={e.path}
                onChange={(ev) => setPath(i, ev.currentTarget.value)}
                placeholder={labels.folders.pathPlaceholder}
              />
              <Button onClick={() => removeEntry(i)} aria-label={labels.folders.removeAria}>
                ✕
              </Button>
            </Row>
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
      {saveError && <Banner tone="err">{labels.folders.saveFailed(saveError)}</Banner>}
    </div>
  );
}
