import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  type ModelCapabilityFields,
  type ModelProviderOut,
  type ModelRowCreateBody,
  type ModelRowKind,
  type ModelRowOut,
  type ModelRowPatchBody,
  errorMessage,
} from "../../../lib/api";
import { invalidateCapabilities } from "../../../lib/capabilities";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Drawer, Field, Select, Tag } from "../../ui";
import { fieldSource, intOrNull, isProbeFailed, isProbeUnavailable, MODEL_KINDS, SOURCE_TONE } from "./common";
import { ProbeFindings } from "./ProbeDialog";

interface FormState {
  api_model_name: string;
  kind: ModelRowKind;
  label: string;
  probe: boolean;
  enabled: boolean;
  tools: boolean;
  streaming: boolean;
  vision: boolean;
  temperature: boolean;
  top_p: boolean;
  default_max_tokens: string;
  max_output_tokens: string;
  default_judge_max_tokens: string;
  embedding_dimension: string;
  embedding_max_chars: string;
}

function fromModel(m: ModelRowOut | null, kind: ModelRowKind): FormState {
  return {
    api_model_name: m?.api_model_name ?? "",
    kind: m?.kind ?? kind,
    label: m?.label ?? "",
    probe: true, // probe-on-add default on (user ruling §12.1-5)
    enabled: m?.enabled ?? true,
    tools: m?.tools ?? true,
    streaming: m?.streaming ?? true,
    vision: m?.vision ?? false,
    temperature: m?.inference_params?.temperature ?? true,
    top_p: m?.inference_params?.top_p ?? true,
    default_max_tokens: m?.default_max_tokens != null ? String(m.default_max_tokens) : "",
    max_output_tokens: m?.max_output_tokens != null ? String(m.max_output_tokens) : "",
    default_judge_max_tokens: m?.default_judge_max_tokens != null ? String(m.default_judge_max_tokens) : "",
    embedding_dimension: m?.embedding_dimension != null ? String(m.embedding_dimension) : "",
    embedding_max_chars: m?.embedding_max_chars != null ? String(m.embedding_max_chars) : "",
  };
}

/** The capability fields the form carries, as the API body shape. */
function capabilities(form: FormState): Partial<ModelCapabilityFields> {
  if (form.kind === "embedding") {
    return {
      embedding_dimension: intOrNull(form.embedding_dimension),
      embedding_max_chars: intOrNull(form.embedding_max_chars),
    };
  }
  const out: Partial<ModelCapabilityFields> = {
    tools: form.tools,
    streaming: form.streaming,
    vision: form.vision,
    inference_params: { temperature: form.temperature, top_p: form.top_p },
    max_output_tokens: intOrNull(form.max_output_tokens),
    default_judge_max_tokens: intOrNull(form.default_judge_max_tokens),
  };
  const dflt = intOrNull(form.default_max_tokens);
  if (dflt !== null) out.default_max_tokens = dflt;
  return out;
}

/** Only the fields the admin changed go into a PUT (each one flips its source to `manual`). */
function changedCapabilities(form: FormState, m: ModelRowOut): Partial<ModelCapabilityFields> {
  const next = capabilities(form);
  const out: Partial<ModelCapabilityFields> = {};
  if (next.tools !== undefined && next.tools !== m.tools) out.tools = next.tools;
  if (next.streaming !== undefined && next.streaming !== m.streaming) out.streaming = next.streaming;
  if (next.vision !== undefined && next.vision !== m.vision) out.vision = next.vision;
  if (
    next.inference_params &&
    (next.inference_params.temperature !== (m.inference_params?.temperature ?? true) ||
      next.inference_params.top_p !== (m.inference_params?.top_p ?? true))
  ) {
    out.inference_params = next.inference_params;
  }
  for (const key of ["default_max_tokens", "max_output_tokens", "default_judge_max_tokens", "embedding_dimension", "embedding_max_chars"] as const) {
    if (key in next && (next[key] ?? null) !== (m[key] ?? null)) (out as Record<string, unknown>)[key] = next[key] ?? null;
  }
  return out;
}

/**
 * Add / edit a model row (plan §8). Add: API model name (datalist from the provider's
 * last probe), kind, *Probe on add* (default on) — the probe runs server-side before
 * insert; a failed `exists` is a 422 the drawer shows with a *Save without probe* way out.
 * Edit: every capability with its `source` badge and *Reset to probed*.
 */
