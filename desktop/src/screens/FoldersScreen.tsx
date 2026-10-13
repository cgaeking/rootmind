import { useEffect, useState } from "react";
import { labels, useLanguage } from "../i18n/labels";
import { isCurrentBackend, useLiveBackend } from "../hooks/useLiveBackend";
import { useStore } from "../state/store";
import { getConfig, getConnectors, saveConfig } from "../api/rlm";
import { ApiError } from "../api/client";
import { inTauri } from "../api/backend";
import type { AppConfigDto, ConnectorDto } from "../types/config";
import { Banner, Button, Row, Section, TextInput } from "../components/ui";
import { ConnBanner } from "../components/ConnBanner";
import { displayPath, samePath, storedPath } from "../utils/paths";

interface Entry {
  name: string; // preserved for existing folders; "" for new ones until saved
  path: string; // as stored (see utils/paths)
}

/** A folder path being edited: `index === entries.length` is a new row. */
interface Draft {
  index: number;
  text: string;
}

/** Why the last change was not saved, shown under folder `index` (null: below the list). */
interface Failure {
  index: number | null;
  message: string;
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

/** Give every folder a unique connector name; existing names are kept. */
function withNames(entries: Entry[]): Entry[] {
  const used = new Set<string>();
  return entries.map((e) => {
    let name = e.name || slugify(e.path);
    while (used.has(name)) name = `${name}_2`;
    used.add(name);
    return { ...e, name };
  });
}

function toConnectors(entries: Entry[]): ConnectorDto[] {
  // No include/exclude -> the folder inherits the global patterns.
  return entries.map((e) => ({ name: e.name, type: "local", path: e.path }));
}

/** A backend message that names a folder, with the path written the way the UI shows it. */
function withDisplayPath(message: string, path: string): string {
  return path ? message.split(path).join(displayPath(path)) : message;
}

export function FoldersScreen() {
  useLanguage();
  // The backend the loaded folders belong to (null while unreachable).
  const live = useLiveBackend();
  const kind = useStore((s) => s.kind);

  const [config, setConfig] = useState<AppConfigDto | null>(null);
  const [configUrl, setConfigUrl] = useState<string | null>(null);
  // The folders as saved in the backend.
  const [entries, setEntries] = useState<Entry[]>([]);
  const [draft, setDraft] = useState<Draft | null>(null);
  // Background service: folder name → why the service cannot use it.
  const [refused, setRefused] = useState<Record<string, string>>({});
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [failure, setFailure] = useState<Failure | null>(null);
  // Bumped after a save so the service's folder check is read again.
  const [saveCount, setSaveCount] = useState(0);

  // Another backend (or a lost connection) first clears what was loaded.
  useEffect(() => {
    setConfig(null);
    setConfigUrl(null);
    setEntries([]);
    setDraft(null);
    setLoadError(null);
    setSaveMsg(null);
    setFailure(null);
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

  /**
   * Save the folder list right away. If the backend refuses it (for example a
   * folder the background service may not read), the list goes back to what
   * it was and `failure` turns the reason into the message to show.
   */
  async function persist(next: Entry[], failure: (reason: string) => Failure) {
    if (!config) return;
    setSaveMsg(null);
    setFailure(null);
    // Never send one backend's folders to another one.
    if (!isCurrentBackend(configUrl)) {
      setFailure({ index: null, message: labels.folders.staleConfig });
      return;
    }
    const before = entries;
    const named = withNames(next);
    setEntries(named);
    setSaving(true);
    try {
      const res = await saveConfig({ ...config, connectors: toConnectors(named) });
      if (!isCurrentBackend(configUrl)) return;
      setSaveMsg(res.restart_required ? labels.folders.savedRestart : labels.folders.saved);
      setSaveCount((n) => n + 1);
    } catch (e) {
      if (!isCurrentBackend(configUrl)) return;
      setEntries(before);
      setFailure(failure(errMsg(e)));
    } finally {
      setSaving(false);
    }
  }

  function addFolder(path: string) {
    if (!path || entries.some((e) => samePath(e.path, path))) return;
    void persist([...entries, { name: "", path }], (reason) => ({
      index: null,
      message: labels.folders.notAdded(withDisplayPath(reason, path)),
    }));
  }

  function removeFolder(index: number) {
    const path = entries[index].path;
    void persist(
      entries.filter((_, j) => j !== index),
      (reason) => ({ index, message: labels.folders.notRemoved(withDisplayPath(reason, path)) }),
    );
  }

  /** Commit a typed path (on blur or Enter). */
  function commitDraft(index: number, text: string) {
    setDraft(null);
    const path = storedPath(text);
    if (index >= entries.length) {
      addFolder(path);
      return;
    }
    // An emptied or unchanged field keeps the folder; ✕ removes it.
    if (!path || samePath(path, entries[index].path)) return;
    void persist(
      entries.map((e, j) => (j === index ? { ...e, path } : e)),
      (reason) => ({ index, message: labels.folders.notChanged(withDisplayPath(reason, path)) }),
    );
  }

  async function pickFolder() {
    setSaveMsg(null);
    // Native folder picker in the app; in the dev browser preview fall back to
    // an editable empty row.
    if (inTauri()) {
      const { open } = await import("@tauri-apps/plugin-dialog");
      const result = await open({
        directory: true,
        multiple: false,
        title: labels.folders.pickerTitle,
      });
      if (typeof result === "string") addFolder(storedPath(result));
    } else {
      setDraft({ index: entries.length, text: "" });
    }
  }

  function folderRow(index: number, value: string) {
    const entry: Entry | undefined = entries[index];
    return (
      <div key={index} style={{ display: "flex", flexDirection: "column", gap: 6 }}>
        <Row>
          <TextInput
            value={value}
            onChange={(ev) => setDraft({ index, text: ev.currentTarget.value })}
            onBlur={(ev) => commitDraft(index, ev.currentTarget.value)}
            onKeyDown={(ev) => {
              if (ev.key === "Enter") ev.currentTarget.blur();
            }}
            placeholder={labels.folders.pathPlaceholder}
            disabled={saving}
            autoFocus={!entry}
            spellCheck={false}
          />
          {entry && (
            <Button
              onClick={() => removeFolder(index)}
              aria-label={labels.folders.removeAria}
              disabled={saving}
            >
              ✕
            </Button>
          )}
        </Row>
        {entry?.name && entry.name in refused && (
          <Banner tone="err">
            {labels.folders.notAccessible(withDisplayPath(refused[entry.name], entry.path))}
          </Banner>
        )}
        {failure?.index === index && <Banner tone="err">{failure.message}</Banner>}
      </div>
    );
  }

  const newRow = draft !== null && draft.index >= entries.length;

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
          {entries.length === 0 && !newRow && (
            <p className="muted">{labels.folders.none}</p>
          )}
          {entries.map((e, i) =>
            folderRow(i, draft?.index === i ? draft.text : displayPath(e.path)),
          )}
          {newRow && folderRow(entries.length, draft.text)}
          {failure && failure.index === null && <Banner tone="err">{failure.message}</Banner>}
          <Row>
            <Button onClick={() => void pickFolder()} disabled={saving || newRow}>
              {labels.folders.addFolder}
            </Button>
            {saving ? (
              <span className="muted">{labels.folders.saving}</span>
            ) : (
              saveMsg && <span className="muted">{saveMsg}</span>
            )}
          </Row>
        </Section>
      )}
    </div>
  );
}
