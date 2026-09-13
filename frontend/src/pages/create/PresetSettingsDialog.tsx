import { useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { useTranslation } from "react-i18next";

import { Btn, Chip, ConfirmDialog } from "../../components";
import type {
  SystemPresetInfo,
  SystemPresetInstallInput,
  SystemPresetInstallResult,
  SystemPresetSettings,
} from "../../lib/api";
import { api, ApiError } from "../../lib/api";
import type { ModelSource, ReasoningEffort } from "../../lib/models";
import {
  CUSTOM_MODEL_OPTION,
  isCustomModelId,
  modelOptionsFor,
  REASONING_EFFORTS,
  supportsReasoningEffort,
} from "../../lib/models";

const EFFORT_NONE = "none";
const MAX_TOKENS_CEILING = 131072;

// A managed KB offered by the catalog (only ACTIVE + MANAGED are selectable).
interface AttachableKb {
  kb_id: string;
  name: string;
  description?: string;
  status?: string;
  type?: string;
}

interface Form {
  model_source: ModelSource;
  model_id: string;
  /** empty string ⇒ no per-call ceiling (the knob is cleared) */
  max_tokens: string;
  reasoning_effort: ReasoningEffort | typeof EFFORT_NONE;
  system_prompt: string;
  max_iterations: string;
  timeout_seconds: string;
  knowledge_bases: SystemPresetSettings["knowledge_bases"];
}

function toForm(settings: SystemPresetSettings): Form {
  return {
    model_source: settings.model_source,
    model_id: settings.model_id,
    max_tokens: settings.max_tokens == null ? "" : String(settings.max_tokens),
    reasoning_effort: settings.reasoning_effort ?? EFFORT_NONE,
    system_prompt: settings.system_prompt,
    max_iterations: String(settings.max_iterations),
    timeout_seconds: String(settings.timeout_seconds),
    knowledge_bases: settings.knowledge_bases,
  };
}

function intOrNull(raw: string): number | null {
  const trimmed = raw.trim();
  if (trimmed === "") return null;
  return /^\d+$/.test(trimmed) ? Number(trimmed) : Number.NaN;
}

/**
 * The PARTIAL edit a save sends: only members that differ from what is stored, so
 * the server keeps everything else untouched. The two optional knobs are `clear`ed
 * (never silently dropped) when the form empties them or the model no longer
 * accepts a reasoning effort.
 */
function diffSettings(form: Form, stored: SystemPresetSettings): SystemPresetInstallInput {
  const body: SystemPresetInstallInput = {};
  const clear: ("max_tokens" | "reasoning_effort")[] = [];
  if (form.model_source !== stored.model_source) body.model_source = form.model_source;
  if (form.model_id.trim() !== stored.model_id) body.model_id = form.model_id.trim();
  const tokens = intOrNull(form.max_tokens);
  if (tokens === null) {
    if (stored.max_tokens != null) clear.push("max_tokens");
  } else if (tokens !== stored.max_tokens) {
    body.max_tokens = tokens;
  }
  const effortAllowed = supportsReasoningEffort(form.model_id.trim(), form.model_source);
  const effort = effortAllowed && form.reasoning_effort !== EFFORT_NONE ? form.reasoning_effort : null;
  if (effort === null) {
    if (stored.reasoning_effort != null) clear.push("reasoning_effort");
  } else if (effort !== stored.reasoning_effort) {
    body.reasoning_effort = effort;
  }
  if (form.system_prompt !== stored.system_prompt) body.system_prompt = form.system_prompt;
  const iterations = intOrNull(form.max_iterations);
  if (iterations !== null && iterations !== stored.max_iterations) body.max_iterations = iterations;
  const timeout = intOrNull(form.timeout_seconds);
  if (timeout !== null && timeout !== stored.timeout_seconds) body.timeout_seconds = timeout;
  const storedKbs = stored.knowledge_bases.map((kb) => kb.kb_id).sort().join(",");
  const formKbs = form.knowledge_bases.map((kb) => kb.kb_id).sort().join(",");
  if (storedKbs !== formKbs) body.knowledge_bases = form.knowledge_bases;
  if (clear.length) body.clear = clear;
  return body;
}

/** Client-side bounds mirror the server's (which stays authoritative). */
function validate(form: Form, t: (key: string, opts?: Record<string, unknown>) => string): string[] {
  const problems: string[] = [];
  if (!form.model_id.trim()) problems.push(t("create.system.settings.errors.model"));
  const tokens = intOrNull(form.max_tokens);
  if (Number.isNaN(tokens) || (tokens !== null && (tokens < 1 || tokens > MAX_TOKENS_CEILING))) {
    problems.push(t("create.system.settings.errors.maxTokens", { max: MAX_TOKENS_CEILING }));
  }
  const iterations = intOrNull(form.max_iterations);
  if (iterations === null || Number.isNaN(iterations) || iterations < 1 || iterations > 100) {
    problems.push(t("create.system.settings.errors.maxIterations"));
  }
  const timeout = intOrNull(form.timeout_seconds);
  if (timeout === null || Number.isNaN(timeout) || timeout < 10 || timeout > 3600) {
    problems.push(t("create.system.settings.errors.timeout"));
  }
  if (!form.system_prompt.trim()) problems.push(t("create.system.settings.errors.prompt"));
  return problems;
}

/**
 * Stored settings of one installed system preset. Administrators edit and save
 * (an explicit confirm, then the normal async update job through the maintenance
 * route); everyone else sees the same fields read-only. Opening, editing and
 * cancelling reach neither the backend nor AWS — only SAVE posts, and only the
 * members that changed.
 */
export function PresetSettingsDialog({
  preset,
  editable,
  readOnlyReason,
  onClose,
  onSaved,
  apiMessage,
}: {
  preset: SystemPresetInfo;
  /** the caller is an administrator and the preset is settled */
  editable: boolean;
  /** why the form is read-only (rendered for non-editable callers) */
  readOnlyReason?: string;
  onClose: () => void;
  /** the maintenance route accepted the edit (202 job / 200 already current) */
  onSaved: (result: SystemPresetInstallResult) => void;
  apiMessage: (err: unknown) => string;
}) {
  const { t } = useTranslation();
  const stored = preset.settings as SystemPresetSettings;
  const [form, setForm] = useState<Form>(() => toForm(stored));
  const [customModel, setCustomModel] = useState(() =>
    isCustomModelId(stored.model_id, stored.model_source),
  );
  const [kbCatalog, setKbCatalog] = useState<AttachableKb[] | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [errorDetails, setErrorDetails] = useState<string[]>([]);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Escape closes; the backdrop click too (same contract as ConfirmDialog).
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && !confirming) onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [confirming, onClose]);

  // The KB catalog is read only for an administrator who can actually change the
  // mounts; a failed or absent list leaves the stored chips as they are.
  useEffect(() => {
    if (!editable) return;
    let cancelled = false;
    fetch("/api/knowledge-bases")
      .then((res) => (res.ok ? res.json() : { items: [] }))
      .then((d: { items: AttachableKb[] }) => {
        if (!cancelled) setKbCatalog(d.items ?? []);
      })
      .catch(() => {
        if (!cancelled) setKbCatalog([]);
      });
    return () => {
      cancelled = true;
    };
  }, [editable]);

  const effortAllowed = supportsReasoningEffort(form.model_id.trim(), form.model_source);
  const body = useMemo(() => diffSettings(form, stored), [form, stored]);
  const changed = Object.keys(body).length > 0;
  const problems = useMemo(() => validate(form, t), [form, t]);
  const defaults = preset.defaults;
  const isDefault = (key: keyof SystemPresetSettings): boolean => {
    switch (key) {
      case "max_tokens":
        return intOrNull(form.max_tokens) === defaults.max_tokens;
      case "reasoning_effort":
        return (form.reasoning_effort === EFFORT_NONE ? null : form.reasoning_effort) ===
          defaults.reasoning_effort;
      case "max_iterations":
        return intOrNull(form.max_iterations) === defaults.max_iterations;
      case "timeout_seconds":
        return intOrNull(form.timeout_seconds) === defaults.timeout_seconds;
      case "knowledge_bases":
        return form.knowledge_bases.length === 0;
      default:
        return form[key] === defaults[key];
    }
  };
  const defaultHint = (key: keyof SystemPresetSettings) =>
    isDefault(key) ? null : (
      <span className="dim mono" style={{ fontSize: 10 }} data-testid={`differs-${key}`}>
        {" "}
        · {t("create.system.settings.differsFromDefault", {
          value:
            key === "knowledge_bases"
              ? t("create.system.settings.kbNoneShort")
              : key === "system_prompt"
                ? t("create.system.settings.buildPrompt")
                : String(defaults[key] ?? t("create.system.settings.effortNone")),
        })}
      </span>
    );

  const update = (patch: Partial<Form>) => {
    setError(null);
    setErrorDetails([]);
    setForm((prev) => ({ ...prev, ...patch }));
  };
  const applySource = (source: ModelSource) => {
    const options = modelOptionsFor(source);
    const keep = options.some((o) => o.model_id === form.model_id);
    update({ model_source: source, model_id: keep ? form.model_id : options[0].model_id });
    setCustomModel(false);
  };
  const useDefaults = () => {
    update(toForm({ ...defaults, knowledge_bases: form.knowledge_bases }));
    setCustomModel(isCustomModelId(defaults.model_id, defaults.model_source));
  };
  const toggleKb = (kb: AttachableKb) => {
    const present = form.knowledge_bases.some((k) => k.kb_id === kb.kb_id);
    update({
      knowledge_bases: present
        ? form.knowledge_bases.filter((k) => k.kb_id !== kb.kb_id)
        : [
            ...form.knowledge_bases,
            { kb_id: kb.kb_id, name: kb.name, description: kb.description ?? "" },
          ],
    });
  };

  const save = async () => {
    setConfirming(false);
    setSaving(true);
    setError(null);
    setErrorDetails([]);
    try {
      const result = await api.installSystemPreset(preset.key, body);
      if (!alive.current) return;
      onSaved(result);
    } catch (err) {
      if (!alive.current) return;
      setError(apiMessage(err));
      if (err instanceof ApiError) {
        const detail = err.detail as { errors?: { msg?: string; loc?: unknown[] }[] } | unknown[];
        const rows = Array.isArray(detail)
          ? detail
          : Array.isArray((detail as { errors?: unknown[] })?.errors)
            ? ((detail as { errors: unknown[] }).errors as { msg?: string; loc?: unknown[] }[])
            : [];
        setErrorDetails(
          rows
            .map((row) => {
              const r = row as { msg?: string; loc?: unknown[] };
              const loc = Array.isArray(r.loc) ? r.loc.filter((p) => p !== "body").join(".") : "";
              return [loc, r.msg].filter(Boolean).join(": ");
            })
            .filter(Boolean),
        );
      }
    } finally {
      if (alive.current) setSaving(false);
    }
  };

  const activeKbs = (kbCatalog ?? []).filter(
    (k) => k.status === "ACTIVE" && (k.type == null || k.type === "MANAGED"),
  );
  const disabled = !editable || saving;
  const fieldId = (name: string) => `preset-settings-${name}`;

  // Portaled to <body>: the panel sits inside a scrolling/stacking context, so a
  // fixed backdrop rendered in place would not cover the page (and the agents
  // table below would intercept clicks meant for the dialog).
  return createPortal(
    <div className="confirm-backdrop" onClick={onClose} data-testid="preset-settings-dialog">
      <div
        className="confirm-box preset-settings"
        role="dialog"
        aria-modal="true"
        aria-label={t("create.system.settings.title", { name: preset.label })}
        onClick={(e) => e.stopPropagation()}
      >
        <div className="confirm-title">◈ {t("create.system.settings.title", { name: preset.label })}</div>
        <p className="confirm-body dim" style={{ fontSize: 12 }}>
          {t("create.system.settings.sub")}
        </p>
        {!editable && (
          <div className="note" data-testid="preset-settings-readonly">
            <span className="i">[i]</span>
            <span>{readOnlyReason ?? t("create.system.settings.readOnly")}</span>
          </div>
        )}
        <div className="preset-settings-grid">
          <div className="field">
            <label>{t("create.system.settings.modelSource")}</label>
            <div className="selchips">
              {(["bedrock", "mantle"] as ModelSource[]).map((source) => (
                <button
                  key={source}
                  type="button"
                  className={`selchip${form.model_source === source ? " on" : ""}`}
                  style={{ cursor: disabled ? "default" : "pointer" }}
                  disabled={disabled}
                  data-testid={`preset-settings-source-${source}`}
                  onClick={() => applySource(source)}
                >
                  {t(
                    source === "mantle"
                      ? "create.configure.modelSourceMantle"
                      : "create.configure.modelSourceBedrock",
                  )}{" "}
                  {form.model_source === source ? "✓" : ""}
                </button>
              ))}
            </div>
          </div>
          <div className="field">
            <label htmlFor={fieldId("model")}>
              {t("create.system.settings.model")}
              {defaultHint("model_id")}
            </label>
            <select
              id={fieldId("model")}
              className="input"
              data-testid="preset-settings-model"
              disabled={disabled}
              value={customModel ? CUSTOM_MODEL_OPTION : form.model_id}
              onChange={(e) => {
                const picked = e.target.value;
                if (picked === CUSTOM_MODEL_OPTION) {
                  setCustomModel(true);
                  return;
                }
                setCustomModel(false);
                update({ model_id: picked });
              }}
            >
              {modelOptionsFor(form.model_source).map((option) => (
                <option key={option.model_id} value={option.model_id} style={{ background: "#141816" }}>
                  {option.label} · {option.model_id}
                </option>
              ))}
              <option value={CUSTOM_MODEL_OPTION} style={{ background: "#141816" }}>
                {t("create.configure.modelCustom")}
              </option>
            </select>
            {customModel && (
              <input
                id={fieldId("model-custom")}
                className="input mono"
                style={{ marginTop: 8 }}
                data-testid="preset-settings-model-custom"
                disabled={disabled}
                value={form.model_id}
                onChange={(e) => update({ model_id: e.target.value })}
              />
            )}
          </div>
          <div className="field">
            <label htmlFor={fieldId("max-tokens")}>
              {t("create.system.settings.maxTokens")}
              {defaultHint("max_tokens")}
            </label>
            <input
              id={fieldId("max-tokens")}
              className="input mono"
              inputMode="numeric"
              data-testid="preset-settings-max-tokens"
              disabled={disabled}
              placeholder={t("create.system.settings.maxTokensEmpty")}
              value={form.max_tokens}
              onChange={(e) => update({ max_tokens: e.target.value })}
            />
            <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>
              {t("create.system.settings.maxTokensHint")}
            </div>
          </div>
          <div className="field">
            <label htmlFor={fieldId("effort")}>
              {t("create.system.settings.effort")}
              {defaultHint("reasoning_effort")}
            </label>
            <select
              id={fieldId("effort")}
              className="input"
              data-testid="preset-settings-effort"
              disabled={disabled || !effortAllowed}
              value={effortAllowed ? form.reasoning_effort : EFFORT_NONE}
              onChange={(e) =>
                update({ reasoning_effort: e.target.value as Form["reasoning_effort"] })
              }
            >
              <option value={EFFORT_NONE} style={{ background: "#141816" }}>
                {t("create.system.settings.effortNone")}
              </option>
              {REASONING_EFFORTS.map((effort) => (
                <option key={effort} value={effort} style={{ background: "#141816" }}>
                  {t(`create.system.settings.effortLevels.${effort}`)}
                </option>
              ))}
            </select>
            <div className="dim" style={{ fontSize: 11, marginTop: 4 }} data-testid="preset-settings-effort-hint">
              {effortAllowed
                ? t("create.system.settings.effortHint")
                : t("create.system.settings.effortUnsupported")}
            </div>
          </div>
          <div className="field">
            <label htmlFor={fieldId("max-iterations")}>
              {t("create.system.settings.maxIterations")}
              {defaultHint("max_iterations")}
            </label>
            <input
              id={fieldId("max-iterations")}
              className="input mono"
              inputMode="numeric"
              data-testid="preset-settings-max-iterations"
              disabled={disabled}
              value={form.max_iterations}
              onChange={(e) => update({ max_iterations: e.target.value })}
            />
          </div>
          <div className="field">
            <label htmlFor={fieldId("timeout")}>
              {t("create.system.settings.timeout")}
              {defaultHint("timeout_seconds")}
            </label>
            <input
              id={fieldId("timeout")}
              className="input mono"
              inputMode="numeric"
              data-testid="preset-settings-timeout"
              disabled={disabled}
              value={form.timeout_seconds}
              onChange={(e) => update({ timeout_seconds: e.target.value })}
            />
          </div>
        </div>
        <div className="field">
          <label htmlFor={fieldId("prompt")}>
            {t("create.system.settings.systemPrompt")}
            {defaultHint("system_prompt")}
          </label>
          <textarea
            id={fieldId("prompt")}
            className="input mono"
            rows={8}
            data-testid="preset-settings-prompt"
            disabled={disabled}
            value={form.system_prompt}
            onChange={(e) => update({ system_prompt: e.target.value })}
          />
          {editable && form.system_prompt !== defaults.system_prompt && (
            <Btn
              className="small"
              style={{ marginTop: 6 }}
              data-testid="preset-settings-prompt-default"
              onClick={() => update({ system_prompt: defaults.system_prompt })}
            >
              {t("create.system.settings.usePromptDefault")}
            </Btn>
          )}
        </div>
        <div className="field" data-testid="preset-settings-kbs">
          <label>
            {t("create.system.settings.kb")}
            {defaultHint("knowledge_bases")}
          </label>
          <div className="selchips">
            {form.knowledge_bases.map((kb) => (
              <button
                key={kb.kb_id}
                type="button"
                className="selchip on"
                style={{ cursor: disabled ? "default" : "pointer" }}
                disabled={disabled}
                title={kb.description || kb.name}
                data-testid={`preset-settings-kb-${kb.kb_id}`}
                onClick={() => toggleKb(kb)}
              >
                {kb.name || kb.kb_id} · kb ✓
              </button>
            ))}
            {editable &&
              activeKbs
                .filter((kb) => !form.knowledge_bases.some((k) => k.kb_id === kb.kb_id))
                .map((kb) => (
                  <button
                    key={kb.kb_id}
                    type="button"
                    className="selchip"
                    style={{ cursor: "pointer" }}
                    disabled={saving}
                    title={kb.description || kb.name}
                    data-testid={`preset-settings-kb-${kb.kb_id}`}
                    onClick={() => toggleKb(kb)}
                  >
                    {kb.name} · kb +
                  </button>
                ))}
            {form.knowledge_bases.length === 0 && (!editable || activeKbs.length === 0) && (
              <span className="dim mono" style={{ fontSize: 11 }}>
                {t("create.system.settings.kbNone")}
              </span>
            )}
          </div>
        </div>
        <div className="dim" style={{ fontSize: 11 }}>{t("create.system.settings.loopNote")}</div>
        {editable && problems.length > 0 && (
          <div className="note" style={{ borderColor: "var(--crit)" }} data-testid="preset-settings-problems">
            <span className="i" style={{ color: "var(--crit)" }}>[!]</span>
            <span className="mono" style={{ fontSize: 11 }}>{problems.join(" · ")}</span>
          </div>
        )}
        {error && (
          <div className="note" style={{ borderColor: "var(--crit)" }} data-testid="preset-settings-error">
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span>
              {t("create.system.settings.saveFailed", { reason: error })}
              {errorDetails.length > 0 && (
                <ul className="mono" style={{ fontSize: 11, margin: "6px 0 0", paddingLeft: 16 }}>
                  {errorDetails.map((row, i) => (
                    <li key={i}>{row}</li>
                  ))}
                </ul>
              )}
            </span>
          </div>
        )}
        <div className="confirm-actions" style={{ flexWrap: "wrap" }}>
          <Btn onClick={onClose} data-testid="preset-settings-cancel">
            {editable ? t("common.cancel") : t("common.close")}
          </Btn>
          {editable && (
            <>
              <Btn onClick={useDefaults} disabled={saving} data-testid="preset-settings-defaults">
                {t("create.system.settings.useDefaults")}
              </Btn>
              <Btn
                primary
                data-testid="preset-settings-save"
                disabled={saving || !changed || problems.length > 0}
                disabledReason={
                  !changed ? t("create.system.settings.noChanges") : problems[0] ?? undefined
                }
                onClick={() => setConfirming(true)}
              >
                {saving ? t("create.system.settings.saving") : t("create.system.settings.save")}
              </Btn>
              {changed && (
                <Chip tone="amber" title={Object.keys(body).join(", ")}>
                  {t("create.system.settings.pending", { n: Object.keys(body).length })}
                </Chip>
              )}
            </>
          )}
        </div>
        <ConfirmDialog
          open={confirming}
          title={t("create.system.settings.confirmTitle")}
          body={t("create.system.settings.confirm", {
            name: preset.label,
            fields: Object.keys(body).join(", "),
          })}
          confirmLabel={t("create.system.settings.save")}
          onConfirm={() => void save()}
          onCancel={() => setConfirming(false)}
        />
      </div>
    </div>,
    document.body,
  );
}
