import { ArrowDown, Plus } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type V2Pipeline, type V2PipelineBody, type V2Range } from "../../../lib/api";
import { cleanSource, defaultLogFormat, logSourceProblem, PROBLEM_KEY } from "../../../lib/pipelineLogs";
import { fmtTime, RANGES, rangeLabel } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Field,
  FilterSelect,
  FlowHeader,
  LinkButton,
  OptionCard,
  Pager,
  SearchInput,
  Segmented,
  Select,
  Spin,
  Steps,
  Table,
  Tag,
  type TagTone,
} from "../../ui";
import { LogFormatStep, LogSourceFields } from "./LogSource";

const STATUS_TONE: Record<V2Pipeline["status"], TagTone> = {
  idle: "gray",
  running: "blue",
  succeeded: "green",
  failed: "red",
};

function useDatasetNames() {
  const datasets = useLoad(() => api.v2Datasets(), "datasets");
  const names = useMemo(() => new Map((datasets.data?.datasets ?? []).map((d) => [d.id, d])), [datasets.data]);
  return { names, loaded: datasets.data !== null, reload: datasets.reload };
}

// ─── list ──────────────────────────────────────────────────────────────────
export function PipelinesTab() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { data, loading, error, reload } = useLoad(() => api.v2Pipelines(), "pipelines");
  const { names: datasets, loaded: datasetsLoaded, reload: reloadDatasets } = useDatasetNames();
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [running, setRunning] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<V2Pipeline | null>(null);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data?.pipelines ?? []).filter(
      (p) => (!status || p.status === status) && (!needle || `${p.name} ${p.description}`.toLowerCase().includes(needle)),
    );
  }, [data, status, q]);
  const paged = usePaged(rows, 12);

  // a run proceeds in the background: refresh until it settles, then say how it went
  const inFlight = (data?.pipelines ?? []).some((p) => p.status === "running");
  useEffect(() => {
    if (!inFlight) return;
    const timer = window.setInterval(reload, 4000);
    return () => window.clearInterval(timer);
  }, [inFlight, reload]);
  const seenRunning = useRef<Set<string> | null>(null);
  useEffect(() => {
    if (!data) return;
    const now = new Set(data.pipelines.filter((p) => p.status === "running").map((p) => p.id));
    const settled = data.pipelines.filter((p) => seenRunning.current?.has(p.id) && !now.has(p.id));
    if (settled.length) reloadDatasets(); // a first run creates its output dataset
    for (const p of settled) {
      if (p.status === "failed") toast("error", p.last_run?.error ?? t("v2.pipelines.failed"));
      else if (p.status === "succeeded") toast("success", t("v2.pipelines.ran", { count: p.last_run?.added ?? 0 }));
    }
    seenRunning.current = now;
  }, [data, toast, t, reloadDatasets]);

  const run = async (p: V2Pipeline) => {
    setRunning(p.id);
    try {
      await api.v2RunPipeline(p.id);
      toast("success", t("v2.pipelines.started"));
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setRunning(null);
    }
  };

  const remove = async () => {
    if (!deleting) return;
    try {
      await api.v2DeletePipeline(deleting.id);
      toast("success", t("v2.pipelines.deleted"));
      setDeleting(null);
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };

  // the output dataset, linked once it exists (a first run creates a new one)
  const outputCell = (p: V2Pipeline) => {
    const id = p.config.output.dataset_id;
    if (!id) return t("v2.pipelines.newDataset", { name: p.config.output.dataset_name ?? "" });
    const ds = datasets.get(id);
    if (ds) {
      return (
        <>
          <LinkButton onClick={() => setParams({ tab: "datasets", view: "dataset", id })} testId={`v2-pipeline-dataset-${p.id}`}>
            {ds.name}
          </LinkButton>
          <span className="sub">{t("v2.datasets.items", { count: ds.item_count })}</span>
        </>
      );
    }
    return (
      <>
        <span className="mono">{id}</span>
        {datasetsLoaded && <span className="sub">{t("v2.pipelines.datasetGone")}</span>}
      </>
    );
  };

  const columns: Column<V2Pipeline>[] = [
    {
      key: "name",
      title: t("v2.pipelines.colName"),
      render: (p) => (
        <>
          <LinkButton onClick={() => setParams({ tab: "pipelines", view: "pipeline", id: p.id })}>{p.name}</LinkButton>
          {p.description && <span className="sub clip">{p.description}</span>}
        </>
      ),
    },
    {
      key: "input",
      title: t("v2.pipelines.colInput"),
      render: (p) => {
        const src = p.config.source;
        if (src.type === "logs") {
          const groups = src.log_groups ?? [];
          return (
            <>
              {t("v2.pipelines.srcLogs")}
              <span className="sub clip" title={groups.join("\n")}>
                {[
                  groups.length > 1 ? `${groups[0]} +${groups.length - 1}` : (groups[0] ?? "—"),
                  rangeLabel(t, src.range),
                  t(`v2.pipelines.logs.preset.${src.format?.preset ?? "genai"}`),
                ].join(" · ")}
              </span>
            </>
          );
        }
        return (
          <>
            {t("v2.pipelines.inputTraces")}
            <span className="sub">
              {[src.agent ?? t("v2.pipelines.allAgents"), rangeLabel(t, src.range), t(`v2.pipelines.status.${src.status}`)].join(" · ")}
            </span>
          </>
        );
      },
    },
    { key: "output", title: t("v2.pipelines.colOutput"), render: outputCell },
    { key: "mode", title: t("v2.pipelines.colMode"), render: () => t("v2.pipelines.manual") },
    {
      key: "status",
      title: t("v2.pipelines.colStatus"),
      render: (p) => <Tag tone={STATUS_TONE[p.status]}>{t(`v2.pipelines.state.${p.status}`)}</Tag>,
    },
    {
      key: "last",
      title: t("v2.pipelines.colLastRun"),
      className: "nowrap",
      render: (p) =>
        p.last_run ? (
          <>
            {fmtTime(p.last_run.at)}
            <span className="sub">
              {p.last_run.error ? t("v2.pipelines.lastError") : t("v2.pipelines.lastAdded", { count: p.last_run.added })}
            </span>
          </>
        ) : (
          <span className="v2-muted">{t("v2.pipelines.never")}</span>
        ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (p) => (
        <div className="v2-actions">
          <LinkButton disabled={running !== null || p.status === "running"} onClick={() => void run(p)} testId={`v2-pipeline-run-${p.id}`}>
            {running === p.id || p.status === "running" ? t("v2.pipelines.running") : t("v2.pipelines.run")}
          </LinkButton>
          <LinkButton onClick={() => setParams({ tab: "pipelines", view: "pipeline", id: p.id })}>{t("v2.common.edit")}</LinkButton>
          <LinkButton danger onClick={() => setDeleting(p)}>
            {t("v2.common.delete")}
          </LinkButton>
        </div>
      ),
    },
  ];

  return (
    <Card>
      <div className="v2-toolbar">
        <Button onClick={reload}>{t("v2.common.refresh")}</Button>
        <Button kind="primary" onClick={() => setParams({ tab: "pipelines", view: "pipeline-new" })} testId="v2-pipeline-new">
          <Plus size={14} aria-hidden="true" />
          {t("v2.pipelines.new")}
        </Button>
        <FilterSelect
          label={t("v2.pipelines.colStatus")}
          value={status}
          allLabel={t("v2.common.all")}
          onChange={setStatus}
          options={(["idle", "running", "succeeded", "failed"] as const).map((s) => ({ value: s, label: t(`v2.pipelines.state.${s}`) }))}
        />
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.pipelines.search")} />
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
        </div>
      </div>
      <Alert>{t("v2.pipelines.intro")}</Alert>
      <Table columns={columns} rows={paged.slice} rowKey={(p) => p.id} loading={loading} error={error} onRetry={reload} empty={t("v2.pipelines.empty")} testId="v2-pipelines-table" />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      <Confirm
        open={deleting !== null}
        title={t("v2.pipelines.deleteTitle")}
        body={t("v2.pipelines.deleteBody", { name: deleting?.name })}
        confirmLabel={t("v2.common.delete")}
        danger
        onConfirm={() => void remove()}
        onClose={() => setDeleting(null)}
      />
    </Card>
  );
}

// ─── create / edit ─────────────────────────────────────────────────────────
const EMPTY: V2PipelineBody = {
  name: "",
  description: "",
  source: { type: "traces", agent: null, range: "24h", status: "all", max_sessions: 20, log_groups: [], keyword: "", format: defaultLogFormat() },
  processing: { first_turn_only: false, dedupe: true, min_input_chars: 0, keep_replies: true },
  output: { dataset_name: "" },
};

type StepKey = "source" | "format" | "logic" | "output";

export function PipelineEditor({ id }: { id: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const existing = useLoad<V2Pipeline | null>(() => (id ? api.v2Pipeline(id) : Promise.resolve(null)), `pipeline:${id ?? "new"}`);
  const datasets = useLoad(() => api.v2Datasets(), "datasets");
  const [draft, setDraft] = useState<V2PipelineBody | null>(id ? null : EMPTY);
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const range: V2Range = draft?.source.range ?? "24h";
  const isLogs = draft?.source.type === "logs";
  // the trajectory preview only — a logs source previews in its format step
  const sessions = useLoad(() => (isLogs ? Promise.resolve(null) : api.obsSessions(range)), `sessions:${isLogs ? "logs" : range}`);

  if (id && draft === null && existing.data) {
    const p = existing.data;
    // rows saved before log sources existed carry no type / log fields
    setDraft({
      name: p.name,
      description: p.description,
      source: {
        ...EMPTY.source,
        ...p.config.source,
        type: p.config.source.type ?? "traces",
        format: { ...defaultLogFormat(), ...(p.config.source.format ?? {}) },
      },
      processing: { ...EMPTY.processing, ...p.config.processing },
      output: p.config.output,
    });
  }

  const matching = useMemo(() => {
    if (!draft) return [];
    return (sessions.data?.sessions ?? []).filter((s) => {
      if (draft.source.agent && s.agent !== draft.source.agent) return false;
      if (draft.source.status === "error" && s.errors === 0) return false;
      if (draft.source.status === "ok" && s.errors > 0) return false;
      return true;
    });
  }, [sessions.data, draft]);
  const agents = useMemo(() => [...new Set((sessions.data?.sessions ?? []).map((s) => s.agent).filter(Boolean))].sort(), [sessions.data]);

  if (id && (existing.loading || draft === null)) return existing.error ? <Alert tone="error">{existing.error}</Alert> : <Spin />;
  if (!draft) return <Spin />;

  const receivable = (datasets.data?.datasets ?? []).filter((d) => d.kind !== "simulated");
  const outMode: "new" | "existing" = draft.output.dataset_id ? "existing" : "new";
  const setSource = (patch: Partial<V2PipelineBody["source"]>) => setDraft({ ...draft, source: { ...draft.source, ...patch } });
  const setProc = (patch: Partial<V2PipelineBody["processing"]>) => setDraft({ ...draft, processing: { ...draft.processing, ...patch } });

  const save = async (runAfter: boolean) => {
    if (!draft.name.trim()) return setError(t("v2.pipelines.errName"));
    if (!draft.output.dataset_id && !draft.output.dataset_name?.trim()) return setError(t("v2.pipelines.errOutput"));
    const problem = isLogs ? logSourceProblem(draft.source) : null;
    if (problem) return setError(t(PROBLEM_KEY[problem]));
    const body: V2PipelineBody = {
      ...draft,
      source: cleanSource(draft.source),
      name: draft.name.trim(),
      output: draft.output.dataset_id ? { dataset_id: draft.output.dataset_id } : { dataset_name: draft.output.dataset_name?.trim() },
    };
    setSaving(true);
    setError(null);
    try {
      const saved = id ? await api.v2UpdatePipeline(id, body) : await api.v2CreatePipeline(body);
      if (runAfter) {
        // the run proceeds in the background; the list follows it to the end
        await api.v2RunPipeline(saved.id);
        toast("success", t("v2.pipelines.started"));
      } else {
        toast("success", t("v2.pipelines.saved"));
      }
      setParams({ tab: "pipelines" });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSaving(false);
    }
  };

  const stepKeys: StepKey[] = isLogs ? ["source", "format", "logic", "output"] : ["source", "logic", "output"];
  const stepLabel: Record<StepKey, string> = {
    source: t("v2.pipelines.step1"),
    format: t("v2.pipelines.logs.stepFormat"),
    logic: t("v2.pipelines.step2"),
    output: t("v2.pipelines.step3"),
  };
  const current = stepKeys[Math.min(step, stepKeys.length - 1)];
  const lastStep = stepKeys.length - 1;
  const last = existing.data?.last_run;

  return (
    <>
      <FlowHeader
        title={id ? t("v2.pipelines.editTitle") : t("v2.pipelines.newTitle")}
        onBack={() => setParams({ tab: "pipelines" })}
        steps={<Steps steps={stepKeys.map((k) => stepLabel[k])} current={step} onSelect={setStep} />}
        end={
          <>
            <Button disabled={step === 0} onClick={() => setStep(step - 1)}>
              {t("v2.common.prev")}
            </Button>
            {step < lastStep ? (
              <Button kind="primary" onClick={() => setStep(step + 1)} testId="v2-pipeline-next">
                {t("v2.common.next")}
              </Button>
            ) : (
              <>
                <Button disabled={saving} onClick={() => void save(false)} testId="v2-pipeline-save">
                  {t("v2.common.save")}
                </Button>
                <Button kind="primary" disabled={saving} onClick={() => void save(true)} testId="v2-pipeline-save-run">
                  {t("v2.pipelines.saveRun")}
                </Button>
              </>
            )}
          </>
        }
      />
      {error && <Alert tone="error">{error}</Alert>}
      {last?.error && <Alert tone="error">{t("v2.pipelines.lastFailed", { error: last.error })}</Alert>}

      {last?.log && (last.log.truncated || Object.keys(last.log.failed).length > 0) && (
        <Alert tone="warn">
          {t("v2.pipelines.logs.lastRunStats", { events: last.log.events, parsed: last.log.parsed })}
          {last.log.truncated && ` ${t("v2.pipelines.logs.truncated")}`}
        </Alert>
      )}

      {current === "source" && (
        <>
          <Card title={t("v2.pipelines.sourceTitle")}>
            <div className="v2-options" style={{ marginBottom: 20 }}>
              <OptionCard title={t("v2.pipelines.srcTraces")} desc={t("v2.pipelines.srcTracesDesc")} on={!isLogs} onClick={() => setSource({ type: "traces" })} testId="v2-pipeline-src-traces" />
              <OptionCard title={t("v2.pipelines.srcLogs")} desc={t("v2.pipelines.srcLogsDesc")} on={isLogs} onClick={() => setSource({ type: "logs" })} testId="v2-pipeline-src-logs" />
            </div>
            {isLogs ? (
              <LogSourceFields source={draft.source} onChange={setSource} />
            ) : (
            <div className="v2-form cols-2">
              <Field label="Agent" hint={t("v2.pipelines.agentHint")}>
                <Select
                  value={draft.source.agent ?? ""}
                  placeholder={t("v2.pipelines.allAgents")}
                  options={[...new Set([...(draft.source.agent ? [draft.source.agent] : []), ...agents])].map((a) => ({ value: a, label: a }))}
                  onChange={(v) => setSource({ agent: v || null })}
                />
              </Field>
              <Field label={t("v2.common.timeRange")}>
                <Select
                  value={draft.source.range}
                  options={RANGES.map((r) => ({ value: r, label: rangeLabel(t, r) }))}
                  onChange={(v) => setSource({ range: v as V2Range })}
                />
              </Field>
              <Field label={t("v2.pipelines.sessionStatus")}>
                <Segmented
                  value={draft.source.status}
                  onChange={(v) => setSource({ status: v })}
                  options={(["all", "ok", "error"] as const).map((s) => ({ value: s, label: t(`v2.pipelines.status.${s}`) }))}
                />
              </Field>
              <Field label={t("v2.pipelines.maxSessions")} hint={t("v2.pipelines.maxSessionsHint")}>
                <input
                  className="v2-input"
                  type="number"
                  min={1}
                  max={50}
                  value={draft.source.max_sessions}
                  onChange={(e) => setSource({ max_sessions: Math.max(1, Math.min(50, Number(e.target.value) || 1)) })}
                />
              </Field>
            </div>
            )}
          </Card>
          {!isLogs && (
          <Card title={t("v2.pipelines.preview")} sub={sessions.loading ? undefined : t("v2.pipelines.previewSub", { count: matching.length, take: Math.min(matching.length, draft.source.max_sessions) })}>
            <Table
              columns={[
                { key: "sid", title: t("v2.traces.colSession"), render: (s: (typeof matching)[number]) => <span className="mono">{s.session_id}</span> },
                { key: "agent", title: "Agent", render: (s: (typeof matching)[number]) => s.agent },
                { key: "traces", title: t("v2.pipelines.traceCount"), className: "num", render: (s: (typeof matching)[number]) => s.traces },
                {
                  key: "errors",
                  title: t("v2.traces.colStatus"),
                  render: (s: (typeof matching)[number]) => (s.errors ? <Tag tone="red">{t("v2.traces.error", { count: s.errors })}</Tag> : <Tag tone="green">{t("v2.traces.ok")}</Tag>),
                },
                { key: "last", title: t("v2.pipelines.lastActive"), className: "nowrap", render: (s: (typeof matching)[number]) => fmtTime(s.last) },
              ]}
              rows={matching.slice(0, 8)}
              rowKey={(s) => s.session_id}
              loading={sessions.loading}
              error={sessions.error}
              empty={t("v2.pipelines.previewEmpty")}
            />
          </Card>
          )}
        </>
      )}

      {current === "format" && <LogFormatStep source={draft.source} onChange={setSource} />}

      {current === "logic" && (
        <div className="v2-grid-2" style={{ gridTemplateColumns: "minmax(0, 360px) minmax(0, 1fr)" }}>
          <Card title={t("v2.pipelines.flow")}>
            {[isLogs ? t("v2.pipelines.logs.node1") : t("v2.pipelines.node1"), t("v2.pipelines.node2"), t("v2.pipelines.node3"), t("v2.pipelines.node4"), t("v2.pipelines.node5")].map((label, i, all) => (
              <div key={label}>
                <div className="v2-option" style={{ cursor: "default" }}>
                  <span className="t">
                    <span className="v2-tag blue">{i + 1}</span>
                    {label}
                  </span>
                </div>
                {i < all.length - 1 && (
                  <div style={{ display: "grid", placeItems: "center", color: "var(--v2-ink-4)", padding: "4px 0" }}>
                    <ArrowDown size={14} aria-hidden="true" />
                  </div>
                )}
              </div>
            ))}
          </Card>
          <Card title={t("v2.pipelines.logic")}>
            <div className="v2-form">
              <Field label={t("v2.pipelines.extract")} hint={t("v2.pipelines.extractHint")}>
                <Segmented
                  value={draft.processing.first_turn_only ? "first" : "all"}
                  onChange={(v) => setProc({ first_turn_only: v === "first" })}
                  options={[
                    { value: "all", label: t("v2.pipelines.allTurns") },
                    { value: "first", label: t("v2.pipelines.firstTurn") },
                  ]}
                />
              </Field>
              <Field label={t("v2.pipelines.minChars")} hint={t("v2.pipelines.minCharsHint")}>
                <input
                  className="v2-input"
                  style={{ width: 160 }}
                  type="number"
                  min={0}
                  max={500}
                  value={draft.processing.min_input_chars}
                  onChange={(e) => setProc({ min_input_chars: Math.max(0, Math.min(500, Number(e.target.value) || 0)) })}
                />
              </Field>
              <label className="v2-check">
                <input type="checkbox" checked={draft.processing.dedupe} onChange={(e) => setProc({ dedupe: e.target.checked })} />
                {t("v2.pipelines.dedupe")}
              </label>
              <label className="v2-check">
                <input
                  type="checkbox"
                  checked={draft.processing.keep_replies !== false}
                  onChange={(e) => setProc({ keep_replies: e.target.checked })}
                  data-testid="v2-pipeline-keep-replies"
                />
                {t("v2.pipelines.keepReplies")}
              </label>
              {draft.processing.keep_replies !== false ? (
                <Alert tone="warn">{t("v2.pipelines.logicNote")}</Alert>
              ) : (
                <Alert>{t("v2.pipelines.inputsOnlyNote")}</Alert>
              )}
            </div>
          </Card>
        </div>
      )}

      {current === "output" && (
        <Card title={t("v2.pipelines.outputTitle")}>
          <div className="v2-form cols-2">
            <Field label={t("v2.pipelines.colName")} required>
              <input className="v2-input" value={draft.name} maxLength={64} onChange={(e) => setDraft({ ...draft, name: e.target.value })} data-testid="v2-pipeline-name" />
            </Field>
            <Field label={t("v2.pipelines.description")}>
              <input className="v2-input" value={draft.description} maxLength={1000} onChange={(e) => setDraft({ ...draft, description: e.target.value })} />
            </Field>
            <Field label={t("v2.pipelines.outputTarget")} full>
              <Segmented
                value={outMode}
                onChange={(v) => setDraft({ ...draft, output: v === "new" ? { dataset_name: "" } : { dataset_id: receivable[0]?.id ?? "" } })}
                options={[
                  { value: "new", label: t("v2.addToDataset.new") },
                  { value: "existing", label: t("v2.addToDataset.existing") },
                ]}
              />
            </Field>
            {outMode === "new" ? (
              <Field label={t("v2.pipelines.newDatasetName")} required hint={t("v2.pipelines.newDatasetHint")}>
                <input
                  className="v2-input"
                  value={draft.output.dataset_name ?? ""}
                  maxLength={64}
                  onChange={(e) => setDraft({ ...draft, output: { dataset_name: e.target.value } })}
                  data-testid="v2-pipeline-dataset-name"
                />
              </Field>
            ) : (
              <Field label={t("v2.addToDataset.dataset")} required>
                <Select
                  value={draft.output.dataset_id ?? ""}
                  placeholder={t("v2.common.choose")}
                  options={receivable.map((d) => ({ value: d.id, label: `${d.name} · ${t("v2.datasets.items", { count: d.item_count })}` }))}
                  onChange={(v) => setDraft({ ...draft, output: { dataset_id: v } })}
                />
              </Field>
            )}
          </div>
        </Card>
      )}
    </>
  );
}
