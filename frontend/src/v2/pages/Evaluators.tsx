import { Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  errorMessage,
  type EvaluatorCreateBody,
  type EvaluatorDefinition,
  type EvaluatorDetail,
  type EvaluatorRow,
  type EvaluatorUpdateBody,
  type ScalePoint,
} from "../../lib/api";
import {
  evaluatorLabel,
  type EvaluatorLevel,
  JUDGE_MODEL_OPTIONS,
  LEVEL_PLACEHOLDERS,
} from "../../lib/evaluators";
import { useLoad, usePaged, useV2Toast } from "../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  Field,
  FilterSelect,
  FlowHeader,
  LinkButton,
  OptionCard,
  PageHeader,
  Pager,
  SearchInput,
  Spin,
  Table,
  Tag,
} from "../ui";

const LEVELS: EvaluatorLevel[] = ["SESSION", "TRACE", "TOOL_CALL"];
const NAME_RE = /^[a-zA-Z][a-zA-Z0-9_]{0,47}$/;
const PLACEHOLDER_RE = /\{[a-zA-Z_][a-zA-Z0-9_]*\}/;
const DEFAULT_SCALE: ScalePoint[] = [
  { value: 1, label: "pass", definition: "meets the instruction" },
  { value: 0, label: "fail", definition: "does not meet the instruction" },
];

type Source = EvaluatorRow["source"];

function sourceTone(source: Source) {
  return source === "custom" ? "blue" : source === "third_party" ? "orange" : "gray";
}

// ─── list ──────────────────────────────────────────────────────────────────
function EvaluatorList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { data, loading, error, reload } = useLoad(() => api.v2Evaluators(), "evaluators");
  const [source, setSource] = useState("");
  const [level, setLevel] = useState("");
  const [q, setQ] = useState("");
  const [deleting, setDeleting] = useState<EvaluatorRow | null>(null);
  const [busy, setBusy] = useState(false);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data?.evaluators ?? []).filter((row) => {
      if (source && row.source !== source) return false;
      if (level && row.level !== level) return false;
      if (!needle) return true;
      return `${row.id} ${row.name ?? ""} ${evaluatorLabel(t, row.id)}`.toLowerCase().includes(needle);
    });
  }, [data, source, level, q, t]);
  const paged = usePaged(rows, 12);

  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.v2DeleteEvaluator(deleting.id);
      toast("success", t("v2.evaluators.deleted"));
      setDeleting(null);
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<EvaluatorRow>[] = [
    {
      key: "name",
      title: t("v2.evaluators.colName"),
      render: (row) => (
        <>
          <LinkButton onClick={() => setParams({ view: "detail", id: row.id })}>
            {row.source === "custom" ? (row.name ?? row.id) : evaluatorLabel(t, row.id)}
          </LinkButton>
          <span className="sub mono">{row.id}</span>
        </>
      ),
    },
    {
      key: "source",
      title: t("v2.evaluators.colSource"),
      render: (row) => <Tag tone={sourceTone(row.source)}>{t(`v2.evaluators.source.${row.source}`)}</Tag>,
    },
    { key: "level", title: t("v2.evaluators.colLevel"), render: (row) => t(`v2.level.${row.level}`, { defaultValue: row.level }) },
    {
      key: "definition",
      title: t("v2.evaluators.colDefinition"),
      render: (row) =>
        row.definition ? t(`v2.evaluators.definition.${row.definition}`) : t("v2.evaluators.definition.managed"),
    },
    {
      key: "gt",
      title: t("v2.evaluators.colGroundTruth"),
      render: (row) =>
        row.requires_ground_truth ? <Tag tone="orange">{t("v2.evaluators.needsGt")}</Tag> : <span className="v2-muted">{t("v2.common.no")}</span>,
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) => (
        <div className="v2-actions">
          <LinkButton onClick={() => setParams({ view: "detail", id: row.id })}>{t("v2.common.view")}</LinkButton>
          {row.source === "custom" && (
            <>
              <LinkButton onClick={() => setParams({ view: "edit", id: row.id })}>{t("v2.common.edit")}</LinkButton>
              <LinkButton danger onClick={() => setDeleting(row)}>
                {t("v2.common.delete")}
              </LinkButton>
            </>
          )}
        </div>
      ),
    },
  ];

  return (
    <>
      <PageHeader title={t("v2.evaluators.title")} desc={t("v2.evaluators.desc")} />
      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload}>{t("v2.common.refresh")}</Button>
          <Button kind="primary" onClick={() => setParams({ view: "new" })} testId="v2-evaluator-new">
            <Plus size={14} aria-hidden="true" />
            {t("v2.evaluators.new")}
          </Button>
          <FilterSelect
            label={t("v2.evaluators.colSource")}
            value={source}
            allLabel={t("v2.common.all")}
            onChange={setSource}
            options={(["builtin", "custom", "third_party"] as const).map((s) => ({
              value: s,
              label: t(`v2.evaluators.source.${s}`),
            }))}
          />
          <FilterSelect
            label={t("v2.evaluators.colLevel")}
            value={level}
            allLabel={t("v2.common.all")}
            onChange={setLevel}
            options={LEVELS.map((l) => ({ value: l, label: t(`v2.level.${l}`) }))}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.evaluators.search")} />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(row) => row.id}
          loading={loading}
          error={error}
          onRetry={reload}
          testId="v2-evaluators-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      <Confirm
        open={deleting !== null}
        title={t("v2.evaluators.deleteTitle")}
        body={t("v2.evaluators.deleteBody", { name: deleting?.name ?? deleting?.id })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}

