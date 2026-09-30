import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { DiffPanes } from "../../../components/DiffPanes";
import { api, errorMessage } from "../../../lib/api";
import type { EvaluationRunInfo, RunRecommendation, RunRecommendationKind } from "../../../lib/evaluation";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, Spin, Table, Tag, type TagTone } from "../../ui";

const POLL_MS = 10000;
const ACTIVE = new Set(["PENDING", "IN_PROGRESS"]);
const STATUS_TONE: Record<string, TagTone> = {
  PENDING: "gray",
  IN_PROGRESS: "blue",
  COMPLETED: "green",
  FAILED: "red",
  DELETING: "gray",
};

interface ToolRow {
  key: number;
  name: string;
  description: string;
  /** sent to the job; a pre-filled tool without a description starts excluded */
  include: boolean;
}

/**
 * 优化建议 — AgentCore Recommendations seeded from one completed evaluation run.
 * The traces are that run's batch evaluation. A Managed Harness's system prompt and
 * tool descriptions arrive pre-filled from GetHarness; for any other agent the
 * operator types what the backend could not read (both inputs are required).
 */
export function RunRecommendations({ run }: { run: EvaluationRunInfo }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { can } = useAuth();
  const [tick, setTick] = useState(0);
  const inputs = useLoad(() => api.runRecommendationInputs(run.id), `rec-inputs:${run.id}`);
  const list = useLoad(() => api.runRecommendations(run.id), `rec-list:${run.id}:${tick}`);
  const recs = useMemo(() => list.data?.recommendations ?? [], [list.data]);
  const active = recs.some((r) => ACTIVE.has(r.status));
  useEffect(() => {
    if (!active) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [active]);

  const [open, setOpen] = useState<boolean | null>(null);
  const [wantPrompt, setWantPrompt] = useState(true);
  const [wantTools, setWantTools] = useState(false);
  const [prompt, setPrompt] = useState("");
  const [evaluator, setEvaluator] = useState("");
  const [tools, setTools] = useState<ToolRow[]>([]);
  const [nextKey, setNextKey] = useState(0);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);

  // seed the editable form once from what the backend could read
  const seed = inputs.data;
  useEffect(() => {
    if (!seed) return;
    setPrompt(seed.system_prompt);
    setEvaluator(seed.default_evaluator);
    setTools(seed.tools.map((tool, i) => ({ key: i, name: tool.name, description: tool.description, include: !!tool.description.trim() })));
    setNextKey(seed.tools.length);
    setWantTools(seed.tools_eligible && seed.tools.some((tool) => tool.description.trim()));
  }, [seed]);

  if (inputs.loading && !seed) return <Card title={t("v2.rec.title")}><Spin /></Card>;
  if (inputs.error && !seed) return <Card title={t("v2.rec.title")}><Alert tone="error">{inputs.error}</Alert></Card>;
  if (!seed) return null;

  const source = seed.source;
  const formOpen = open ?? recs.length === 0;
  const filledTools = tools.filter((row) => row.include && row.name.trim());
  const promptMissing = wantPrompt && !prompt.trim();
  const toolsMissing = wantTools && filledTools.length === 0;
  const descMissing = wantTools && filledTools.some((row) => !row.description.trim());
  const invalid = (!wantPrompt && !wantTools) || promptMissing || toolsMissing || descMissing;
  const mayRun = can("eval.run");

  const setTool = (key: number, patch: Partial<ToolRow>) =>
    setTools((rows) => rows.map((row) => (row.key === key ? { ...row, ...patch } : row)));
  const addTool = () => {
    setTools((rows) => [...rows, { key: nextKey, name: "", description: "", include: true }]);
    setNextKey((n) => n + 1);
  };

  const submit = async () => {
    setTouched(true);
    if (invalid) return;
    const kinds: RunRecommendationKind[] = [
      ...(wantPrompt ? (["system_prompt"] as const) : []),
      ...(wantTools ? (["tool_descriptions"] as const) : []),
    ];
    setBusy(true);
    try {
      await api.createRunRecommendations(run.id, {
        kinds,
        input_source: source,
        ...(wantPrompt ? { system_prompt: prompt, evaluator } : {}),
        ...(wantTools
          ? { tools: filledTools.map((row) => ({ name: row.name.trim(), description: row.description.trim() })) }
          : {}),
      });
      toast("success", t("v2.rec.started"));
      setOpen(false);
      setTouched(false);
      setTick((n) => n + 1);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const sourceAlert =
    source === "harness" ? (
      <Alert tone="success">{t("v2.rec.sourceHarness")}</Alert>
    ) : source === "spec" ? (
      <Alert>{t("v2.rec.sourceSpec")}</Alert>
    ) : (
      <Alert tone="warn">{t("v2.rec.sourceManual")}</Alert>
    );

  const form = (
    <div className="v2-form" data-testid="v2-rec-form">
      {sourceAlert}
      {seed.notes.map((note) => (
        <Alert key={`${note.code}:${note.tool}`} tone="warn">
          {t(`v2.rec.note.${note.code}`, { tool: note.tool, detail: note.detail })}
        </Alert>
      ))}
      <Field label={t("v2.rec.kinds")}>
        <div className="v2-checks">
          <label className="v2-check">
            <input type="checkbox" checked={wantPrompt} onChange={(e) => setWantPrompt(e.target.checked)} data-testid="v2-rec-kind-sp" />
            {t("expPage.recTypePrompt")}
          </label>
          <label className={seed.tools_eligible ? "v2-check" : "v2-check disabled"} title={seed.tools_eligible ? undefined : t("v2.rec.toolsIneligible")}>
            <input
              type="checkbox"
              checked={wantTools}
              disabled={!seed.tools_eligible}
              onChange={(e) => setWantTools(e.target.checked)}
              data-testid="v2-rec-kind-td"
            />
            {t("expPage.recTypeTools")}
          </label>
        </div>
        {!seed.tools_eligible && <span className="v2-muted">{t("v2.rec.toolsIneligible")}</span>}
      </Field>
      {wantPrompt && (
        <>
          <Field
            label={t("v2.rec.currentPrompt")}
            required
            hint={source === "manual" ? t("v2.rec.promptHintManual") : t("v2.rec.promptHintPrefilled")}
            error={touched && promptMissing ? t("v2.rec.promptRequired") : null}
            full
          >
            <textarea
              className="v2-textarea code"
              rows={10}
              value={prompt}
              maxLength={20000}
              placeholder={t("v2.rec.promptPlaceholder")}
              onChange={(e) => setPrompt(e.target.value)}
              data-testid="v2-rec-prompt"
            />
          </Field>
          <Field label={t("v2.rec.evaluator")} hint={t("v2.rec.evaluatorHint")}>
            <select className="v2-select" value={evaluator} onChange={(e) => setEvaluator(e.target.value)} data-testid="v2-rec-evaluator">
              {seed.evaluators.map((id) => (
                <option key={id} value={id}>
                  {id}
                </option>
              ))}
            </select>
          </Field>
        </>
      )}
      {wantTools && (
        <Field
          label={t("v2.rec.currentTools")}
          required
          hint={source === "manual" ? t("v2.rec.toolsHintManual") : t("v2.rec.toolsHintPrefilled")}
          error={touched && toolsMissing ? t("v2.rec.toolsRequired") : touched && descMissing ? t("v2.rec.toolDescRequired") : null}
          full
        >
          <div className="v2-form" style={{ gap: 10 }}>
            {tools.map((row) => (
              <div key={row.key} className="v2-row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                <label className="v2-check" style={{ paddingTop: 8 }} title={t("v2.rec.includeTool")}>
                  <input
                    type="checkbox"
                    checked={row.include}
                    onChange={(e) => setTool(row.key, { include: e.target.checked })}
                    aria-label={t("v2.rec.includeTool")}
                    data-testid="v2-rec-tool-include"
                  />
                </label>
                <input
                  className="v2-input mono"
                  style={{ flex: "0 0 32%" }}
                  value={row.name}
                  maxLength={256}
                  placeholder={t("v2.experiments.toolName")}
                  disabled={!row.include}
                  onChange={(e) => setTool(row.key, { name: e.target.value })}
                  data-testid="v2-rec-tool-name"
                />
                <textarea
                  className="v2-textarea"
                  style={{ flex: 1, minHeight: 38 }}
                  rows={2}
                  value={row.description}
                  maxLength={20000}
                  placeholder={t("v2.experiments.toolDesc")}
                  disabled={!row.include}
                  onChange={(e) => setTool(row.key, { description: e.target.value })}
                  data-testid="v2-rec-tool-desc"
                />
                <Button size="sm" onClick={() => setTools((rows) => rows.filter((r) => r.key !== row.key))}>
                  {t("v2.rec.removeTool")}
                </Button>
              </div>
            ))}
            <div>
              <Button size="sm" onClick={addTool} testId="v2-rec-tool-add">
                {t("v2.rec.addTool")}
              </Button>
            </div>
          </div>
        </Field>
      )}
      <div className="v2-row">
        <Button kind="primary" disabled={!mayRun || busy || (touched && invalid)} onClick={() => void submit()} testId="v2-rec-submit">
          {busy ? t("v2.rec.starting") : t("expPage.generateRec")}
        </Button>
        {recs.length > 0 && <Button onClick={() => setOpen(false)}>{t("v2.common.cancel")}</Button>}
        <span className="v2-muted">{t("v2.rec.traceNote", { id: run.batch_eval_id ?? "—", count: run.session_ids.length })}</span>
      </div>
    </div>
  );

  return (
    <Card
      title={t("v2.rec.title")}
      sub={t("v2.rec.sub")}
      end={
        seed.eligible && !formOpen ? (
          <Button size="sm" disabled={!mayRun} onClick={() => setOpen(true)} testId="v2-rec-new">
            {t("v2.rec.new")}
          </Button>
        ) : undefined
      }
      testId="v2-rec-card"
    >
      <div className="v2-form">
        {!seed.eligible ? <Alert>{t(`v2.rec.reason.${seed.reason_code ?? "run_not_completed"}`)}</Alert> : formOpen && form}
        {list.error && <Alert tone="error">{list.error}</Alert>}
        {recs.map((rec) => (
          <RecommendationResult key={rec.id} rec={rec} />
        ))}
      </div>
    </Card>
  );
}

function RecommendationResult({ rec }: { rec: RunRecommendation }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const done = rec.status === "COMPLETED";
  const copy = (text: string) =>
    void navigator.clipboard.writeText(text).then(
      () => toast("success", t("v2.rec.copied")),
      () => toast("error", t("v2.rec.copyFailed")),
    );
  const toolRows = Object.entries(rec.result.tools ?? {});

  return (
    <Card
      title={
        <span className="v2-row">
          {rec.kind === "system_prompt" ? t("expPage.recTypePrompt") : t("expPage.recTypeTools")}
          <Tag tone={STATUS_TONE[rec.status] ?? "gray"}>{rec.status}</Tag>
          <Tag tone="outline">{t(`v2.rec.source.${rec.input_source}`)}</Tag>
        </span>
      }
      sub={[fmtTime(rec.created_at), rec.evaluator].filter(Boolean).join(" · ")}
      end={
        done && rec.kind === "system_prompt" && rec.result.recommended_prompt ? (
          <Button size="sm" onClick={() => copy(rec.result.recommended_prompt ?? "")}>
            {t("v2.rec.copy")}
          </Button>
        ) : undefined
      }
      testId={`v2-rec-${rec.kind}`}
    >
      <div className="v2-form">
        {ACTIVE.has(rec.status) && <span className="v2-muted">{t("v2.rec.running", { id: rec.recommendation_id })}</span>}
        {rec.skipped_tools.length > 0 && <Alert tone="warn">{t("v2.rec.skippedTools", { tools: rec.skipped_tools.join(", ") })}</Alert>}
        {rec.error && <Alert tone={rec.status === "FAILED" ? "error" : "warn"}>{rec.error}</Alert>}
        {done && rec.kind === "system_prompt" && (
          <Field label={t("v2.experiments.promptDiff")} hint={rec.result.explanation} full>
            <DiffPanes
              before={rec.system_prompt ?? ""}
              after={rec.result.recommended_prompt ?? ""}
              beforeLabel={t("expPage.currentLabel")}
              afterLabel={t("expPage.recommendedLabel")}
            />
          </Field>
        )}
        {done && rec.kind === "tool_descriptions" && (
          <>
            <Table
              columns={[
                { key: "n", title: t("v2.experiments.toolName"), render: (r: [string, { description: string; explanation: string }]) => <span className="mono">{r[0]}</span> },
                { key: "c", title: t("expPage.currentLabel"), render: (r) => rec.tools[r[0]] || "—" },
                { key: "r", title: t("expPage.recommendedLabel"), render: (r) => r[1].description },
              ]}
              rows={toolRows}
              rowKey={(r) => r[0]}
              density="dense"
            />
            {/* the job's per-tool reasoning runs to paragraphs — kept out of the table */}
            {toolRows
              .filter(([, v]) => v.explanation)
              .map(([name, v]) => (
                <details key={name}>
                  <summary className="v2-muted">
                    {t("v2.rec.explanation")} · <span className="mono">{name}</span>
                  </summary>
                  <p className="v2-muted" style={{ whiteSpace: "pre-wrap", margin: "8px 0 0" }}>
                    {v.explanation}
                  </p>
                </details>
              ))}
          </>
        )}
        <span className="v2-muted mono">{rec.recommendation_id}</span>
      </div>
    </Card>
  );
}
