// 数据处理 · AgentCore 生成 source (P2): the source fields, the preview of the
// sessions a run would send, the generation options of the logic step, and the
// last run's generation report. Pure rules live in lib/pipelineAgentcore.
import { Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { AgentInfo, V2PipelineBody, V2PipelineRun, V2PipelineSource, V2Range } from "../../../lib/api";
import type { EvaluationRunInfo, EvaluationRunResults } from "../../../lib/evaluation";
import {
  AGENTCORE_MAX_SESSIONS,
  type AgentcorePreview,
  clampScore,
  clampSessions,
  DEFAULT_MAX_SCORE,
  FRESH_MINUTES,
  GENERATION_COUNTS,
  MANAGED_REDACTION_IDS,
  MAX_CUSTOM_IDENTIFIERS,
  REDACTION_PROBLEM_KEY,
  redactionProblem,
  type RunPreviewRow,
  type RunVerdict,
  scenarioTypeOf,
  sessionsFromOf,
} from "../../../lib/pipelineAgentcore";
import { fmtScore, fmtTime, RANGES, rangeLabel, scoreTone } from "../../format";
import { Alert, Button, Card, Descriptions, Field, LinkButton, Segmented, Select, Table, Tag, type TagTone } from "../../ui";

type Source = V2PipelineSource;
type Processing = V2PipelineBody["processing"];

export function AgentcoreSourceFields({
  source,
  agents,
  agentsLoading,
  runs,
  runsLoading,
  onChange,
}: {
  source: Source;
  agents: AgentInfo[];
  agentsLoading: boolean;
  /** the picked agent's completed evaluator runs, newest first (P3) */
  runs: EvaluationRunInfo[];
  runsLoading: boolean;
  onChange: (patch: Partial<Source>) => void;
}) {
  const { t } = useTranslation();
  const scenario = scenarioTypeOf(source);
  const fromRun = sessionsFromOf(source) === "run";
  const picked = agents.some((a) => a.id === source.agent_id);
  // a saved agent that is no longer offered (deleted / not active) stays visible, flagged
  const options = [
    ...(source.agent_id && !picked && !agentsLoading
      ? [{ value: source.agent_id, label: t("v2.pipelines.ac.agentGone", { name: source.agent ?? source.agent_id }) }]
      : []),
    ...agents.map((a) => ({ value: a.id, label: `${a.display_name || a.name} · ${t(`v2.pipelines.ac.method.${a.method}`, { defaultValue: a.method })}` })),
  ];
  const runListed = runs.some((r) => r.id === source.batch_run_id);
  const runOptions = [
    ...(source.batch_run_id && !runListed && !runsLoading ? [{ value: source.batch_run_id, label: t("v2.pipelines.ac.runGone", { id: source.batch_run_id }) }] : []),
    ...runs.map((r) => ({
      value: r.id,
      label: t("v2.pipelines.ac.runOption", { name: r.name || r.id, time: fmtTime(r.created_at), count: r.session_ids.length }),
    })),
  ];
  const allSessions = source.max_score === null;
  return (
    <div className="v2-form cols-2">
      <Field label="Agent" required hint={t("v2.pipelines.ac.agentHint")}>
        <Select
          value={source.agent_id ?? ""}
          placeholder={agentsLoading ? t("v2.pipelines.ac.agentsLoading") : t("v2.common.choose")}
          options={options}
          // a run belongs to one agent: picking another agent clears the run
          onChange={(v) => onChange({ agent_id: v || null, ...(fromRun ? { batch_run_id: "" } : {}) })}
          testId="v2-pipeline-ac-agent"
        />
      </Field>
      <Field label={t("v2.pipelines.ac.sessionsFrom")} hint={fromRun ? t("v2.pipelines.ac.fromRunHint") : t("v2.pipelines.ac.fromTrafficHint")}>
        <Segmented
          value={fromRun ? "run" : "traffic"}
          onChange={(v) => onChange(v === "run" ? { batch_run_id: source.batch_run_id ?? "", max_score: source.max_score === undefined ? DEFAULT_MAX_SCORE : source.max_score } : { batch_run_id: null })}
          options={[
            { value: "traffic", label: t("v2.pipelines.ac.fromTraffic"), testId: "v2-pipeline-ac-from-traffic" },
            { value: "run", label: t("v2.pipelines.ac.fromRun"), testId: "v2-pipeline-ac-from-run" },
          ]}
        />
      </Field>
      {fromRun ? (
        <>
          <Field
            label={t("v2.pipelines.ac.run")}
            required
            full
            hint={!source.agent_id ? t("v2.pipelines.ac.runPickAgent") : !runsLoading && runs.length === 0 ? t("v2.pipelines.ac.runsNone") : t("v2.pipelines.ac.runHint")}
          >
            <Select
              value={source.batch_run_id ?? ""}
              placeholder={runsLoading ? t("v2.pipelines.ac.runsLoading") : t("v2.common.choose")}
              options={runOptions}
              disabled={!source.agent_id}
              onChange={(v) => onChange({ batch_run_id: v })}
              testId="v2-pipeline-ac-run"
            />
          </Field>
          <Field label={t("v2.pipelines.ac.maxScore")} hint={allSessions ? t("v2.pipelines.ac.allSessionsHint") : t("v2.pipelines.ac.maxScoreHint")}>
            <div style={{ display: "flex", gap: 14, alignItems: "center", flexWrap: "wrap" }}>
              <input
                className="v2-input"
                style={{ width: 120 }}
                type="number"
                min={0}
                max={1}
                step={0.05}
                disabled={allSessions}
                value={allSessions ? "" : (source.max_score ?? DEFAULT_MAX_SCORE)}
                onChange={(e) => onChange({ max_score: clampScore(Number(e.target.value)) })}
                data-testid="v2-pipeline-ac-max-score"
              />
              <label className="v2-check">
                <input
                  type="checkbox"
                  checked={allSessions}
                  onChange={(e) => onChange({ max_score: e.target.checked ? null : DEFAULT_MAX_SCORE })}
                  data-testid="v2-pipeline-ac-all-sessions"
                />
                {t("v2.pipelines.ac.allSessions")}
              </label>
            </div>
          </Field>
        </>
      ) : (
        <>
          <Field label={t("v2.common.timeRange")}>
            <Select value={source.range} options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))} onChange={(v) => onChange({ range: v as V2Range })} />
          </Field>
          <Field label={t("v2.pipelines.sessionStatus")}>
            <Segmented
              value={source.status}
              onChange={(v) => onChange({ status: v })}
              options={(["all", "ok", "error"] as const).map((s) => ({ value: s, label: t(`v2.pipelines.status.${s}`) }))}
            />
          </Field>
        </>
      )}
      <Field label={t("v2.pipelines.maxSessions")} hint={t("v2.pipelines.ac.maxSessionsHint", { max: AGENTCORE_MAX_SESSIONS })}>
        <input
          className="v2-input"
          type="number"
          min={1}
          max={AGENTCORE_MAX_SESSIONS}
          value={source.max_sessions}
          onChange={(e) => onChange({ max_sessions: clampSessions(Number(e.target.value)) })}
          data-testid="v2-pipeline-ac-max"
        />
      </Field>
      <Field
        label={t("v2.pipelines.ac.scenarioType")}
        full
        hint={scenario === "DYNAMIC" ? (fromRun ? t("v2.pipelines.ac.dynamicRunHint") : t("v2.pipelines.ac.dynamicHint")) : t("v2.pipelines.ac.fixedHint")}
      >
        <Segmented
          value={scenario}
          onChange={(v) => onChange({ scenario_type: v })}
          options={[
            { value: "FIXED", label: t("v2.pipelines.ac.fixed") },
            { value: "DYNAMIC", label: t("v2.pipelines.ac.dynamic") },
          ]}
        />
      </Field>
    </div>
  );
}

