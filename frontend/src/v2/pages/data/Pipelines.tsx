import { ArrowDown, Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type V2Pipeline, type V2PipelineBody, type V2Range } from "../../../lib/api";
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
  Spin,
  Steps,
  Table,
  Tag,
  type TagTone,
} from "../../ui";

const STATUS_TONE: Record<V2Pipeline["status"], TagTone> = {
  idle: "gray",
  running: "blue",
  succeeded: "green",
  failed: "red",
};

function useDatasetNames() {
  const datasets = useLoad(() => api.v2Datasets(), "datasets");
  return useMemo(() => new Map((datasets.data?.datasets ?? []).map((d) => [d.id, d])), [datasets.data]);
}

// ─── list ──────────────────────────────────────────────────────────────────
export function PipelinesTab() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { data, loading, error, reload } = useLoad(() => api.v2Pipelines(), "pipelines");
  const datasets = useDatasetNames();
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

  const run = async (p: V2Pipeline) => {
    setRunning(p.id);
    try {
      const out = await api.v2RunPipeline(p.id);
      if (out.status === "failed") toast("error", out.last_run?.error ?? t("v2.pipelines.failed"));
      else toast("success", t("v2.pipelines.ran", { count: out.last_run?.added ?? 0 }));
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

  const outputName = (p: V2Pipeline) => {
    const id = p.config.output.dataset_id;
    if (id) return datasets.get(id)?.name ?? id;
    return t("v2.pipelines.newDataset", { name: p.config.output.dataset_name ?? "" });
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
      render: (p) => (
        <>
          {t("v2.pipelines.inputTraces")}
          <span className="sub">
            {[p.config.source.agent ?? t("v2.pipelines.allAgents"), rangeLabel(t, p.config.source.range), t(`v2.pipelines.status.${p.config.source.status}`)].join(" · ")}
          </span>
        </>
      ),
    },
    { key: "output", title: t("v2.pipelines.colOutput"), render: outputName },
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
          <LinkButton disabled={running !== null} onClick={() => void run(p)} testId={`v2-pipeline-run-${p.id}`}>
            {running === p.id ? t("v2.pipelines.running") : t("v2.pipelines.run")}
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
  source: { agent: null, range: "24h", status: "all", max_sessions: 20 },
  processing: { first_turn_only: false, dedupe: true, min_input_chars: 0 },
  output: { dataset_name: "" },
};

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
  const sessions = useLoad(() => api.obsSessions(range), `sessions:${range}`);

  if (id && draft === null && existing.data) {
    const p = existing.data;
    setDraft({ name: p.name, description: p.description, source: p.config.source, processing: p.config.processing, output: p.config.output });
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
    const body: V2PipelineBody = {
      ...draft,
      name: draft.name.trim(),
      output: draft.output.dataset_id ? { dataset_id: draft.output.dataset_id } : { dataset_name: draft.output.dataset_name?.trim() },
    };
    setSaving(true);
    setError(null);
    try {
      const saved = id ? await api.v2UpdatePipeline(id, body) : await api.v2CreatePipeline(body);
      if (runAfter) {
        const ran = await api.v2RunPipeline(saved.id);
        if (ran.status === "failed") toast("error", ran.last_run?.error ?? t("v2.pipelines.failed"));
        else toast("success", t("v2.pipelines.ran", { count: ran.last_run?.added ?? 0 }));
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

  const stepsLabels = [t("v2.pipelines.step1"), t("v2.pipelines.step2"), t("v2.pipelines.step3")];
  const last = existing.data?.last_run;

  return (
    <>
      <FlowHeader
        title={id ? t("v2.pipelines.editTitle") : t("v2.pipelines.newTitle")}
        onBack={() => setParams({ tab: "pipelines" })}
        steps={<Steps steps={stepsLabels} current={step} onSelect={setStep} />}
        end={
          <>
            <Button disabled={step === 0} onClick={() => setStep(step - 1)}>
              {t("v2.common.prev")}
            </Button>
            {step < 2 ? (
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

      {step === 0 && (
        <>
          <Card title={t("v2.pipelines.sourceTitle")}>
            <div className="v2-options" style={{ marginBottom: 20 }}>
              <OptionCard title={t("v2.pipelines.srcTraces")} desc={t("v2.pipelines.srcTracesDesc")} on onClick={() => undefined} />
              <OptionCard title={t("v2.pipelines.srcLogs")} desc={t("v2.pipelines.srcLogsDesc")} on={false} disabled onClick={() => undefined} badge={<Tag tone="gray">{t("v2.common.soon")}</Tag>} />
            </div>
            <div className="v2-form cols-2">
              <Field label="Agent" hint={t("v2.pipelines.agentHint")}>
                <select className="v2-select" value={draft.source.agent ?? ""} onChange={(e) => setSource({ agent: e.target.value || null })}>
                  <option value="">{t("v2.pipelines.allAgents")}</option>
                  {[...new Set([...(draft.source.agent ? [draft.source.agent] : []), ...agents])].map((a) => (
                    <option key={a} value={a}>
                      {a}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t("v2.common.timeRange")}>
                <select className="v2-select" value={draft.source.range} onChange={(e) => setSource({ range: e.target.value as V2Range })}>
                  {RANGES.map((r) => (
                    <option key={r} value={r}>
                      {rangeLabel(t, r)}
                    </option>
                  ))}
                </select>
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
          </Card>
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
        </>
      )}

      {step === 1 && (
        <div className="v2-grid-2" style={{ gridTemplateColumns: "minmax(0, 360px) minmax(0, 1fr)" }}>
          <Card title={t("v2.pipelines.flow")}>
            {[t("v2.pipelines.node1"), t("v2.pipelines.node2"), t("v2.pipelines.node3"), t("v2.pipelines.node4"), t("v2.pipelines.node5")].map((label, i, all) => (
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
              <Alert tone="warn">{t("v2.pipelines.logicNote")}</Alert>
            </div>
          </Card>
        </div>
      )}

      {step === 2 && (
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
                <select className="v2-select" value={draft.output.dataset_id ?? ""} onChange={(e) => setDraft({ ...draft, output: { dataset_id: e.target.value } })}>
                  <option value="">{t("v2.common.choose")}</option>
                  {receivable.map((d) => (
                    <option key={d.id} value={d.id}>
                      {d.name} · {t("v2.datasets.items", { count: d.item_count })}
                    </option>
                  ))}
                </select>
              </Field>
            )}
          </div>
        </Card>
      )}
    </>
  );
}
