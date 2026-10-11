import { useCallback, useEffect, useState } from "react";
import { labels } from "../i18n/labels";
import { useStore } from "../state/store";
import { useBackend } from "../hooks/useBackend";
import { getConfig, listLlmModels, saveConfig } from "../api/rlm";
import { ApiError } from "../api/client";
import type { ThemePref } from "../state/store";
import type { AppConfigDto, LlmProvider } from "../types/config";
import { Badge, Banner, Button, Field, Row, Section, Select, TextInput } from "../components/ui";

const PROVIDERS: LlmProvider[] = ["anthropic", "openai", "xai"];

function errMsg(e: unknown): string {
  return e instanceof ApiError ? e.message : String(e);
}

function csvToArr(text: string): string[] {
  return text
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Prefer a sensible default model over just the first in the list. */
function pickDefaultModel(models: string[]): string {
  return (
    models.find((m) => /sonnet/i.test(m)) ??
    models.find((m) => /gpt-4o/i.test(m)) ??
    models.find((m) => /gpt-4/i.test(m)) ??
    models.find((m) => /^grok-4(?!-fast)/i.test(m)) ??
    models[0]
  );
}

export function SettingsScreen() {
  const baseUrl = useStore((s) => s.baseUrl);
  const setBaseUrl = useStore((s) => s.setBaseUrl);
  const theme = useStore((s) => s.theme);
  const setTheme = useStore((s) => s.setTheme);
  const { conn, version, recheck } = useBackend();

  const [urlDraft, setUrlDraft] = useState(baseUrl);
  const [config, setConfig] = useState<AppConfigDto | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [models, setModels] = useState<string[] | null>(null);
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [replacedFrom, setReplacedFrom] = useState<string | null>(null);

  const loadModels = useCallback(
    async (provider: string, apiKey: string) => {
      if (!apiKey) return;
      setLoadingModels(true);
      setModelsError(null);
      try {
        const res = await listLlmModels({ provider, api_key: apiKey });
        setModels(res.models);
      } catch (e) {
        setModels([]);
        setModelsError(errMsg(e));
      } finally {
        setLoadingModels(false);
      }
    },
    [],
  );

  // Load the editable config once the backend is reachable, then fetch models
  // for the active provider if a key is present.
  useEffect(() => {
    if (conn !== "online") return;
    let cancelled = false;
    getConfig()
      .then((c) => {
        if (cancelled) return;
        setConfig(c);
        setLoadError(null);
        const key = (c.llm.api_keys ?? {})[c.llm.provider];
        if (key) {
          void loadModels(c.llm.provider, key);
        }
      })
      .catch((e) => {
        if (!cancelled) setLoadError(errMsg(e));
      });
    return () => {
      cancelled = true;
    };
  }, [conn, loadModels]);

  // If the saved model is not among the available ones (e.g. deprecated), switch to a valid one.
  useEffect(() => {
    if (!config) return;
    if (!models || models.length === 0) return;
    if (!models.includes(config.llm.model)) {
      setReplacedFrom(config.llm.model);
      updateLlm({ model: pickDefaultModel(models) });
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [models]);

  function applyUrl() {
    setBaseUrl(urlDraft.trim().replace(/\/+$/, ""));
  }

  function updateLlm(patch: Partial<AppConfigDto["llm"]>) {
    setConfig((c) => (c ? { ...c, llm: { ...c.llm, ...patch } } : c));
    setSaveMsg(null);
  }

  function updateIndexer(patch: Partial<AppConfigDto["indexer"]>) {
    setConfig((c) => (c ? { ...c, indexer: { ...c.indexer, ...patch } } : c));
    setSaveMsg(null);
  }

  function updateKey(value: string) {
    setConfig((c) =>
      c
        ? { ...c, llm: { ...c.llm, api_keys: { ...(c.llm.api_keys ?? {}), [c.llm.provider]: value } } }
        : c,
    );
    setSaveMsg(null);
  }

  function changeProvider(provider: LlmProvider) {
    if (!config) return;
    updateLlm({ provider });
    setModels(null);
    setModelsError(null);
    setReplacedFrom(null);
    const key = (config.llm.api_keys ?? {})[provider];
    if (key) {
      void loadModels(provider, key);
    }
  }

  async function save() {
    if (!config) return;
    setSaving(true);
    setSaveError(null);
    setSaveMsg(null);
    try {
      const res = await saveConfig(config);
      setReplacedFrom(null);
      setSaveMsg(res.restart_required ? labels.settings.savedRestart : labels.settings.saved);
    } catch (e) {
      setSaveError(errMsg(e));
    } finally {
      setSaving(false);
    }
  }

  const connBadge =
    conn === "online" ? (
      <Badge tone="ok">{labels.conn.online}</Badge>
    ) : conn === "offline" ? (
      <Badge tone="err">{labels.conn.offline}</Badge>
    ) : conn === "checking" ? (
      <Badge tone="muted">{labels.conn.checking}</Badge>
    ) : (
      <Badge tone="muted">{labels.conn.unknown}</Badge>
    );

  const currentKey = config ? ((config.llm.api_keys ?? {})[config.llm.provider] ?? "") : "";
  const modelOptions = models ?? (config?.llm.model ? [config.llm.model] : []);

  return (
    <div className="screen">
      <h1>{labels.nav.settings}</h1>
      <p className="subtitle">{labels.settings.subtitle}</p>

      <Section title={labels.settings.appearanceTitle} description={labels.settings.appearanceDesc}>
        <Field label={labels.settings.designLabel} hint={labels.settings.designHint}>

          <Select value={theme} onChange={(e) => setTheme(e.currentTarget.value as ThemePref)}>
            <option value="system">{labels.settings.themeSystem}</option>
            <option value="light">{labels.settings.themeLight}</option>
            <option value="dark">{labels.settings.themeDark}</option>
          </Select>
        </Field>
      </Section>

      <Section title={labels.settings.backendTitle} description={labels.settings.backendDesc} actions={connBadge}>
        <Field label={labels.settings.urlLabel} hint={labels.settings.urlHint}>
          <Row>
            <TextInput
              value={urlDraft}
              onChange={(e) => setUrlDraft(e.currentTarget.value)}
              placeholder="http://127.0.0.1:8000"
            />
            <Button onClick={applyUrl}>{labels.settings.apply}</Button>
            <Button onClick={() => void recheck()}>{labels.settings.check}</Button>
          </Row>
        </Field>
        {version && <span className="muted">{labels.settings.versionLabel(version)}</span>}
        {conn === "offline" && <Banner tone="err">{labels.common.backendUnreachable}</Banner>}
      </Section>

      <Section title={labels.settings.llmTitle} description={labels.settings.llmDesc}>
        {conn !== "online" && (
          <p className="muted">{labels.settings.connectPrompt}</p>
        )}
        {loadError && (
          <Banner tone="err">{labels.settings.loadConfigError(loadError)}</Banner>
        )}
        {conn === "online" && !config && !loadError && (
          <p className="muted">{labels.common.loading}</p>
        )}

        {config && (
          <>
            <Field label={labels.settings.providerLabel}>
              <Select
                value={config.llm.provider}
                onChange={(e) => changeProvider(e.currentTarget.value as LlmProvider)}
              >
                {PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {p}
                  </option>
                ))}
              </Select>
            </Field>

            <Field
              label={labels.settings.apiKeyLabel}
              hint={
                currentKey
                  ? labels.settings.apiKeyHintSaved(currentKey)
                  : labels.settings.apiKeyHintUnsaved
              }
            >
              <Row>
                <TextInput
                  type="password"
                  value={currentKey}
                  onChange={(e) => updateKey(e.currentTarget.value)}
                  // The field shows the masked stored key. Selecting it on focus
                  // makes a paste replace the mask instead of appending to it.
                  onFocus={(e) => e.currentTarget.select()}
                  placeholder="sk-…"
                />
                <Button
                  onClick={() => void loadModels(config.llm.provider, currentKey)}
                  disabled={loadingModels || !currentKey}
                >
                  {loadingModels ? labels.settings.loadingModels : labels.settings.loadModels}
                </Button>
              </Row>
            </Field>

            <Field
              label={labels.settings.modelLabel}
              hint={
                !currentKey
                  ? labels.settings.modelHintNoKey
                  : modelsError
                    ? undefined
                    : models
                      ? labels.settings.modelHintCount(models.length)
                      : labels.settings.modelHintLoading
              }
            >
              <Select
                value={config.llm.model}
                onChange={(e) => updateLlm({ model: e.currentTarget.value })}
                disabled={!currentKey || !models}
              >
                {modelOptions.map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </Select>
            </Field>

            {modelsError && (
              <Banner tone="err">
                {labels.settings.modelsError(modelsError)}
              </Banner>
            )}
            {replacedFrom && replacedFrom !== config.llm.model && (
              <Banner tone="info">
                {labels.settings.modelReplaced(replacedFrom, config.llm.model)}
              </Banner>
            )}
          </>
        )}
      </Section>

      {config && (
        <Section
          title={labels.settings.indexerTitle}
          description={labels.settings.indexerDesc}
        >
          <Field
            label={labels.settings.intervalLabel}
            hint={labels.settings.intervalHint}
          >
            <TextInput
              type="number"
              min={1}
              value={config.indexer.sync_interval_hours}
              onChange={(e) => {
                const n = parseInt(e.currentTarget.value, 10);
                if (!Number.isNaN(n) && n > 0) updateIndexer({ sync_interval_hours: n });
              }}
            />
          </Field>
          <Field label={labels.settings.includeLabel} hint={labels.settings.includeHint}>
            <TextInput
              value={(config.indexer.include ?? []).join(", ")}
              onChange={(e) => updateIndexer({ include: csvToArr(e.currentTarget.value) })}
            />
          </Field>
          <Field label={labels.settings.excludeLabel} hint={labels.settings.excludeHint}>
            <TextInput
              value={(config.indexer.exclude ?? []).join(", ")}
              onChange={(e) => updateIndexer({ exclude: csvToArr(e.currentTarget.value) })}
            />
          </Field>
        </Section>
      )}

      {config && (
        <Row>
          <Button variant="primary" onClick={() => void save()} disabled={saving}>
            {saving ? labels.settings.saving : labels.common.save}
          </Button>
          {saveMsg && <span className="muted">{saveMsg}</span>}
        </Row>
      )}
      {saveError && <Banner tone="err">{labels.settings.saveFailed(saveError)}</Banner>}
    </div>
  );
}