const VERDICT_TONE: Record<RunVerdict, TagTone> = { send: "blue", above: "gray", unscored: "orange", unsupported: "red", overflow: "gray" };

/** The run-sourced preview (P3): the run's sessions with their lowest normalized
 *  score and whether a run would send them. */
export function RunPreviewCard({
  rows,
  results,
  pickedRun,
  loading,
  error,
}: {
  rows: RunPreviewRow[];
  results: EvaluationRunResults | null;
  /** whether a run is chosen yet */
  pickedRun: boolean;
  loading: boolean;
  error: string | null;
}) {
  const { t } = useTranslation();
  const sending = rows.filter((r) => r.verdict === "send").length;
  const unavailable = results && !results.available;
  return (
    <Card title={t("v2.pipelines.preview")} sub={loading || !pickedRun || unavailable ? undefined : t("v2.pipelines.ac.previewSub", { count: sending })}>
      {pickedRun && !loading && unavailable && (
        <Alert tone="warn">{t("v2.pipelines.ac.runResultsUnavailable", { reason: results.detail || results.reason || "—" })}</Alert>
      )}
      {pickedRun && !loading && results?.available && (
        <Alert>
          {[results.truncated && t("v2.pipelines.ac.runResultsTruncated"), t("v2.pipelines.ac.runPreviewNote")].filter(Boolean).join(" ")}
        </Alert>
      )}
      <Table
        columns={[
          { key: "sid", title: t("v2.traces.colSession"), render: (r: RunPreviewRow) => <span className="mono">{r.session_id}</span> },
          {
            key: "low",
            title: t("v2.pipelines.ac.colLowest"),
            className: "num",
            render: (r: RunPreviewRow) => (r.lowest === null ? "—" : <span className={`v2-score ${scoreTone(r.lowest)}`}>{fmtScore(r.lowest)}</span>),
          },
          {
            key: "send",
            title: t("v2.pipelines.ac.colSend"),
            render: (r: RunPreviewRow) => <Tag tone={VERDICT_TONE[r.verdict]}>{t(`v2.pipelines.ac.verdict.${r.verdict}`)}</Tag>,
          },
        ]}
        rows={rows}
        rowKey={(r) => r.session_id}
        loading={loading}
        error={error}
        empty={pickedRun ? t("v2.pipelines.ac.runPreviewEmpty") : t("v2.pipelines.ac.runPickRun")}
        testId="v2-pipeline-ac-run-preview"
      />
    </Card>
  );
}

