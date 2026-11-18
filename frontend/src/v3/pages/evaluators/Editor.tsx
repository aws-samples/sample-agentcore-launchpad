import { ArrowLeft, Plus, Trash2 } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  errorMessage,
  type EvaluatorCreateBody,
  type EvaluatorDefinition,
  type EvaluatorDetail,
  type EvaluatorUpdateBody,
  type ScalePoint,
} from "../../../lib/api";
import { FAILURE_ANALYSIS_EVALUATOR,
  defaultJudgeModel,
  evaluatorLabel,
  type EvaluatorLevel,
  judgeModelOptions,
  LEVEL_PLACEHOLDERS,
} from "../../../lib/evaluators";
import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { sharedFromOf } from "../../../lib/api/sharedFrom";
import { useModelCatalog } from "../../../lib/model-catalog";
import { adoptModelDefault, useModelDefaultsArrival } from "../../../lib/model-defaults";
import { CUSTOM_MODEL_OPTION } from "../../../lib/models";
import { useLoad, useToast } from "../../hooks";
import { Reasoned } from "../../reason";
import { Btn, Chip, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { LEVELS } from "./common";
import { Select } from "../../select";

// Same rules as V2's editor (and the backend behind both).
const NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;
const PLACEHOLDER_RE = /\{[a-zA-Z_][a-zA-Z0-9_]*\}/;
const DEFAULT_SCALE: ScalePoint[] = [
  { value: 1, label: "pass", definition: "meets the instruction" },
  { value: 0, label: "fail", definition: "does not meet the instruction" },
];

interface Draft {
  definition: EvaluatorDefinition;
  name: string;
  description: string;
  level: EvaluatorLevel;
  model_id: string;
  instructions: string;
  base_evaluator_id: string;
  lambda_arn: string;
  lambda_timeout_s: number;
  rating_scale: ScalePoint[];
}

// a function, not a constant: the judge is the configured default once loaded
const emptyDraft = (): Draft => ({
  definition: "judge",
  name: "",
  description: "",
  level: "TRACE",
  model_id: defaultJudgeModel(),
  instructions: "",
  base_evaluator_id: "",
  lambda_arn: "",
  lambda_timeout_s: 60,
  rating_scale: DEFAULT_SCALE,
});

function draftFrom(d: EvaluatorDetail): Draft {
  return {
    definition: d.definition,
    name: d.name ?? d.id,
    description: d.description ?? "",
    level: (d.level as EvaluatorLevel) ?? "TRACE",
    model_id: d.model_id ?? defaultJudgeModel(),
    instructions: d.instructions ?? "",
    base_evaluator_id: d.base_evaluator_id ?? "",
    lambda_arn: d.lambda_arn ?? "",
    lambda_timeout_s: d.lambda_timeout_s ?? 60,
    rating_scale: d.rating_scale.length ? d.rating_scale : DEFAULT_SCALE,
  };
}

/** UpdateEvaluator replaces the whole config, so the body carries only the definition's own fields. */
function bodyFrom(draft: Draft): EvaluatorUpdateBody {
  if (draft.definition === "derived") {
    return { base_evaluator_id: draft.base_evaluator_id, model_id: draft.model_id, description: draft.description };
  }
  if (draft.definition === "code") {
    return { lambda_arn: draft.lambda_arn.trim(), lambda_timeout_s: draft.lambda_timeout_s, level: draft.level, description: draft.description };
  }
  return { instructions: draft.instructions, model_id: draft.model_id, level: draft.level, description: draft.description, rating_scale: draft.rating_scale };
}

function validate(draft: Draft, editing: boolean, t: (k: string) => string): string | null {
  if (!editing && !NAME_RE.test(draft.name)) return t("v2.evaluators.errName");
  if (draft.definition === "judge") {
    if (draft.instructions.trim().length < 10) return t("v2.evaluators.errRubric");
    if (!PLACEHOLDER_RE.test(draft.instructions)) return t("v2.evaluators.errPlaceholder");
    if (draft.rating_scale.length < 2 || draft.rating_scale.some((p) => !p.label.trim() || !p.definition.trim())) return t("v2.evaluators.errScale");
  }
  if (draft.definition === "derived" && !draft.base_evaluator_id) return t("v2.evaluators.errBase");
  if (draft.definition === "code" && !draft.lambda_arn.trim().startsWith("arn:")) return t("v2.evaluators.errLambda");
  return null;
}

const MAPS_TO: Record<string, string> = {
  "{context}": "Input",
  "{user_message}": "Input",
  "{assistant_turn}": "Output",
  "{expected_response}": "Expected Output",
  "{actual_tool_trajectory}": "Trajectory",
  "{expected_tool_trajectory}": "Expected Trajectory",
  "{tool_turn}": "Trajectory",
};

export function EvaluatorEditor({ id }: { id: string | null }) {
  const { t } = useTranslation();
  const toast = useToast();
  const [, setParams] = useSearchParams();
  const editing = id !== null;
  const detail = useLoad<EvaluatorDetail | null>(() => (id ? api.v2Evaluator(id) : Promise.resolve(null)), `v3-evaluator-edit:${id ?? "new"}`);
  const catalog = useLoad(() => api.v2Evaluators(), "v3-evaluators");
  const [draft, setDraft] = useState<Draft | null>(() => (editing ? null : emptyDraft()));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  // A new evaluator seeds the judge before the configured defaults arrive; adopt the
  // configured judge then, unless the user already picked one (edits keep the stored model).
  const seededJudge = useRef(defaultJudgeModel());
  const judgeTouched = useRef(false);
  const [judgeCustom, setJudgeCustom] = useState(false);
  useModelCatalog();
  const modelDefaults = useModelDefaultsArrival((d) => {
    if (editing) return;
    setDraft((cur) => cur && {
      ...cur,
      model_id: adoptModelDefault(cur.model_id, seededJudge.current, d.judge_model_id, judgeTouched.current),
    });
  });

  // seed the edit form once the evaluator arrives
  if (editing && draft === null && detail.data) setDraft(draftFrom(detail.data));
  if (editing && (detail.loading || draft === null)) return detail.error ? <Notice s="act"><ApiErrorDetail error={detail.failure ?? detail.error} /></Notice> : <Skeleton rows={6} />;
  if (!draft) return <Skeleton rows={6} />;
  // an evaluator another project lent this one is its owner's to edit (§11.2): a
  // typed ?view=edit URL lands on a note, not on a form the backend would refuse
  const lent = editing
    ? sharedFromOf(detail.data) ?? sharedFromOf(catalog.data?.evaluators.find((e) => e.id === id))
    : null;
  if (lent && id) {
    return (
      <div style={{ display: "grid", gap: 16 }}>
        <div>
          <button type="button" className="v3-btn ghost sm" onClick={() => setParams({ view: "detail", id })}>
            <ArrowLeft size={14} /> {id}
          </button>
        </div>
        <Notice s="wait">{t("v3.evaluators.sharedReadOnly", { project: lent.name })}</Notice>
      </div>
    );
  }

  const bases = (catalog.data?.evaluators ?? []).filter((e) => e.source !== "custom" && e.id !== FAILURE_ANALYSIS_EVALUATOR);
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });
  const ph = LEVEL_PLACEHOLDERS[draft.level];
  const tokens = [
    ...ph.core.map((token) => ({ token, gt: false })),
    ...ph.groundTruth.map((token) => ({ token, gt: true })),
    ...(ph.skill ?? []).map((token) => ({ token, gt: false })),
  ];
  const insert = (token: string) =>
    set({ instructions: `${draft.instructions}${draft.instructions && !draft.instructions.endsWith(" ") ? " " : ""}${token}` });
  // configured default + fallback + GLM first, then the static judges of this
  // workspace's geography; a stored id is never dropped
  const models = judgeModelOptions(modelDefaults, draft.model_id);

  const save = async () => {
    const problem = validate(draft, editing, t);
    setError(problem);
    if (problem) return;
    setSaving(true);
    try {
      const reply = id
        ? await api.v2UpdateEvaluator(id, bodyFrom(draft))
        : await api.v2CreateEvaluator({ ...bodyFrom(draft), name: draft.name } as EvaluatorCreateBody);
      // a judge saved on the fallback model is still a success, but it must say so
      toast("ok", reply.model_fallback
        ? t("v2.evaluators.modelFallback", { requested: reply.model_fallback.requested, used: reply.model_fallback.used })
        : t(id ? "v2.evaluators.saved" : "v2.evaluators.created"));
      setParams({ view: "detail", id: id ?? (reply as { evaluator_id: string }).evaluator_id });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={() => setParams(id ? { view: "detail", id } : {})}>
          <ArrowLeft size={14} /> {editing ? (draft.name || id) : t("v2.evaluators.title")}
        </button>
      </div>
      <PageHead
        eyebrow={t("v3.evaluators.eyebrow")}
        title={editing ? t("v2.evaluators.editTitle") : t("v2.evaluators.newTitle")}
        end={<Btn kind="primary" disabled={saving} onClick={() => void save()}>{editing ? t("v3.evaluators.save") : t("v3.evaluators.create")}</Btn>}
      />
      {error && <Notice s="act">{error}</Notice>}

      <Panel title={t("v2.evaluators.definitionLabel")}>
        <div className="v3-ev-defs" role="radiogroup" aria-label={t("v2.evaluators.definitionLabel")}>
          {(["judge", "derived", "code"] as const).map((def) => (
            <Reasoned key={def} disabledReason={t("story.reason.fixedOnceCreated")}>
              <button type="button" role="radio" aria-checked={draft.definition === def}
                className={draft.definition === def ? "v3-ev-def on" : "v3-ev-def"}
                disabled={editing && draft.definition !== def} onClick={() => set({ definition: def })}>
                <b>{t(`v2.evaluators.definition.${def}`)}</b>
                <small>{t(`v2.evaluators.definitionDesc.${def}`)}</small>
              </button>
            </Reasoned>
          ))}
        </div>
      </Panel>

      <Panel title={t("v2.evaluators.basic")}>
        <div className="v3-grid c2" style={{ alignItems: "start" }}>
          <label className="v3-field">
            <span>{t("v2.evaluators.colName")}</span>
            <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
              <input className="v3-input mono" value={draft.name} disabled={editing} placeholder="answer_faithfulness" onChange={(e) => set({ name: e.target.value })} />
            </Reasoned>
            <small className="v3-hint">{t("v2.evaluators.nameHint")}</small>
          </label>
          {draft.definition !== "derived" && (
            <label className="v3-field">
              <span>{t("v2.evaluators.colLevel")}</span>
              <Select className="v3-select" value={draft.level} onChange={(e) => set({ level: e.target.value as EvaluatorLevel })}>
                {LEVELS.map((l) => <option key={l} value={l}>{t(`v2.level.${l}`)}</option>)}
              </Select>
              <small className="v3-hint">{t("v2.evaluators.levelHint")}</small>
            </label>
          )}
          {draft.definition !== "code" && (
            <label className="v3-field">
              <span>{t("v2.evaluators.model")}</span>
              <Select className="v3-select" value={judgeCustom ? CUSTOM_MODEL_OPTION : draft.model_id} onChange={(e) => {
                judgeTouched.current = true;
                setJudgeCustom(e.target.value === CUSTOM_MODEL_OPTION);
                if (e.target.value !== CUSTOM_MODEL_OPTION) set({ model_id: e.target.value });
              }}>
                {models.map((m) => <option key={m} value={m}>{m}</option>)}
                <option value={CUSTOM_MODEL_OPTION}>{t("models.picker.custom")}</option>
              </Select>
              {judgeCustom && (
                <input className="v3-input mono" value={draft.model_id}
                  placeholder={t("models.picker.customPlaceholder", { example: defaultJudgeModel(modelDefaults) })}
                  onChange={(e) => set({ model_id: e.target.value.trim() })} />
              )}
              <small className="v3-hint">{t("v2.evaluators.modelHint")}</small>
            </label>
          )}
          {draft.definition === "derived" && (
            <label className="v3-field">
              <span>{t("v2.evaluators.base")}</span>
              <Reasoned disabledReason={t("story.reason.fixedOnceCreated")}>
                <Select className="v3-select" value={draft.base_evaluator_id} disabled={editing} onChange={(e) => set({ base_evaluator_id: e.target.value })}>
                  <option value="">{t("v3.evaluators.choose")}</option>
                  {bases.map((b) => <option key={b.id} value={b.id}>{`${evaluatorLabel(t, b.id)} · ${b.id}`}</option>)}
                </Select>
              </Reasoned>
            </label>
          )}
          {draft.definition === "code" && (
            <>
              <label className="v3-field" style={{ gridColumn: "1 / -1" }}>
                <span>{t("v2.evaluators.lambda")}</span>
                <input className="v3-input mono" value={draft.lambda_arn} placeholder="arn:aws:lambda:us-west-2:123456789012:function:my-evaluator"
                  onChange={(e) => set({ lambda_arn: e.target.value })} />
                <small className="v3-hint">{t("v2.evaluators.lambdaHint")}</small>
              </label>
              <label className="v3-field">
                <span>{t("v2.evaluators.timeout")}</span>
                <input className="v3-input" type="number" min={1} max={300} value={draft.lambda_timeout_s}
                  onChange={(e) => set({ lambda_timeout_s: Math.max(1, Math.min(300, Number(e.target.value) || 60)) })} />
              </label>
            </>
          )}
          <label className="v3-field" style={{ gridColumn: "1 / -1" }}>
            <span>{t("v2.evaluators.description")}</span>
            <input className="v3-input" value={draft.description} maxLength={1000} onChange={(e) => set({ description: e.target.value })} />
          </label>
        </div>
      </Panel>

      {draft.definition === "judge" && (
        <>
          <div className="v3-grid v3-split" style={{ alignItems: "start" }}>
            <Panel title={t("v2.evaluators.rubric")}>
              <p style={{ margin: "0 0 10px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.evaluators.rubricSub")}</p>
              <textarea className="v3-input" rows={12} value={draft.instructions} placeholder={t("v2.evaluators.rubricPlaceholder")}
                onChange={(e) => set({ instructions: e.target.value })} />
            </Panel>
            <Panel title={t("v2.evaluators.mapping")} flush>
              <p style={{ margin: "0 20px 10px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.evaluators.mappingSub")}</p>
              <table className="v3-table">
                <tbody>
                  {tokens.map((r) => (
                    <tr key={r.token}>
                      <td>
                        <div style={{ display: "grid", gap: 2 }}>
                          <code className="mono" style={{ color: "var(--v3-info)" }}>{r.token}</code>
                          <small style={{ color: "var(--v3-text-3)" }}>
                            {t(`v2.evaluators.ph.${r.token.slice(1, -1)}`, { defaultValue: r.token })}
                            {MAPS_TO[r.token] ? ` · ${MAPS_TO[r.token]}` : ""}
                          </small>
                        </div>
                      </td>
                      <td style={{ width: 1, whiteSpace: "nowrap" }}>
                        {r.gt && <Chip s="wait">{t("v2.evaluators.needsGt")}</Chip>}{" "}
                        <Btn size="sm" kind="ghost" onClick={() => insert(r.token)}>{t("v2.evaluators.insert")}</Btn>
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </Panel>
          </div>
          <Panel title={t("v2.evaluators.scale")}>
            <p style={{ margin: "0 0 12px", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.evaluators.scaleSub")}</p>
            <div style={{ display: "grid", gap: 8 }}>
              {draft.rating_scale.map((p, i) => (
                <div key={i} style={{ display: "grid", gridTemplateColumns: "90px 180px minmax(0, 1fr) auto", gap: 8 }}>
                  <input className="v3-input mono" type="number" step="0.1" value={p.value} aria-label={t("v2.evaluators.scaleValue")}
                    onChange={(e) => set({ rating_scale: draft.rating_scale.map((x, j) => (j === i ? { ...x, value: Number(e.target.value) } : x)) })} />
                  <input className="v3-input" value={p.label} placeholder={t("v2.evaluators.scaleLabel")} aria-label={t("v2.evaluators.scaleLabel")}
                    onChange={(e) => set({ rating_scale: draft.rating_scale.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)) })} />
                  <input className="v3-input" value={p.definition} placeholder={t("v2.evaluators.scaleDefinition")} aria-label={t("v2.evaluators.scaleDefinition")}
                    onChange={(e) => set({ rating_scale: draft.rating_scale.map((x, j) => (j === i ? { ...x, definition: e.target.value } : x)) })} />
                  <Btn size="sm" kind="ghost" disabled={draft.rating_scale.length <= 2} disabledReason={t("story.reason.twoScalePoints")}
                    title={t("v3.evaluators.delete")} aria-label={t("v3.evaluators.delete")}
                    onClick={() => set({ rating_scale: draft.rating_scale.filter((_, j) => j !== i) })}>
                    <Trash2 size={13} />
                  </Btn>
                </div>
              ))}
              <div>
                <Btn size="sm" onClick={() => set({ rating_scale: [...draft.rating_scale, { value: 0.5, label: "", definition: "" }] })}>
                  <Plus size={13} /> {t("v2.evaluators.scaleAdd")}
                </Btn>
              </div>
            </div>
            <p style={{ margin: "12px 0 0", color: "var(--v3-text-3)", fontSize: 13 }}>{t("v2.evaluators.outputFields")}</p>
          </Panel>
        </>
      )}
    </div>
  );
}
