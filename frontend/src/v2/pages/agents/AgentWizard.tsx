import { ExternalLink } from "lucide-react";
import { type ReactNode, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { DEFAULT_TIMEOUT_SECONDS } from "../../../lib/agent-defaults";
import {
  type AgentSpecInput,
  api,
  errorMessage,
  HARNESS_NATIVE_TOOLS,
  type HarnessNativeTool,
} from "../../../lib/api";
import {
  CUSTOM_MODEL_OPTION,
  DEFAULT_MODEL_SOURCE,
  defaultModelFor,
  type ModelSource,
  modelOptionsFor,
  REASONING_EFFORTS,
  type ReasoningEffort,
  supportsReasoningEffort,
} from "../../../lib/models";
import { DEFAULT_MAX_ITERATIONS, MAX_TOKENS_CEILING } from "../../../pages/create/presetSettings";
import { useLoad, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  Descriptions,
  Field,
  FlowHeader,
  OptionCard,
  Segmented,
  Spin,
  Steps,
  Tag,
} from "../../ui";

/** Same rule as the backend's `AgentSpec.name`. */
const NAME_RE = /^[a-z][a-z0-9-]{2,47}$/;
const BUILTIN_TOOLS = ["code-interpreter", "browser"] as const;

type Method = "harness" | "zip_runtime" | "container" | "byoc";
const METHODS: Method[] = ["harness", "zip_runtime", "container", "byoc"];

interface Draft {
  name: string;
  systemPrompt: string;
  modelSource: ModelSource;
  modelId: string;
  customModel: boolean;
  maxTokens: string;
  effort: ReasoningEffort | "";
  maxIterations: string;
  timeoutSeconds: string;
  builtins: string[];
  gateways: string[];
  mcps: string[];
  nativeTools: HarnessNativeTool[];
  skills: string[];
  kbs: string[];
  longTerm: boolean;
  memoryId: string;
}

const EMPTY: Draft = {
  name: "",
  systemPrompt: "",
  modelSource: DEFAULT_MODEL_SOURCE,
  modelId: defaultModelFor(DEFAULT_MODEL_SOURCE),
  customModel: false,
  maxTokens: "",
  effort: "",
  maxIterations: String(DEFAULT_MAX_ITERATIONS),
  timeoutSeconds: String(DEFAULT_TIMEOUT_SECONDS),
  builtins: [],
  gateways: [],
  mcps: [],
  nativeTools: [],
  skills: [],
  kbs: [],
  longTerm: true,
  memoryId: "",
};

function toggle<T>(list: T[], value: T): T[] {
  return list.includes(value) ? list.filter((v) => v !== value) : [...list, value];
}

function positiveInt(raw: string): number | null {
  const n = Number(raw.trim());
  return raw.trim() && Number.isInteger(n) && n > 0 ? n : null;
}

/** Checkbox list of a catalog; an empty catalog shows `empty`. */
function CheckList<T>({
  items,
  keyOf,
  labelOf,
  hintOf,
  disabledOf,
  selected,
  onToggle,
  empty,
  testId,
}: {
  items: T[];
  keyOf: (item: T) => string;
  labelOf: (item: T) => ReactNode;
  hintOf?: (item: T) => string | undefined;
  disabledOf?: (item: T) => boolean;
  selected: string[];
  onToggle: (key: string) => void;
  empty: string;
  testId?: string;
}) {
  if (items.length === 0) return <span className="v2-muted">{empty}</span>;
  return (
    <div className="v2-checks" data-testid={testId}>
      {items.map((item) => {
        const key = keyOf(item);
        const disabled = disabledOf?.(item) ?? false;
        return (
          <label key={key} className={disabled ? "v2-check disabled" : "v2-check"} title={hintOf?.(item)}>
            <input
              type="checkbox"
              checked={selected.includes(key)}
              disabled={disabled}
              onChange={() => onToggle(key)}
            />
            {labelOf(item)}
          </label>
        );
      })}
    </div>
  );
}

/**
 * Native V2 creation wizard. The managed Harness (the recommended, build-free
 * method) is configured here end to end and posts the same `AgentSpecInput`
 * the classic wizard builds for it; the other methods hand off to the classic
 * wizard (inside the V2 shell) with the method preselected.
 */
export function AgentWizard() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { can } = useAuth();
  const canDeploy = can("agents.deploy");
  const [step, setStep] = useState(0);
  const [method, setMethod] = useState<Method>("harness");
  const [draft, setDraft] = useState<Draft>(EMPTY);
  const [touched, setTouched] = useState(false);
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const set = (patch: Partial<Draft>) => setDraft((prev) => ({ ...prev, ...patch }));

  // Catalogs: a failed read leaves that section empty and never blocks the form.
  const attachables = useLoad(() => api.registryAttachables(), "attachables");
  const kbs = useLoad(() => api.listAttachableKnowledgeBases(), "kbs");
  const memories = useLoad(() => api.memoryResources(), "memories");
  const gatewayTargets = (attachables.data?.mcp_servers ?? []).filter((m) => m.gateway);
  const remoteMcp = (attachables.data?.mcp_servers ?? []).filter((m) => !m.gateway);
  const skillCatalog = attachables.data?.skills ?? [];
  const kbCatalog = (kbs.data?.items ?? []).filter((kb) => !kb.status || kb.status === "ACTIVE");
  const memoryOptions = (memories.data?.items ?? []).filter((m) => m.id && !m.is_default);

  const modelOptions = modelOptionsFor(draft.modelSource);
  const modelId = draft.modelId.trim();
  const effortAllowed = supportsReasoningEffort(modelId, draft.modelSource);

  const problems = useMemo(() => {
    const out: Partial<Record<"name" | "prompt" | "model" | "maxTokens" | "maxIterations" | "timeout", string>> = {};
    if (!NAME_RE.test(draft.name)) out.name = t("v2.agents.wizard.errName");
    if (!draft.systemPrompt.trim()) out.prompt = t("v2.agents.wizard.errPrompt");
    if (!modelId) out.model = t("v2.agents.wizard.errModel");
    if (draft.maxTokens.trim()) {
      const n = positiveInt(draft.maxTokens);
      if (!n || n > MAX_TOKENS_CEILING) out.maxTokens = t("v2.agents.wizard.errMaxTokens", { max: MAX_TOKENS_CEILING });
    }
    if (!positiveInt(draft.maxIterations)) out.maxIterations = t("v2.agents.wizard.errPositive");
    if (!positiveInt(draft.timeoutSeconds)) out.timeout = t("v2.agents.wizard.errPositive");
    return out;
  }, [draft, modelId, t]);
  const valid = Object.keys(problems).length === 0;
  const show = (key: keyof typeof problems) => (touched ? problems[key] : undefined);

  const buildSpec = (): AgentSpecInput => {
    const maxTokens = positiveInt(draft.maxTokens);
    const kbInfo = (id: string) => {
      const kb = kbCatalog.find((k) => k.kb_id === id);
      return { kb_id: id, name: kb?.name ?? id, description: kb?.description ?? "" };
    };
    return {
      name: draft.name,
      method: "harness",
      model_id: modelId,
      model_source: draft.modelSource,
      ...(maxTokens ? { max_tokens: maxTokens } : {}),
      ...(effortAllowed && draft.effort ? { reasoning_effort: draft.effort } : {}),
      max_iterations: positiveInt(draft.maxIterations) ?? DEFAULT_MAX_ITERATIONS,
      timeout_seconds: positiveInt(draft.timeoutSeconds) ?? DEFAULT_TIMEOUT_SECONDS,
      system_prompt: draft.systemPrompt,
      tools: [
        ...draft.builtins.map((name) => ({ type: "builtin", name })),
        ...draft.gateways.flatMap((name) => {
          const target = gatewayTargets.find((g) => g.name === name);
          if (!target) return [];
          return [
            {
              type: "gateway",
              name,
              ...(target.gateway_id ? { config: { record_id: target.record_id, gateway_id: target.gateway_id } } : {}),
            },
          ];
        }),
        ...draft.mcps.flatMap((name) => {
          const server = remoteMcp.find((m) => m.name === name);
          return server ? [{ type: "mcp", name, config: { url: server.url } }] : [];
        }),
      ],
      memory: {
        short_term: true,
        long_term: draft.longTerm,
        ...(draft.memoryId ? { memory_id: draft.memoryId } : {}),
      },
      ...(draft.kbs.length ? { knowledge_bases: draft.kbs.map(kbInfo) } : {}),
      ...(draft.skills.length ? { skills: draft.skills } : {}),
      // null ⇒ the backend derives the allowlist from attachments, Skills and native tools
      allowed_tools: null,
      native_tools: draft.nativeTools,
    };
  };

  const next = () => {
    setError(null);
    if (step === 0) {
      if (method === "harness") setStep(1);
      else navigate(`/agents/new?method=${method}`);
      return;
    }
    setTouched(true);
    if (valid) setStep(2);
  };

  const submit = async () => {
    if (submitting) return;
    setSubmitting(true);
    setError(null);
    try {
      const res = await api.createAgent(buildSpec());
      toast("success", t("v2.agents.wizard.started", { name: res.agent.name }));
      setParams({ view: "detail", id: res.agent.id });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  const stepLabels = [t("v2.agents.wizard.stepMethod"), t("v2.agents.wizard.stepConfig"), t("v2.agents.wizard.stepReview")];
  const selectedNames = (keys: string[], label: (key: string) => string) =>
    keys.length ? keys.map(label).join(", ") : t("v2.agents.wizard.none");

  return (
    <>
      <FlowHeader
        title={t("v2.agents.new")}
        onBack={() => setParams({})}
        steps={<Steps steps={stepLabels} current={step} onSelect={setStep} />}
        end={
          <>
            <Button disabled={step === 0 || submitting} onClick={() => setStep(step - 1)}>
              {t("v2.common.prev")}
            </Button>
            {step < 2 ? (
              <Button kind="primary" disabled={!canDeploy} onClick={next} testId="v2-agent-wizard-next">
                {t("v2.common.next")}
              </Button>
            ) : (
              <Button kind="primary" disabled={submitting || !canDeploy} onClick={() => void submit()} testId="v2-agent-wizard-submit">
                {t("v2.agents.wizard.submit")}
              </Button>
            )}
          </>
        }
      />
      {!canDeploy && <Alert tone="warn">{t("v2.agents.noPermission")}</Alert>}
      {error && <Alert tone="error">{error}</Alert>}

      {step === 0 && (
        <>
          <Card title={t("v2.agents.wizard.methodTitle")} sub={t("v2.agents.wizard.methodSub")}>
            <div className="v2-options">
              {METHODS.map((m) => (
                <OptionCard
                  key={m}
                  title={t(`v2.agents.wizard.method.${m}`)}
                  desc={t(`v2.agents.wizard.method.${m}Desc`)}
                  on={method === m}
                  onClick={() => setMethod(m)}
                  badge={
                    m === "harness" ? (
                      <Tag tone="blue">{t("v2.agents.wizard.recommended")}</Tag>
                    ) : (
                      <Tag tone="gray">{t("v2.agents.wizard.classicForm")}</Tag>
                    )
                  }
                  testId={`v2-agent-method-${m}`}
                />
              ))}
            </div>
          </Card>
          <Card title={t("v2.agents.wizard.otherWays")}>
            <div className="v2-row">
              <Link to="/create/assistant" className="v2-btn">
                {t("v2.agents.wizard.assistant")}
              </Link>
              <Link to="/create/studio" className="v2-btn">
                {t("v2.agents.wizard.studio")}
              </Link>
              <Link to="/agents/import" className="v2-btn">
                {t("v2.agents.import")}
              </Link>
              <Link to="/agents/new" className="v2-btn">
                <ExternalLink size={13} aria-hidden="true" />
                {t("v2.agents.wizard.presets")}
              </Link>
            </div>
          </Card>
        </>
      )}

      {step === 1 && (
        <>
          <Card title={t("v2.agents.basic")}>
            <div className="v2-form">
              <Field label={t("v2.agents.colName")} required hint={t("v2.agents.wizard.nameHint")} error={show("name")}>
                <input
                  className="v2-input"
                  value={draft.name}
                  maxLength={48}
                  onChange={(e) => set({ name: e.target.value.toLowerCase() })}
                  data-testid="v2-agent-name"
                />
              </Field>
              <Field label={t("v2.agents.wizard.systemPrompt")} required error={show("prompt")}>
                <textarea
                  className="v2-textarea"
                  rows={6}
                  maxLength={20000}
                  value={draft.systemPrompt}
                  placeholder={t("v2.agents.wizard.promptPlaceholder")}
                  onChange={(e) => set({ systemPrompt: e.target.value })}
                  data-testid="v2-agent-prompt"
                />
              </Field>
            </div>
          </Card>

          <Card title={t("v2.agents.model")}>
            <div className="v2-form cols-2">
              <Field label={t("v2.agents.wizard.modelSource")} full>
                <Segmented
                  value={draft.modelSource}
                  options={(["mantle", "bedrock"] as ModelSource[]).map((s) => ({ value: s, label: t(`v2.agents.wizard.source.${s}`) }))}
                  onChange={(s) => set({ modelSource: s, modelId: defaultModelFor(s), customModel: false, effort: "" })}
                />
              </Field>
              <Field label={t("v2.agents.model")} required error={show("model")}>
                <select
                  className="v2-select"
                  value={draft.customModel ? CUSTOM_MODEL_OPTION : draft.modelId}
                  onChange={(e) =>
                    e.target.value === CUSTOM_MODEL_OPTION
                      ? set({ customModel: true, modelId: "" })
                      : set({ customModel: false, modelId: e.target.value })
                  }
                  data-testid="v2-agent-model"
                >
                  {modelOptions.map((o) => (
                    <option key={o.model_id} value={o.model_id}>
                      {o.label}
                    </option>
                  ))}
                  <option value={CUSTOM_MODEL_OPTION}>{t("v2.agents.wizard.customModel")}</option>
                </select>
              </Field>
              {draft.customModel ? (
                <Field label={t("v2.agents.wizard.customModelId")} required>
                  <input
                    className="v2-input mono"
                    value={draft.modelId}
                    onChange={(e) => set({ modelId: e.target.value })}
                    placeholder="us.anthropic.claude-…"
                  />
                </Field>
              ) : (
                <Field label={t("v2.agents.wizard.modelId")}>
                  <input className="v2-input mono" value={draft.modelId} readOnly />
                </Field>
              )}
              <Field label={t("v2.agents.wizard.maxTokens")} hint={t("v2.agents.wizard.maxTokensHint")} error={show("maxTokens")}>
                <input className="v2-input" inputMode="numeric" value={draft.maxTokens} onChange={(e) => set({ maxTokens: e.target.value })} />
              </Field>
              <Field
                label={t("v2.agents.wizard.effort")}
                hint={effortAllowed ? undefined : t("v2.agents.wizard.effortUnsupported")}
              >
                <select
                  className="v2-select"
                  value={effortAllowed ? draft.effort : ""}
                  disabled={!effortAllowed}
                  onChange={(e) => set({ effort: e.target.value as Draft["effort"] })}
                >
                  <option value="">{t("v2.agents.wizard.none")}</option>
                  {REASONING_EFFORTS.map((e) => (
                    <option key={e} value={e}>
                      {e}
                    </option>
                  ))}
                </select>
              </Field>
            </div>
          </Card>

          <Card title={t("v2.agents.wizard.tools")} sub={t("v2.agents.wizard.toolsSub")}>
            <div className="v2-form">
              <Field label={t("v2.agents.wizard.builtinTools")}>
                <CheckList
                  items={[...BUILTIN_TOOLS]}
                  keyOf={(n) => n}
                  labelOf={(n) => t(`v2.agents.wizard.builtin.${n}`)}
                  selected={draft.builtins}
                  onToggle={(n) => set({ builtins: toggle(draft.builtins, n) })}
                  empty=""
                />
              </Field>
              <Field label={t("v2.agents.wizard.gateways")}>
                {attachables.loading ? (
                  <Spin />
                ) : (
                  <CheckList
                    items={gatewayTargets}
                    keyOf={(g) => g.name}
                    labelOf={(g) => g.name}
                    hintOf={(g) => g.attachability_reason ?? g.description}
                    disabledOf={(g) => !g.attachable}
                    selected={draft.gateways}
                    onToggle={(n) => set({ gateways: toggle(draft.gateways, n) })}
                    empty={t("v2.agents.wizard.noGateways")}
                    testId="v2-agent-gateways"
                  />
                )}
              </Field>
              <Field label={t("v2.agents.wizard.remoteMcp")}>
                <CheckList
                  items={remoteMcp}
                  keyOf={(m) => m.name}
                  labelOf={(m) => m.name}
                  hintOf={(m) => m.attachability_reason ?? m.description}
                  disabledOf={(m) => !m.attachable}
                  selected={draft.mcps}
                  onToggle={(n) => set({ mcps: toggle(draft.mcps, n) })}
                  empty={t("v2.agents.wizard.noMcp")}
                />
              </Field>
              <Field label={t("v2.agents.wizard.nativeTools")} hint={t("v2.agents.wizard.nativeToolsHint")}>
                <CheckList
                  items={[...HARNESS_NATIVE_TOOLS]}
                  keyOf={(n) => n}
                  labelOf={(n) => t(`v2.agents.wizard.native.${n}`)}
                  selected={draft.nativeTools}
                  onToggle={(n) => set({ nativeTools: toggle(draft.nativeTools, n as HarnessNativeTool) })}
                  empty=""
                />
              </Field>
            </div>
          </Card>

          <Card title={t("v2.agents.wizard.skillsKb")}>
            <div className="v2-form">
              <Field label={t("v2.agents.wizard.skills")} hint={t("v2.agents.wizard.skillsHint")}>
                <CheckList
                  items={skillCatalog}
                  keyOf={(s) => s.path}
                  labelOf={(s) => s.name}
                  hintOf={(s) => s.description}
                  selected={draft.skills}
                  onToggle={(p) => set({ skills: toggle(draft.skills, p) })}
                  empty={t("v2.agents.wizard.noSkills")}
                  testId="v2-agent-skills"
                />
              </Field>
              <Field label={t("v2.agents.kbTitle")}>
                <CheckList
                  items={kbCatalog}
                  keyOf={(kb) => kb.kb_id}
                  labelOf={(kb) => kb.name}
                  hintOf={(kb) => kb.description}
                  selected={draft.kbs}
                  onToggle={(id) => set({ kbs: toggle(draft.kbs, id) })}
                  empty={t("v2.agents.wizard.noKbs")}
                />
              </Field>
            </div>
          </Card>

          <Card title={t("v2.agents.wizard.memory")}>
            <div className="v2-form cols-2">
              <Field label={t("v2.agents.wizard.longTerm")} hint={t("v2.agents.wizard.longTermHint")}>
                <label className="v2-check">
                  <input type="checkbox" checked={draft.longTerm} onChange={(e) => set({ longTerm: e.target.checked })} />
                  {t("v2.agents.wizard.longTermOn")}
                </label>
              </Field>
              <Field label={t("v2.agents.wizard.memoryResource")}>
                <select className="v2-select" value={draft.memoryId} onChange={(e) => set({ memoryId: e.target.value })}>
                  <option value="">{t("v2.agents.wizard.memoryDefault")}</option>
                  {memoryOptions.map((m) => (
                    <option key={m.id ?? ""} value={m.id ?? ""}>
                      {m.name ?? m.id}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t("v2.agents.wizard.maxIterations")} error={show("maxIterations")}>
                <input className="v2-input" inputMode="numeric" value={draft.maxIterations} onChange={(e) => set({ maxIterations: e.target.value })} />
              </Field>
              <Field label={t("v2.agents.wizard.timeout")} error={show("timeout")}>
                <input className="v2-input" inputMode="numeric" value={draft.timeoutSeconds} onChange={(e) => set({ timeoutSeconds: e.target.value })} />
              </Field>
            </div>
          </Card>
        </>
      )}

      {step === 2 && (
        <Card title={t("v2.agents.wizard.reviewTitle")} sub={t("v2.agents.wizard.reviewSub")} testId="v2-agent-review">
          <Descriptions
            items={[
              { label: t("v2.agents.colName"), value: draft.name },
              { label: t("v2.agents.colMethod"), value: t("v2.agents.wizard.method.harness") },
              { label: t("v2.agents.model"), value: <span className="mono">{modelId}</span> },
              { label: t("v2.agents.wizard.modelSource"), value: t(`v2.agents.wizard.source.${draft.modelSource}`) },
              {
                label: t("v2.agents.wizard.tools"),
                value: selectedNames(
                  [
                    ...draft.builtins.map((n) => t(`v2.agents.wizard.builtin.${n}`)),
                    ...draft.gateways,
                    ...draft.mcps,
                    ...draft.nativeTools.map((n) => t(`v2.agents.wizard.native.${n}`)),
                  ],
                  (x) => x,
                ),
              },
              {
                label: t("v2.agents.wizard.skills"),
                value: selectedNames(draft.skills, (p) => skillCatalog.find((s) => s.path === p)?.name ?? p),
              },
              {
                label: t("v2.agents.kbTitle"),
                value: selectedNames(draft.kbs, (id) => kbCatalog.find((k) => k.kb_id === id)?.name ?? id),
              },
              {
                label: t("v2.agents.wizard.memory"),
                value: draft.longTerm ? t("v2.agents.wizard.memoryLong") : t("v2.agents.wizard.memoryShort"),
              },
              {
                label: t("v2.agents.wizard.loop"),
                value: t("v2.agents.wizard.loopValue", { iterations: draft.maxIterations, seconds: draft.timeoutSeconds }),
              },
            ]}
          />
          <h3 className="v2-sub-title">{t("v2.agents.wizard.systemPrompt")}</h3>
          <pre className="v2-pre">{draft.systemPrompt}</pre>
          <div style={{ marginTop: 16 }}>
            <Alert>{t("v2.agents.wizard.deployNote")}</Alert>
          </div>
        </Card>
      )}
    </>
  );
}