export function AgentcorePreviewCard({
  preview,
  picked,
  loading,
  error,
}: {
  preview: AgentcorePreview;
  /** whether an agent is chosen yet */
  picked: boolean;
  loading: boolean;
  error: string | null;
}) {
  const { t } = useTranslation();
  type Row = AgentcorePreview["picked"][number] & { held: boolean };
  const rows: Row[] = [...preview.picked.map((s) => ({ ...s, held: false })), ...preview.fresh.map((s) => ({ ...s, held: true }))];
  const notes = [
    preview.fresh.length > 0 && t("v2.pipelines.ac.previewFresh", { count: preview.fresh.length, minutes: FRESH_MINUTES }),
    preview.overflow > 0 && t("v2.pipelines.ac.previewOverflow", { count: preview.overflow }),
    preview.singleTurn > 0 && t("v2.pipelines.ac.previewSingleTurn", { count: preview.singleTurn }),
  ].filter(Boolean);
  return (
    <Card
      title={t("v2.pipelines.preview")}
      sub={loading || !picked ? undefined : t("v2.pipelines.ac.previewSub", { count: preview.picked.length })}
    >
      {picked && !loading && (
        <Alert>
          {[...notes, t("v2.pipelines.ac.previewPresent")].join(" ")}
        </Alert>
      )}
      <Table
        columns={[
          { key: "sid", title: t("v2.traces.colSession"), render: (s: Row) => <span className="mono">{s.session_id}</span> },
          { key: "traces", title: t("v2.pipelines.traceCount"), className: "num", render: (s: Row) => s.traces },
          {
            key: "errors",
            title: t("v2.traces.colStatus"),
            render: (s: Row) => (s.errors ? <Tag tone="red">{t("v2.traces.error", { count: s.errors })}</Tag> : <Tag tone="green">{t("v2.traces.ok")}</Tag>),
          },
          { key: "last", title: t("v2.pipelines.lastActive"), className: "nowrap", render: (s: Row) => fmtTime(s.last) },
          {
            key: "send",
            title: t("v2.pipelines.ac.colSend"),
            render: (s: Row) =>
              s.held ? (
                <Tag tone="orange" title={t("v2.pipelines.ac.heldTitle", { minutes: FRESH_MINUTES })}>
                  {t("v2.pipelines.ac.held")}
                </Tag>
              ) : (
                <Tag tone="blue">{t("v2.pipelines.ac.send")}</Tag>
              ),
          },
        ]}
        rows={rows}
        rowKey={(s) => s.session_id}
        loading={loading}
        error={error}
        empty={picked ? t("v2.pipelines.previewEmpty") : t("v2.pipelines.ac.previewPickAgent")}
        testId="v2-pipeline-ac-preview"
      />
    </Card>
  );
}