export function ModelDrawer({
  open,
  provider,
  model,
  onClose,
  onSaved,
}: {
  open: boolean;
  provider: ModelProviderOut;
  /** null = add */
  model: ModelRowOut | null;
  onClose: () => void;
  onSaved: (m: ModelRowOut) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const editing = model !== null;
  const [form, setForm] = useState<FormState>(() => fromModel(model, "chat"));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [probeFailure, setProbeFailure] = useState<unknown>(null);
  const [touched, setTouched] = useState(false);
  const set = <K extends keyof FormState>(key: K, value: FormState[K]) => setForm((prev) => ({ ...prev, [key]: value }));

  const nameError = !form.api_model_name.trim() ? t("modelProviders.validation.required") : null;
  const dimError = form.kind === "embedding" && !form.probe && !editing && intOrNull(form.embedding_dimension) === null ? t("modelProviders.validation.required") : null;
  const valid = !nameError && !dimError;
  const modelId = `${provider.id_prefix}${form.api_model_name.trim()}`;

  const srcTag = (key: string) => {
    if (!model) return null;
    const source = fieldSource(model, key as never);
    return (
      <Tag tone={SOURCE_TONE[source]} title={t("modelProviders.fieldSourceHint")}>
        {t(`modelProviders.source.${source}`)}
      </Tag>
    );
  };

  const resetToProbed = () => {
    const f = model?.probe?.findings;
    if (!f) return;
    setForm((prev) => ({
      ...prev,
      tools: f.tools ?? prev.tools,
      streaming: f.streaming ?? prev.streaming,
      temperature: f.temperature ?? prev.temperature,
      top_p: f.top_p ?? prev.top_p,
      embedding_dimension: f.embedding_dimension != null ? String(f.embedding_dimension) : prev.embedding_dimension,
    }));
  };

  const submit = async (forceNoProbe = false) => {
    setTouched(true);
    if (!valid) return;
    setBusy(true);
    setError(null);
    try {
      let saved: ModelRowOut;
      if (editing) {
        const body: ModelRowPatchBody = { ...changedCapabilities(form, model) };
        if (form.label.trim() && form.label.trim() !== model.label) body.label = form.label.trim();
        if (form.enabled !== model.enabled) body.enabled = form.enabled;
        saved = await api.modelProviders.updateModel(provider.id, model.id, body);
      } else {
        const probe = form.probe && !forceNoProbe;
        const body: ModelRowCreateBody = {
          api_model_name: form.api_model_name.trim(),
          kind: form.kind,
          probe,
        };
        if (form.label.trim()) body.label = form.label.trim();
        // without a probe the admin's values are the row (`source: manual`); with one they
        // are overrides applied after the probed values
        const overrides = capabilities(form);
        if (!probe || form.kind === "embedding" || form.vision || overrides.max_output_tokens || overrides.default_judge_max_tokens || overrides.default_max_tokens) {
          body.overrides = probe ? pickOverrides(overrides, form) : overrides;
        }
        saved = await api.modelProviders.addModel(provider.id, body);
      }
      invalidateCapabilities();
      toast("success", t(editing ? "modelProviders.toast.modelSaved" : "modelProviders.toast.modelCreated", { label: saved.label }));
      onSaved(saved);
    } catch (err) {
      if (isProbeFailed(err)) setProbeFailure(err);
      else if (isProbeUnavailable(err)) setError(t("modelProviders.probe.unavailable"));
      else setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const chat = form.kind === "chat";
  return (
    <Drawer
      open={open}
      title={editing ? t("modelProviders.drawer.editModel", { label: model.label }) : t("modelProviders.drawer.addModel", { provider: provider.label })}
      onClose={onClose}
      testId="v2-mp-model-drawer"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          {probeFailure !== null && (
            <Button disabled={busy} onClick={() => void submit(true)} testId="v2-mp-model-save-noprobe">
              {t("modelProviders.action.saveWithoutProbe")}
            </Button>
          )}
          <Button kind="primary" disabled={busy || (touched && !valid)} onClick={() => void submit()} testId="v2-mp-model-save">
            {!editing && form.probe ? t("modelProviders.action.probeAndSave") : t("v2.common.save")}
          </Button>
        </>
      }
    >
      <div className="v2-mp-form">
        {error && <Alert tone="error">{error}</Alert>}
        {probeFailure !== null && (
          <Alert tone="error">
            <div>{t("modelProviders.probe.addFailed")}</div>
            <ProbeFindings error={probeFailure} />
          </Alert>
        )}
        <div className="cols">
          <Field label={t("modelProviders.field.apiModelName")} required error={touched ? nameError : null} hint={!editing ? t("modelProviders.hint.launchpadId", { id: modelId }) : undefined}>
            <input
              className="v2-input"
              list={editing ? undefined : "v2-mp-models-seen"}
              value={form.api_model_name}
              disabled={editing}
              placeholder="Qwen/Qwen3-8B"
              onChange={(e) => set("api_model_name", e.target.value)}
              data-testid="v2-mp-model-name"
            />
            {!editing && provider.probe?.models_seen?.length > 0 && (
              <datalist id="v2-mp-models-seen">
                {provider.probe.models_seen.map((id) => (
                  <option key={id} value={id} />
                ))}
              </datalist>
            )}
          </Field>
          <Field label={t("modelProviders.col.kind")} required>
            <Select
              value={form.kind}
              disabled={editing}
              options={MODEL_KINDS.map((kind) => ({ value: kind, label: t(`modelProviders.modelKind.${kind}`) }))}
              onChange={(value) => set("kind", value as ModelRowKind)}
              testId="v2-mp-model-kind"
            />
          </Field>
        </div>
        <Field label={t("modelProviders.field.label")}>
          <input className="v2-input" value={form.label} placeholder={form.api_model_name} onChange={(e) => set("label", e.target.value)} data-testid="v2-mp-model-label" />
        </Field>
        {!editing && (
          <div className="v2-checks">
            <label className="v2-check">
              <input type="checkbox" checked={form.probe} onChange={(e) => set("probe", e.target.checked)} data-testid="v2-mp-model-probe-on-add" />
              {t("modelProviders.field.probeOnAdd")}
            </label>
            <span className="v2-muted" style={{ fontSize: 12 }}>{t("modelProviders.hint.probeOnAdd")}</span>
          </div>
        )}
        {chat ? (
          <>
            <div className="v2-checks">
              {(["tools", "streaming", "vision", "temperature", "top_p"] as const).map((key) => (
                <label className="v2-check" key={key}>
                  <input type="checkbox" checked={form[key]} onChange={(e) => set(key, e.target.checked)} data-testid={`v2-mp-model-${key}`} />
                  {t(`modelProviders.cap.${key}`)}
                  <span className="src">{srcTag(key)}</span>
                </label>
              ))}
            </div>
            <div className="cols">
              <Field label={<>{t("modelProviders.cap.default_max_tokens")} <span className="src">{srcTag("default_max_tokens")}</span></>} hint={t("modelProviders.hint.kindDefault")}>
                <input className="v2-input" type="number" min={1} value={form.default_max_tokens} onChange={(e) => set("default_max_tokens", e.target.value)} data-testid="v2-mp-model-default-max" />
              </Field>
              <Field label={<>{t("modelProviders.cap.max_output_tokens")} <span className="src">{srcTag("max_output_tokens")}</span></>}>
                <input className="v2-input" type="number" min={1} value={form.max_output_tokens} onChange={(e) => set("max_output_tokens", e.target.value)} data-testid="v2-mp-model-max-output" />
              </Field>
              <Field label={<>{t("modelProviders.cap.default_judge_max_tokens")} <span className="src">{srcTag("default_judge_max_tokens")}</span></>}>
                <input className="v2-input" type="number" min={1} value={form.default_judge_max_tokens} onChange={(e) => set("default_judge_max_tokens", e.target.value)} data-testid="v2-mp-model-judge" />
              </Field>
            </div>
          </>
        ) : (
          <div className="cols">
            <Field label={<>{t("modelProviders.cap.embedding_dimension")} <span className="src">{srcTag("embedding_dimension")}</span></>} required={!form.probe || editing} error={touched ? dimError : null} hint={t("modelProviders.hint.embeddingDimension")}>
              <input className="v2-input" type="number" min={1} value={form.embedding_dimension} onChange={(e) => set("embedding_dimension", e.target.value)} data-testid="v2-mp-model-dimension" />
            </Field>
            <Field label={t("modelProviders.cap.embedding_max_chars")}>
              <input className="v2-input" type="number" min={1} value={form.embedding_max_chars} onChange={(e) => set("embedding_max_chars", e.target.value)} data-testid="v2-mp-model-max-chars" />
            </Field>
          </div>
        )}
        {editing && (
          <div className="v2-row">
            <label className="v2-check">
              <input type="checkbox" checked={form.enabled} onChange={(e) => set("enabled", e.target.checked)} data-testid="v2-mp-model-enabled" />
              {t("modelProviders.field.enabled")}
            </label>
            {model.probe?.findings && (
              <Button size="sm" onClick={resetToProbed} testId="v2-mp-model-reset-probed">
                {t("modelProviders.action.resetToProbed")}
              </Button>
            )}
          </div>
        )}
        {editing && <Alert tone="info">{t("modelProviders.manualNote")}</Alert>}
      </div>
    </Drawer>
  );
}

/** With a probe on, only the fields a probe cannot settle (§7.2: vision, budgets) are
 *  sent as overrides — the probed booleans would otherwise all turn `manual`. */
function pickOverrides(all: Partial<ModelCapabilityFields>, form: FormState): Partial<ModelCapabilityFields> {
  const out: Partial<ModelCapabilityFields> = {};
  if (form.kind === "embedding") {
    if (all.embedding_max_chars != null) out.embedding_max_chars = all.embedding_max_chars;
    return out;
  }
  if (form.vision) out.vision = true;
  if (all.default_max_tokens != null) out.default_max_tokens = all.default_max_tokens;
  if (all.max_output_tokens != null) out.max_output_tokens = all.max_output_tokens;
  if (all.default_judge_max_tokens != null) out.default_judge_max_tokens = all.default_judge_max_tokens;
  return out;
}
