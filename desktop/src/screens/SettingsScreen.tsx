import { useCallback, useEffect, useState } from "react";
import { labels, useLanguage, useLanguageStore } from "../i18n/labels";
import type { LanguagePref } from "../i18n/labels";
import { DEFAULT_BASE_URL, useStore } from "../state/store";
import { useBackend } from "../hooks/useBackend";
import { isCurrentBackend, useLiveBackend } from "../hooks/useLiveBackend";
import { connectCustom, switchToDefaultBackend } from "../state/backendActions";
import { getConfig, listLlmModels, saveConfig } from "../api/rlm";
import { ApiError } from "../api/client";
import type { ThemePref } from "../state/store";
import type { AppConfigDto, LlmProvider } from "../types/config";
import { Banner, Button, Field, Row, Section, Select, TextInput } from "../components/ui";
import { ConnBanner } from "../components/ConnBanner";
import { BackgroundServiceSection } from "./BackgroundServiceSection";
import { AboutSection } from "./AboutSection";
import { PROVIDERS, PROVIDER_NAMES, detectProvider, maskKey } from "../state/llmKey";

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

/** Settings → Advanced: a custom backend URL and token, or back to the default backend. */
function AdvancedSection() {
  const { kind, baseUrl, version, recheck } = useBackend();
  const token = useStore((s) => s.token);
  const customUrl = kind === "custom" ? baseUrl : "";
  const customToken = kind === "custom" ? (token ?? "") : "";

  // The fields follow the store: the backend dialog can change the URL too.
  const [urlDraft, setUrlDraft] = useState(customUrl);
  const [tokenDraft, setTokenDraft] = useState(customToken);
  useEffect(() => setUrlDraft(customUrl), [customUrl]);
  useEffect(() => setTokenDraft(customToken), [customToken]);

  const inUse =
    kind === "builtin"
      ? labels.settings.inUse.builtin
      : kind === "service"
        ? labels.settings.inUse.service
        : kind === "custom"
          ? labels.settings.inUse.custom(baseUrl)
          : labels.settings.inUse.none;

  return (
    <Section title={labels.settings.advancedTitle} description={labels.settings.advancedDesc}>
      <span className="muted">
        {inUse}
        {version && <> · {labels.settings.versionLabel(version)}</>}
      </span>
      <Field label={labels.settings.urlLabel} hint={labels.settings.urlHint}>
        <TextInput
          value={urlDraft}
          onChange={(e) => setUrlDraft(e.currentTarget.value)}
          placeholder={DEFAULT_BASE_URL}
          spellCheck={false}
        />
      </Field>
      <Field label={labels.settings.tokenLabel} hint={labels.settings.tokenHint}>
        <TextInput
          type="password"
          value={tokenDraft}
          onChange={(e) => setTokenDraft(e.currentTarget.value)}
          spellCheck={false}
          autoComplete="off"
        />
      </Field>
      <Row>
        <Button onClick={() => void connectCustom(urlDraft || DEFAULT_BASE_URL, tokenDraft)}>
          {labels.settings.apply}
        </Button>
        <Button onClick={() => void recheck()}>{labels.settings.check}</Button>
        <Button onClick={() => void switchToDefaultBackend()} title={labels.settings.useDefaultHint}>
          {labels.settings.useDefault}
        </Button>
      </Row>
    </Section>
  );
}