/** The logic step of an agentcore source: ground truth (FIXED), redaction, dedupe. */
export function AgentcoreLogic({
  source,
  processing,
  onSource,
  onProc,
}: {
  source: Source;
  processing: Processing;
  onSource: (patch: Partial<Source>) => void;
  onProc: (patch: Partial<Processing>) => void;
}) {
  const { t } = useTranslation();
  const fixed = scenarioTypeOf(source) === "FIXED";
  const redaction = source.redaction ?? { identifiers: [], custom: [] };
  const setRedaction = (patch: Partial<typeof redaction>) => onSource({ redaction: { ...redaction, ...patch } });
  const toggleId = (id: string, on: boolean) =>
    setRedaction({ identifiers: on ? [...redaction.identifiers.filter((x) => x !== id), id] : redaction.identifiers.filter((x) => x !== id) });
  const setCustom = (i: number, patch: Partial<{ name: string; regex: string }>) =>
    setRedaction({ custom: redaction.custom.map((c, j) => (j === i ? { ...c, ...patch } : c)) });
  const problem = redactionProblem(redaction);

  return (
    <>
      <Card title={t("v2.pipelines.ac.groundTruth")}>
        <div className="v2-form">
          {fixed ? (
            <>
              <label className="v2-check">
                <input
                  type="checkbox"
                  checked={processing.keep_replies !== false}
                  onChange={(e) => onProc({ keep_replies: e.target.checked })}
                  data-testid="v2-pipeline-ac-expected-response"
                />
                {t("v2.pipelines.ac.expectedResponse")}
              </label>
              <label className="v2-check">
                <input
                  type="checkbox"
                  checked={source.expected_trajectory !== false}
                  onChange={(e) => onSource({ expected_trajectory: e.target.checked })}
                  data-testid="v2-pipeline-ac-expected-trajectory"
                />
                {t("v2.pipelines.ac.expectedTrajectory")}
              </label>
              <Alert tone="warn">{t("v2.pipelines.ac.groundTruthNote")}</Alert>
            </>
          ) : (
            <Alert>{t("v2.pipelines.ac.dynamicNoGroundTruth")}</Alert>
          )}
          <label className="v2-check">
            <input type="checkbox" checked={processing.dedupe} onChange={(e) => onProc({ dedupe: e.target.checked })} />
            {t("v2.pipelines.dedupe")}
          </label>
        </div>
      </Card>
      <Card title={t("v2.pipelines.ac.redaction")} sub={t("v2.pipelines.ac.redactionSub")}>
        <div className="v2-form">
          <Field label={t("v2.pipelines.ac.managedIds")} full>
            <div style={{ display: "flex", flexWrap: "wrap", gap: "8px 18px" }} data-testid="v2-pipeline-ac-managed">
              {MANAGED_REDACTION_IDS.map((id) => (
                <label key={id} className="v2-check" title={id}>
                  <input type="checkbox" checked={redaction.identifiers.includes(id)} onChange={(e) => toggleId(id, e.target.checked)} />
                  {t(`v2.pipelines.ac.pii.${id}`)}
                </label>
              ))}
            </div>
          </Field>
          <Field label={t("v2.pipelines.ac.customIds")} full hint={t("v2.pipelines.ac.customHint", { max: MAX_CUSTOM_IDENTIFIERS })} error={problem ? t(REDACTION_PROBLEM_KEY[problem]) : null}>
            <div style={{ display: "grid", gap: 8 }}>
              {redaction.custom.map((c, i) => (
                <div key={i} style={{ display: "grid", gridTemplateColumns: "minmax(0, 200px) minmax(0, 1fr) auto", gap: 8, alignItems: "center" }}>
                  <input
                    className="v2-input mono"
                    value={c.name}
                    maxLength={128}
                    placeholder="ORDER_ID"
                    aria-label={t("v2.pipelines.ac.customName")}
                    onChange={(e) => setCustom(i, { name: e.target.value })}
                  />
                  <input
                    className="v2-input mono"
                    value={c.regex}
                    maxLength={200}
                    placeholder="ORD-[0-9]+"
                    aria-label={t("v2.pipelines.ac.customRegex")}
                    onChange={(e) => setCustom(i, { regex: e.target.value })}
                  />
                  <LinkButton danger onClick={() => setRedaction({ custom: redaction.custom.filter((_, j) => j !== i) })} title={t("v2.common.delete")}>
                    <Trash2 size={14} aria-hidden="true" />
                  </LinkButton>
                </div>
              ))}
              <div>
                <Button
                  disabled={redaction.custom.length >= MAX_CUSTOM_IDENTIFIERS}
                  onClick={() => setRedaction({ custom: [...redaction.custom, { name: "", regex: "" }] })}
                  testId="v2-pipeline-ac-add-custom"
                >
                  <Plus size={14} aria-hidden="true" />
                  {t("v2.pipelines.ac.addCustom")}
                </Button>
              </div>
            </div>
          </Field>
        </div>
      </Card>
    </>
  );
}

const LABEL = { fontSize: 13, fontWeight: 600, color: "var(--v2-ink-2)", marginBottom: 6 } as const;