// ─── detail ────────────────────────────────────────────────────────────────
function EvaluatorDetailView({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const managed = id.startsWith("Builtin.") || id.startsWith("ThirdParty.");
  const list = useLoad(() => api.v2Evaluators(), "evaluators");
  const detail = useLoad<EvaluatorDetail | null>(
    () => (managed ? Promise.resolve(null) : api.v2Evaluator(id)),
    `evaluator:${id}`,
  );
  const row = list.data?.evaluators.find((e) => e.id === id) ?? null;
  const d = detail.data;
  const name = managed ? evaluatorLabel(t, id) : (d?.name ?? row?.name ?? id);

  return (
    <>
      <FlowHeader
        title={name}
        onBack={() => setParams({})}
        end={
          row?.source === "custom" ? (
            <Button kind="primary" onClick={() => setParams({ view: "edit", id })}>
              {t("v2.common.edit")}
            </Button>
          ) : undefined
        }
      />
      {detail.loading || list.loading ? (
        <Spin />
      ) : detail.error ? (
        <Alert tone="error">{detail.error}</Alert>
      ) : (
        <>
          <Card title={t("v2.evaluators.basic")}>
            <Descriptions
              items={[
                { label: t("v2.evaluators.colName"), value: name },
                { label: "ID", value: <span className="mono">{id}</span> },
                {
                  label: t("v2.evaluators.colSource"),
                  value: row ? <Tag tone={sourceTone(row.source)}>{t(`v2.evaluators.source.${row.source}`)}</Tag> : "—",
                },
                {
                  label: t("v2.evaluators.colLevel"),
                  value: t(`v2.level.${d?.level ?? row?.level}`, { defaultValue: d?.level ?? row?.level ?? "—" }),
                },
                {
                  label: t("v2.evaluators.colDefinition"),
                  value: d?.definition ? t(`v2.evaluators.definition.${d.definition}`) : t("v2.evaluators.definition.managed"),
                },
                { label: t("v2.evaluators.model"), value: d?.model_id ? <span className="mono">{d.model_id}</span> : "—" },
                { label: t("v2.evaluators.description"), value: d?.description || "—" },
                {
                  label: t("v2.evaluators.colGroundTruth"),
                  value: row?.requires_ground_truth ? t("v2.evaluators.needsGt") : t("v2.common.no"),
                },
              ]}
            />
          </Card>
          {managed && <Alert>{t("v2.evaluators.managedHint")}</Alert>}
          {d?.instructions && (
            <Card title={t("v2.evaluators.rubric")}>
              <pre className="v2-pre">{d.instructions}</pre>
            </Card>
          )}
          {d?.base_evaluator_id && (
            <Card title={t("v2.evaluators.base")}>
              <span className="mono">{d.base_evaluator_id}</span>
            </Card>
          )}
          {d?.lambda_arn && (
            <Card title={t("v2.evaluators.lambda")}>
              <Descriptions
                one
                items={[
                  { label: "ARN", value: <span className="mono">{d.lambda_arn}</span> },
                  { label: t("v2.evaluators.timeout"), value: `${d.lambda_timeout_s ?? 60}s` },
                ]}
              />
            </Card>
          )}
          {d && d.rating_scale.length > 0 && (
            <Card title={t("v2.evaluators.scale")}>
              <Table
                columns={[
                  { key: "value", title: t("v2.evaluators.scaleValue"), render: (p: ScalePoint) => p.value },
                  { key: "label", title: t("v2.evaluators.scaleLabel"), render: (p: ScalePoint) => p.label },
                  { key: "def", title: t("v2.evaluators.scaleDefinition"), render: (p: ScalePoint) => p.definition },
                ]}
                rows={d.rating_scale}
                rowKey={(p) => `${p.value}:${p.label}`}
              />
            </Card>
          )}
        </>
      )}
    </>
  );
}

// ─── create / edit ─────────────────────────────────────────────────────────
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

const EMPTY: Draft = {
  definition: "judge",
  name: "",
  description: "",
  level: "TRACE",
  model_id: JUDGE_MODEL_OPTIONS[0],
  instructions: "",
  base_evaluator_id: "",
  lambda_arn: "",
  lambda_timeout_s: 60,
  rating_scale: DEFAULT_SCALE,
};

function draftFrom(d: EvaluatorDetail): Draft {
  return {
    definition: d.definition,
    name: d.name ?? d.id,
    description: d.description ?? "",
    level: (d.level as EvaluatorLevel) ?? "TRACE",
    model_id: d.model_id ?? JUDGE_MODEL_OPTIONS[0],
    instructions: d.instructions ?? "",
    base_evaluator_id: d.base_evaluator_id ?? "",
    lambda_arn: d.lambda_arn ?? "",
    lambda_timeout_s: d.lambda_timeout_s ?? 60,
    rating_scale: d.rating_scale.length ? d.rating_scale : DEFAULT_SCALE,
  };
}

function bodyFrom(draft: Draft): EvaluatorUpdateBody {
  if (draft.definition === "derived") {
    return { base_evaluator_id: draft.base_evaluator_id, model_id: draft.model_id, description: draft.description };
  }
  if (draft.definition === "code") {
    return {
      lambda_arn: draft.lambda_arn.trim(),
      lambda_timeout_s: draft.lambda_timeout_s,
      level: draft.level,
      description: draft.description,
    };
  }
  return {
    instructions: draft.instructions,
    model_id: draft.model_id,
    level: draft.level,
    description: draft.description,
    rating_scale: draft.rating_scale,
  };
}

function validate(draft: Draft, editing: boolean, t: (k: string) => string): string | null {
  if (!editing && !NAME_RE.test(draft.name)) return t("v2.evaluators.errName");
  if (draft.definition === "judge") {
    if (draft.instructions.trim().length < 10) return t("v2.evaluators.errRubric");
    if (!PLACEHOLDER_RE.test(draft.instructions)) return t("v2.evaluators.errPlaceholder");
    if (draft.rating_scale.length < 2 || draft.rating_scale.some((p) => !p.label.trim() || !p.definition.trim())) {
      return t("v2.evaluators.errScale");
    }
  }
  if (draft.definition === "derived" && !draft.base_evaluator_id) return t("v2.evaluators.errBase");
  if (draft.definition === "code" && !draft.lambda_arn.trim().startsWith("arn:")) return t("v2.evaluators.errLambda");
  return null;
}

function EvaluatorEditor({ id }: { id: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const editing = id !== null;
  const detail = useLoad<EvaluatorDetail | null>(
    () => (id ? api.v2Evaluator(id) : Promise.resolve(null)),
    `evaluator-edit:${id ?? "new"}`,
  );
  const catalog = useLoad(() => api.v2Evaluators(), "evaluators");
  const [draft, setDraft] = useState<Draft | null>(editing ? null : EMPTY);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  // seed the edit form once the evaluator arrives
  if (editing && draft === null && detail.data) setDraft(draftFrom(detail.data));

  const bases = (catalog.data?.evaluators ?? []).filter((e) => e.source !== "custom");

  if (editing && (detail.loading || draft === null)) {
    return detail.error ? <Alert tone="error">{detail.error}</Alert> : <Spin />;
  }
  if (!draft) return <Spin />;
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });
  const placeholders = LEVEL_PLACEHOLDERS[draft.level];

  const insert = (token: string) => set({ instructions: `${draft.instructions}${draft.instructions && !draft.instructions.endsWith(" ") ? " " : ""}${token}` });

  const save = async () => {
    const problem = validate(draft, editing, t);
    setError(problem);
    if (problem) return;
    setSaving(true);
    try {
      if (id) {
        await api.v2UpdateEvaluator(id, bodyFrom(draft));
        toast("success", t("v2.evaluators.saved"));
        setParams({ view: "detail", id });
      } else {
        const created = await api.v2CreateEvaluator({ ...bodyFrom(draft), name: draft.name } as EvaluatorCreateBody);
        toast("success", t("v2.evaluators.created"));
        setParams({ view: "detail", id: created.evaluator_id });
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const meaning: Record<string, string> = {
    "{context}": t("v2.evaluators.ph.context"),
    "{assistant_turn}": t("v2.evaluators.ph.assistant_turn"),
    "{available_tools}": t("v2.evaluators.ph.available_tools"),
    "{tool_turn}": t("v2.evaluators.ph.tool_turn"),
    "{expected_response}": t("v2.evaluators.ph.expected_response"),
    "{assertions}": t("v2.evaluators.ph.assertions"),
    "{expected_tool_trajectory}": t("v2.evaluators.ph.expected_tool_trajectory"),
    "{actual_tool_trajectory}": t("v2.evaluators.ph.actual_tool_trajectory"),
    "{invoked_skill}": t("v2.evaluators.ph.invoked_skill"),
    "{skill_content}": t("v2.evaluators.ph.skill_content"),
    "{available_skills}": t("v2.evaluators.ph.available_skills"),
    "{user_message}": t("v2.evaluators.ph.user_message"),
  };
  const mapsTo: Record<string, string> = {
    "{context}": "Input",
    "{user_message}": "Input",
    "{assistant_turn}": "Output",
    "{expected_response}": "Expected Output",
    "{actual_tool_trajectory}": "Trajectory",
    "{expected_tool_trajectory}": "Expected Trajectory",
    "{tool_turn}": "Trajectory",
  };
  const tokens = [
    ...placeholders.core.map((token) => ({ token, gt: false })),
    ...placeholders.groundTruth.map((token) => ({ token, gt: true })),
    ...(placeholders.skill ?? []).map((token) => ({ token, gt: false })),
  ];

  return (
    <>
      <FlowHeader
        title={editing ? t("v2.evaluators.editTitle") : t("v2.evaluators.newTitle")}
        onBack={() => setParams(id ? { view: "detail", id } : {})}
        end={
          <Button kind="primary" disabled={saving} onClick={() => void save()} testId="v2-evaluator-save">
            {editing ? t("v2.common.save") : t("v2.common.create")}
          </Button>
        }
      />
      {error && <Alert tone="error">{error}</Alert>}
      <Card title={t("v2.evaluators.basic")}>
        <div className="v2-form cols-2">
          <Field label={t("v2.evaluators.definitionLabel")} full>
            <div className="v2-options">
              {(["judge", "derived", "code"] as const).map((def) => (
                <OptionCard
                  key={def}
                  title={t(`v2.evaluators.definition.${def}`)}
                  desc={t(`v2.evaluators.definitionDesc.${def}`)}
                  on={draft.definition === def}
                  disabled={editing && draft.definition !== def}
                  onClick={() => set({ definition: def })}
                  testId={`v2-evaluator-def-${def}`}
                />
              ))}
            </div>
          </Field>
          <Field label={t("v2.evaluators.colName")} required={!editing} hint={t("v2.evaluators.nameHint")}>
            <input
              className="v2-input"
              value={draft.name}
              disabled={editing}
              onChange={(e) => set({ name: e.target.value })}
              placeholder="answer_faithfulness"
              data-testid="v2-evaluator-name"
            />
          </Field>
          {draft.definition !== "derived" && (
            <Field label={t("v2.evaluators.colLevel")} required hint={t("v2.evaluators.levelHint")}>
              <select className="v2-select" value={draft.level} onChange={(e) => set({ level: e.target.value as EvaluatorLevel })}>
                {LEVELS.map((l) => (
                  <option key={l} value={l}>
                    {t(`v2.level.${l}`)}
                  </option>
                ))}
              </select>
            </Field>
          )}
          <Field label={t("v2.evaluators.description")} full>
            <input className="v2-input" value={draft.description} maxLength={1000} onChange={(e) => set({ description: e.target.value })} />
          </Field>
          {draft.definition !== "code" && (
            <Field label={t("v2.evaluators.model")} required hint={t("v2.evaluators.modelHint")}>
              <select className="v2-select" value={draft.model_id} onChange={(e) => set({ model_id: e.target.value })}>
                {(JUDGE_MODEL_OPTIONS.includes(draft.model_id) ? JUDGE_MODEL_OPTIONS : [draft.model_id, ...JUDGE_MODEL_OPTIONS]).map((m) => (
                  <option key={m} value={m}>
                    {m}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {draft.definition === "derived" && (
            <Field label={t("v2.evaluators.base")} required>
              <select className="v2-select" value={draft.base_evaluator_id} disabled={editing} onChange={(e) => set({ base_evaluator_id: e.target.value })}>
                <option value="">{t("v2.common.choose")}</option>
                {bases.map((b) => (
                  <option key={b.id} value={b.id}>
                    {evaluatorLabel(t, b.id)} · {b.id}
                  </option>
                ))}
              </select>
            </Field>
          )}
          {draft.definition === "code" && (
            <>
              <Field label={t("v2.evaluators.lambda")} required full hint={t("v2.evaluators.lambdaHint")}>
                <input
                  className="v2-input mono"
                  value={draft.lambda_arn}
                  onChange={(e) => set({ lambda_arn: e.target.value })}
                  placeholder="arn:aws:lambda:us-west-2:123456789012:function:my-evaluator"
                />
              </Field>
              <Field label={t("v2.evaluators.timeout")}>
                <input
                  className="v2-input"
                  type="number"
                  min={1}
                  max={300}
                  value={draft.lambda_timeout_s}
                  onChange={(e) => set({ lambda_timeout_s: Math.max(1, Math.min(300, Number(e.target.value) || 60)) })}
                />
              </Field>
            </>
          )}
        </div>
      </Card>

      {draft.definition === "judge" && (
        <>
          <Card title={t("v2.evaluators.rubric")} sub={t("v2.evaluators.rubricSub")}>
            <textarea
              className="v2-textarea"
              rows={8}
              value={draft.instructions}
              onChange={(e) => set({ instructions: e.target.value })}
              placeholder={t("v2.evaluators.rubricPlaceholder")}
              data-testid="v2-evaluator-rubric"
            />
          </Card>
          <Card title={t("v2.evaluators.mapping")} sub={t("v2.evaluators.mappingSub")}>
            <Table
              columns={[
                { key: "token", title: t("v2.evaluators.mapVar"), render: (r: { token: string; gt: boolean }) => <code>{r.token}</code> },
                {
                  key: "meaning",
                  title: t("v2.evaluators.mapMeaning"),
                  render: (r: { token: string; gt: boolean }) => (
                    <>
                      {meaning[r.token] ?? r.token}
                      {r.gt && (
                        <>
                          {" "}
                          <Tag tone="orange">{t("v2.evaluators.needsGt")}</Tag>
                        </>
                      )}
                    </>
                  ),
                },
                { key: "maps", title: t("v2.evaluators.mapField"), render: (r: { token: string; gt: boolean }) => mapsTo[r.token] ?? "—" },
                {
                  key: "ops",
                  title: t("v2.common.actions"),
                  className: "right",
                  render: (r: { token: string; gt: boolean }) => (
                    <LinkButton onClick={() => insert(r.token)}>{t("v2.evaluators.insert")}</LinkButton>
                  ),
                },
              ]}
              rows={tokens}
              rowKey={(r) => r.token}
            />
          </Card>
          <Card title={t("v2.evaluators.scale")} sub={t("v2.evaluators.scaleSub")}>
            <div className="v2-stack">
              {draft.rating_scale.map((p, i) => (
                <div key={i} className="v2-row" style={{ flexWrap: "nowrap" }}>
                  <input
                    className="v2-input"
                    style={{ width: 90 }}
                    type="number"
                    step="0.1"
                    value={p.value}
                    aria-label={t("v2.evaluators.scaleValue")}
                    onChange={(e) =>
                      set({ rating_scale: draft.rating_scale.map((x, j) => (j === i ? { ...x, value: Number(e.target.value) } : x)) })
                    }
                  />
                  <input
                    className="v2-input"
                    style={{ width: 160 }}
                    value={p.label}
                    placeholder={t("v2.evaluators.scaleLabel")}
                    aria-label={t("v2.evaluators.scaleLabel")}
                    onChange={(e) =>
                      set({ rating_scale: draft.rating_scale.map((x, j) => (j === i ? { ...x, label: e.target.value } : x)) })
                    }
                  />
                  <input
                    className="v2-input"
                    value={p.definition}
                    placeholder={t("v2.evaluators.scaleDefinition")}
                    aria-label={t("v2.evaluators.scaleDefinition")}
                    onChange={(e) =>
                      set({ rating_scale: draft.rating_scale.map((x, j) => (j === i ? { ...x, definition: e.target.value } : x)) })
                    }
                  />
                  <Button
                    size="sm"
                    disabled={draft.rating_scale.length <= 2}
                    title={t("v2.common.delete")}
                    onClick={() => set({ rating_scale: draft.rating_scale.filter((_, j) => j !== i) })}
                  >
                    <Trash2 size={13} aria-hidden="true" />
                  </Button>
                </div>
              ))}
              <div>
                <Button
                  size="sm"
                  onClick={() => set({ rating_scale: [...draft.rating_scale, { value: 0.5, label: "", definition: "" }] })}
                >
                  <Plus size={13} aria-hidden="true" />
                  {t("v2.evaluators.scaleAdd")}
                </Button>
              </div>
            </div>
            <p className="v2-muted" style={{ marginTop: 12, fontSize: 13 }}>
              {t("v2.evaluators.outputFields")}
            </p>
          </Card>
        </>
      )}
    </>
  );
}

export function V2Evaluators() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const id = params.get("id");
  if (view === "new") return <EvaluatorEditor id={null} />;
  if (view === "edit" && id) return <EvaluatorEditor key={id} id={id} />;
  if (view === "detail" && id) return <EvaluatorDetailView key={id} id={id} />;
  return <EvaluatorList />;
}