export function SettingsScreen() {
  useLanguage();
  const languagePref = useLanguageStore((s) => s.pref);
  const setLanguagePref = useLanguageStore((s) => s.setPref);
  const theme = useStore((s) => s.theme);
  const setTheme = useStore((s) => s.setTheme);
  const { conn } = useBackend();
  // The backend the loaded settings belong to (null while unreachable).
  const live = useLiveBackend();

  const [config, setConfig] = useState<AppConfigDto | null>(null);
  const [configUrl, setConfigUrl] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [saveError, setSaveError] = useState<string | null>(null);

  const [models, setModels] = useState<string[] | null>(null);
  const [loadingModels, setLoadingModels] = useState(false);
  const [modelsError, setModelsError] = useState<string | null>(null);
  const [replacedFrom, setReplacedFrom] = useState<string | null>(null);

  // The API key field: null shows the saved (masked) key of the provider,
  // a string is a key typed but not saved yet. `config.llm.api_keys` holds
  // only saved keys, as masks.
  const [keyDraft, setKeyDraft] = useState<string | null>(null);
  // The provider preselected from the typed key's format, until overridden.
  const [detected, setDetected] = useState<LlmProvider | null>(null);
  const [savingKey, setSavingKey] = useState(false);
  const [keyMsg, setKeyMsg] = useState<string | null>(null);
  const [keyError, setKeyError] = useState<string | null>(null);

  // Results for a backend that is no longer in use are dropped.
  const loadModels = useCallback(async (provider: string, apiKey: string) => {
    if (!apiKey) return;
    const from = useStore.getState().baseUrl;
    const stillCurrent = () => useStore.getState().baseUrl === from;
    setLoadingModels(true);
    setModelsError(null);
    try {
      const res = await listLlmModels({ provider, api_key: apiKey });
      if (stillCurrent()) setModels(res.models);
    } catch (e) {
      if (stillCurrent()) {
        setModels([]);
        setModelsError(errMsg(e));
      }
    } finally {
      if (stillCurrent()) setLoadingModels(false);
    }
  }, []);

  // Load the editable config from the live backend, then fetch models for the
  // active provider if a key is present. Another backend (or a lost
  // connection) first clears everything loaded from the previous one.
  useEffect(() => {
    setConfig(null);
    setConfigUrl(null);
    setLoadError(null);
    setSaveMsg(null);
    setSaveError(null);
    setModels(null);
    setModelsError(null);
    setLoadingModels(false);
    setReplacedFrom(null);
    setKeyDraft(null);
    setDetected(null);
    setSavingKey(false);
    setKeyMsg(null);
    setKeyError(null);
    if (!live) return;
    let cancelled = false;
    getConfig()
      .then((c) => {
        if (cancelled) return;
        setConfig(c);
        setConfigUrl(live);
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
  }, [live, loadModels]);

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

  function updateLlm(patch: Partial<AppConfigDto["llm"]>) {
    setConfig((c) => (c ? { ...c, llm: { ...c.llm, ...patch } } : c));
    setSaveMsg(null);
  }

  function updateIndexer(patch: Partial<AppConfigDto["indexer"]>) {
    setConfig((c) => (c ? { ...c, indexer: { ...c.indexer, ...patch } } : c));
    setSaveMsg(null);
  }

  /** Switch the provider and load its models if a key is saved for it. */
  function selectProvider(provider: LlmProvider) {
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

  /** The user picked a provider: this also overrides a detected one. */
  function changeProvider(provider: LlmProvider) {
    selectProvider(provider);
    setDetected(null);
    setKeyMsg(null);
    setKeyError(null);
  }

  /** A key was typed or pasted: preselect the provider its format names. */
  function changeKey(value: string) {
    if (!config) return;
    setKeyDraft(value);
    setKeyMsg(null);
    setKeyError(null);
    const provider = detectProvider(value);
    if (provider && provider !== config.llm.provider) {
      selectProvider(provider);
      setDetected(provider);
    } else if (provider !== detected) {
      setDetected(null);
    }
  }

  /**
   * Save the typed key for the selected provider. A key is first checked by
   * loading the provider's models with it, so an invalid key shows the
   * provider's error and changes nothing. A valid one is saved together with
   * the selected provider and a model from the returned list (the selected
   * one if listed, else the first), so no further Save is needed, and fills
   * the model list. An emptied field removes only the saved key.
   */
  async function saveKey() {
    if (!config || keyDraft === null) return;
    const provider = config.llm.provider;
    const key = keyDraft.trim();
    setKeyMsg(null);
    setKeyError(null);
    // Never send a key to a backend other than the one the settings came from.
    if (!isCurrentBackend(configUrl)) {
      setKeyError(labels.settings.staleConfig);
      return;
    }
    setSavingKey(true);
    try {
      const listed = key ? (await listLlmModels({ provider, api_key: key })).models : null;
      // The masks keep the other providers' keys. A new key also makes its
      // provider and a listed model the active ones; removing a key changes
      // only the keys. Everything else keeps its stored value.
      const apiKeys = { ...(config.llm.api_keys ?? {}), [provider]: key };
      const model =
        listed && !listed.includes(config.llm.model) && listed.length > 0
          ? listed[0]
          : config.llm.model;
      await saveConfig({
        llm: listed ? { provider, model, api_keys: apiKeys } : { api_keys: apiKeys },
      });
      if (!isCurrentBackend(configUrl)) return;
      // The backend's masks for the saved keys (a local mask if it does not answer).
      let saved: Record<string, string>;
      try {
        saved = (await getConfig()).llm.api_keys ?? {};
      } catch {
        saved = Object.fromEntries(
          Object.entries(apiKeys)
            .filter(([, v]) => v)
            .map(([p, v]) => [p, p === provider ? maskKey(v) : v]),
        );
      }
      if (!isCurrentBackend(configUrl)) return;
      setConfig((c) =>
        c ? { ...c, llm: { ...c.llm, api_keys: saved, ...(listed ? { model } : {}) } } : c,
      );
      setReplacedFrom(null);
      setKeyDraft(null);
      setDetected(null);
      setModels(listed);
      setModelsError(null);
      setKeyMsg(key ? labels.settings.keySaved : labels.settings.keyRemoved);
    } catch (e) {
      if (isCurrentBackend(configUrl)) setKeyError(labels.settings.keyRejected(errMsg(e)));
    } finally {
      setSavingKey(false);
    }
  }

  async function save() {
    if (!config) return;
    setSaveError(null);
    setSaveMsg(null);
    // Never send one backend's settings to another one.
    if (!isCurrentBackend(configUrl)) {
      setSaveError(labels.settings.staleConfig);
      return;
    }
    setSaving(true);
    try {
      const res = await saveConfig(config);
      setReplacedFrom(null);
      setSaveMsg(res.restart_required ? labels.settings.savedRestart : labels.settings.saved);
    } catch (e) {
      setSaveError(labels.settings.saveFailed(errMsg(e)));
    } finally {
      setSaving(false);
    }
  }

  // The saved key (masked) of the selected provider, and what the field shows.
  const savedKey = config ? ((config.llm.api_keys ?? {})[config.llm.provider] ?? "") : "";
  const keyValue = keyDraft ?? savedKey;
  const keyChanged =
    keyDraft !== null && keyDraft.trim() !== savedKey && (keyDraft.trim() !== "" || savedKey !== "");
  const modelOptions = models ?? (config?.llm.model ? [config.llm.model] : []);

  return (
    <div className="screen">
      <h1>{labels.nav.settings}</h1>
      <p className="subtitle">{labels.settings.subtitle}</p>

      <ConnBanner />

      <Section title={labels.settings.appearanceTitle} description={labels.settings.appearanceDesc}>
        <Field label={labels.settings.designLabel} hint={labels.settings.designHint}>

          <Select value={theme} onChange={(e) => setTheme(e.currentTarget.value as ThemePref)}>
            <option value="system">{labels.settings.themeSystem}</option>
            <option value="light">{labels.settings.themeLight}</option>
            <option value="dark">{labels.settings.themeDark}</option>
          </Select>
        </Field>
        <Field label={labels.settings.languageLabel} hint={labels.settings.languageHint}>
          <Select
            value={languagePref}
            onChange={(e) => setLanguagePref(e.currentTarget.value as LanguagePref)}
          >
            <option value="system">{labels.settings.languageSystem}</option>
            <option value="en">{labels.settings.languageEn}</option>
            <option value="de">{labels.settings.languageDe}</option>
          </Select>
        </Field>
      </Section>

      <BackgroundServiceSection />

      <Section title={labels.settings.llmTitle} description={labels.settings.llmDesc}>
        {conn !== "online" && (
          <p className="muted">{labels.settings.connectPrompt}</p>
        )}
        {loadError && (
          <Banner tone="err">{labels.settings.loadConfigError(loadError)}</Banner>
        )}
        {live && !config && !loadError && (
          <p className="muted">{labels.common.loading}</p>
        )}

        {config && (
          <>
            <Field
              label={labels.settings.providerLabel}
              hint={
                detected && detected === config.llm.provider
                  ? labels.settings.providerDetected(PROVIDER_NAMES[detected])
                  : undefined
              }
            >
              <Select
                value={config.llm.provider}
                onChange={(e) => changeProvider(e.currentTarget.value as LlmProvider)}
                disabled={savingKey}
              >
                {PROVIDERS.map((p) => (
                  <option key={p} value={p}>
                    {PROVIDER_NAMES[p]}
                  </option>
                ))}
              </Select>
            </Field>

            <Field
              label={labels.settings.apiKeyLabel}
              hint={
                savedKey
                  ? labels.settings.apiKeyHintSaved(savedKey)
                  : labels.settings.apiKeyHintUnsaved
              }
            >
              <Row>
                <TextInput
                  type="password"
                  value={keyValue}
                  onChange={(e) => changeKey(e.currentTarget.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter" && keyChanged && !savingKey) void saveKey();
                  }}
                  // The field shows the masked stored key. Selecting it on focus
                  // makes a paste replace the mask instead of appending to it.
                  onFocus={(e) => e.currentTarget.select()}
                  placeholder="sk-…"
                  disabled={savingKey}
                  spellCheck={false}
                  autoComplete="off"
                />
                <Button
                  onClick={() => void saveKey()}
                  disabled={savingKey || !keyChanged}
                  aria-label={labels.settings.saveKeyAria}
                >
                  {savingKey ? labels.settings.saving : labels.common.save}
                </Button>
              </Row>
            </Field>
            {keyMsg && <span className="muted">{keyMsg}</span>}
            {keyError && <Banner tone="err">{keyError}</Banner>}

            <Field
              label={labels.settings.modelLabel}
              hint={
                !savedKey
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
                disabled={!savedKey || !models || loadingModels || savingKey}
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
      {saveError && <Banner tone="err">{saveError}</Banner>}

      <div style={{ marginTop: 18 }}>
        <AdvancedSection />
      </div>

      <AboutSection />
    </div>
  );
}