const STATUS_TONE: Record<string, TagTone> = {
  IN_PROGRESS: "blue",
  COMPLETED: "green",
  COMPLETED_WITH_ERRORS: "orange",
  FAILED: "red",
};

/** The last agentcore run: what AWS generated, what failed and why, redaction
 *  findings, and what the merge skipped. */
export function GenerationRunCard({ run }: { run: V2PipelineRun }) {
  const { t } = useTranslation();
  const gen = run.generation;
  if (!gen) return null;
  const results = gen.results ?? {};
  const counts = GENERATION_COUNTS.filter((k) => results[k] !== undefined);
  const errors = gen.error_details ?? [];
  const findings = Object.entries(gen.redaction?.findings ?? {});
  return (
    <Card
      title={t("v2.pipelines.ac.lastRun")}
      sub={fmtTime(run.at)}
      end={
        <span style={{ display: "inline-flex", gap: 6 }}>
          <Tag tone="gray">{t(`v2.pipelines.ac.stage.${gen.stage}`, { defaultValue: gen.stage })}</Tag>
          {gen.status && <Tag tone={STATUS_TONE[gen.status] ?? "gray"}>{t(`v2.pipelines.ac.genStatus.${gen.status}`, { defaultValue: gen.status })}</Tag>}
        </span>
      }
      testId="v2-pipeline-ac-last-run"
    >
      <Descriptions
        items={[
          { label: t("v2.pipelines.ac.scenarioType"), value: gen.scenario_type === "DYNAMIC" ? t("v2.pipelines.ac.dynamic") : t("v2.pipelines.ac.fixed") },
          ...(gen.batch_run_id ? [{ label: t("v2.pipelines.ac.sourceRun"), value: <span className="mono">{gen.batch_run_id}</span> }] : []),
          { label: gen.batch_run_id ? t("v2.pipelines.ac.scannedRun") : t("v2.pipelines.ac.scanned"), value: run.scanned },
          { label: t("v2.pipelines.ac.requested"), value: gen.requested },
          { label: t("v2.pipelines.ac.added"), value: run.added },
          ...counts.map((k) => ({ label: t(`v2.pipelines.ac.count.${k}`), value: results[k] })),
          ...(gen.id ? [{ label: t("v2.pipelines.ac.generationId"), value: <span className="mono">{gen.id}</span> }] : []),
        ]}
      />
      {gen.failure_reason && (
        <div style={{ marginTop: 12 }}>
          <Alert tone="error">{gen.failure_reason}</Alert>
        </div>
      )}
      {errors.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={LABEL}>{t("v2.pipelines.ac.errorDetails")}</div>
          {errors.map((e) => (
            <Alert key={e.code} tone="warn">
              <b className="mono">{e.code}</b> · {t("v2.pipelines.ac.errorCount", { count: e.count })}
              {" — "}
              {t(`v2.pipelines.ac.errorHint.${e.code}`, { defaultValue: e.message })}
              {e.session_ids.length > 0 && (
                <span className="sub mono" style={{ display: "block", wordBreak: "break-all" }}>
                  {e.session_ids.join(", ")}
                </span>
              )}
            </Alert>
          ))}
        </div>
      )}
      {gen.redaction && (
        <div style={{ marginTop: 12 }}>
          <span style={LABEL}>{t("v2.pipelines.ac.redactionFindings", { count: gen.redaction.scanned })}</span>{" "}
          {findings.length ? (
            findings.map(([name, n]) => (
              <Tag key={name} tone="outline">
                <span className="mono">{name}</span> × {n}
              </Tag>
            ))
          ) : (
            <span className="v2-muted">{t("v2.pipelines.ac.noFindings")}</span>
          )}
        </div>
      )}
      {run.skipped.length > 0 && (
        <div style={{ marginTop: 12 }}>
          <div style={LABEL}>{t("v2.pipelines.ac.skipped", { count: run.skipped.length })}</div>
          <ul style={{ margin: 0, paddingLeft: 18, fontSize: 13 }}>
            {run.skipped.slice(0, 50).map((s, i) => (
              <li key={`${s.session_id}-${i}`}>
                <span className="mono">{s.session_id}</span> · {t(`v2.skipReason.${s.reason}`, { defaultValue: s.reason })}
                {s.code && (
                  <>
                    {" "}
                    <span className="mono v2-muted">({s.code})</span>
                  </>
                )}
              </li>
            ))}
          </ul>
          {run.skipped.length > 50 && <span className="v2-muted">{t("v2.pipelines.ac.skippedMore", { count: run.skipped.length - 50 })}</span>}
        </div>
      )}
    </Card>
  );
}
