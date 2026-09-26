import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type EvaluatorRow, type V2Range } from "../../../lib/api";
import { CLOUD_VALUE_PREFIX } from "../../../lib/evaluation";
import { evaluatorLabel, type EvaluatorLevel } from "../../../lib/evaluators";
import { fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  Field,
  FilterSelect,
  FlowHeader,
  OptionCard,
  SearchInput,
  Spin,
  Steps,
  Table,
  Tag,
} from "../../ui";

type Source = "window" | "sessions" | "logs" | "dataset";
type Strategy = "history" | "continuous";

const LOOKBACKS = [1, 6, 24, 72, 168, 336];
const SESSION_RANGE: V2Range = "7d";
const MAX_EVALUATORS = 10;

interface Draft {
  name: string;
  description: string;
  agentId: string;
  source: Source;
  strategy: Strategy;
  lookbackHours: number;
  sessionIds: string[];
  /** local dataset id, or `cloud:<datasetId>` */
  dataset: string;
  sampling: number;
  sessionTimeout: number;
  evaluators: string[];
}

const EMPTY: Draft = {
  name: "",
  description: "",
  agentId: "",
  source: "window",
  strategy: "history",
  lookbackHours: 24,
  sessionIds: [],
  dataset: "",
  sampling: 10,
  sessionTimeout: 15,
  evaluators: ["Builtin.Correctness", "Builtin.Helpfulness"],
};

/**
 * Prefill from `?from=run:<id>` / `?from=online:<id>` (copy), or from hand-off
 * params (`?agent=<id>&dataset=<id>&evaluators=<id,id,…>`, e.g. the architect
 * assistant's next steps).
 */
async function seedDraft(from: string | null, handoff: Handoff, copySuffix: string): Promise<Draft> {
  if (from?.startsWith("run:")) {
    const run = await api.getEvaluationRun(from.slice(4));
    const name = run.dataset_name ?? "";
    const base = { ...EMPTY, name: `${run.name || run.agent_name}${copySuffix}`.slice(0, 64), description: run.description ?? "", agentId: run.agent_id, evaluators: run.evaluators };
    if (name.startsWith("window:")) return { ...base, source: "window", lookbackHours: parseInt(name.slice(7), 10) || 24 };
    if (name.startsWith("cloud:") && run.dataset_id) return { ...base, source: "dataset", dataset: `${CLOUD_VALUE_PREFIX}${run.dataset_id}` };
    if (run.dataset_id) return { ...base, source: "dataset", dataset: run.dataset_id };
    return { ...base, source: "sessions", sessionIds: run.session_ids };
  }
  if (from?.startsWith("online:")) {
    const cfg = await api.v2OnlineConfig(from.slice(7));
    return {
      ...EMPTY,
      name: `${cfg.description || cfg.name || cfg.config_id}${copySuffix}`.slice(0, 64),
      agentId: cfg.agent_id ?? "",
      strategy: "continuous",
      sampling: cfg.sampling_percentage ?? 10,
      sessionTimeout: cfg.session_timeout_minutes ?? 15,
      evaluators: cfg.evaluators,
    };
  }
  const seeded: Draft = { ...EMPTY, agentId: handoff.agent ?? "" };
  if (handoff.evaluators.length) seeded.evaluators = handoff.evaluators;
  if (handoff.dataset) return { ...seeded, source: "dataset", dataset: handoff.dataset };
  return seeded;
}

interface Handoff {
  agent: string | null;
  dataset: string | null;
  evaluators: string[];
}

export function TaskWizard() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const toast = useV2Toast();
  const from = params.get("from");
  const handoff: Handoff = {
    agent: params.get("agent"),
    dataset: params.get("dataset"),
    evaluators: (params.get("evaluators") ?? "").split(",").map((s) => s.trim()).filter(Boolean),
  };
  const seed = useLoad(
    () => seedDraft(from, handoff, t("v2.tasks.copySuffix")),
    `seed:${from}:${handoff.agent}:${handoff.dataset}:${handoff.evaluators.join(",")}`,
  );
  const agents = useLoad(() => api.listAgents(), "agents");
  const datasets = useLoad(() => api.v2Datasets(), "datasets");
  const evaluators = useLoad(() => api.v2Evaluators(), "evaluators");
  const [draft, setDraft] = useState<Draft | null>(null);
  const [step, setStep] = useState(0);
  const [error, setError] = useState<string | null>(null);
  const [submitting, setSubmitting] = useState(false);
  const [evalLevel, setEvalLevel] = useState("");
  const [evalQ, setEvalQ] = useState("");

  useEffect(() => {
    if (seed.data && draft === null) setDraft(seed.data);
  }, [seed.data, draft]);

  const activeAgents = useMemo(() => (agents.data?.agents ?? []).filter((a) => a.status === "active"), [agents.data]);
  const agent = activeAgents.find((a) => a.id === draft?.agentId) ?? null;
  const sessions = useLoad(
    () => (draft && (draft.source === "sessions" || draft.source === "window") && agent ? api.obsSessions(SESSION_RANGE) : Promise.resolve(null)),
    `wizard-sessions:${draft?.source}:${agent?.id ?? ""}`,
  );
  const agentSessions = useMemo(
    () => (sessions.data?.sessions ?? []).filter((s) => agent && s.agent === agent.name),
    [sessions.data, agent],
  );

  if (seed.error) return <Alert tone="error">{seed.error}</Alert>;
  if (!draft) return <Spin />;
  const set = (patch: Partial<Draft>) => setDraft({ ...draft, ...patch });

  const localDatasets = (datasets.data?.datasets ?? []).filter((d) => d.kind !== "simulated");
  const selectedDataset = localDatasets.find((d) => d.id === draft.dataset) ?? null;
  const windowSessions = agentSessions.filter(
    (s) => !s.last || Date.now() - new Date(s.last).getTime() <= draft.lookbackHours * 3_600_000,
  );

  const allEvaluators = evaluators.data?.evaluators ?? [];
  const evalRows = allEvaluators.filter((e) => {
    if (evalLevel && e.level !== evalLevel) return false;
    const needle = evalQ.trim().toLowerCase();
    return !needle || `${e.id} ${e.name ?? ""} ${evaluatorLabel(t, e.id)}`.toLowerCase().includes(needle);
  });

  const validateStep0 = (): string | null => {
    if (!draft.name.trim()) return t("v2.tasks.errName");
    if (!draft.agentId) return t("v2.tasks.errAgent");
    if (draft.source === "sessions" && draft.sessionIds.length === 0) return t("v2.tasks.errSessions");
    if (draft.source === "dataset" && !draft.dataset) return t("v2.tasks.errDataset");
    return null;
  };

  const next = () => {
    const problem = validateStep0();
    setError(problem);
    if (!problem) setStep(1);
  };

  const submit = async () => {
    if (draft.evaluators.length === 0) return setError(t("v2.tasks.errEvaluators"));
    if (draft.evaluators.length > MAX_EVALUATORS) return setError(t("v2.tasks.errTooMany", { max: MAX_EVALUATORS }));
    setSubmitting(true);
    setError(null);
    try {
      if (draft.strategy === "continuous") {
        const cfg = await api.v2CreateOnlineConfig({
          agent_id: draft.agentId,
          mode: "scores",
          evaluators: draft.evaluators,
          sampling_percentage: draft.sampling,
          session_timeout_minutes: draft.sessionTimeout,
          filters: [],
          description: draft.name.trim().slice(0, 200),
          enable_on_create: true,
        });
        toast("success", t("v2.tasks.createdOnline"));
        setParams({ view: "detail", kind: "online", id: cfg.config_id });
      } else {
        const scope =
          draft.source === "window"
            ? { lookback_hours: draft.lookbackHours }
            : draft.source === "sessions"
              ? { session_ids: draft.sessionIds }
              : draft.dataset.startsWith(CLOUD_VALUE_PREFIX)
                ? { cloud_dataset_id: draft.dataset.slice(CLOUD_VALUE_PREFIX.length) }
                : { dataset_id: draft.dataset };
        const run = await api.v2CreateRun({
          agent_id: draft.agentId,
          name: draft.name.trim(),
          description: draft.description || undefined,
          evaluators: draft.evaluators,
          ...scope,
        });
        toast("success", t("v2.tasks.createdRun"));
        setParams({ view: "detail", kind: "run", id: run.id });
      }
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  const toggleEvaluator = (id: string) =>
    set({ evaluators: draft.evaluators.includes(id) ? draft.evaluators.filter((e) => e !== id) : [...draft.evaluators, id] });

  const sourceCards: { key: Source; disabled?: boolean }[] = [
    { key: "window" },
    { key: "sessions" },
    { key: "logs", disabled: true },
    { key: "dataset" },
  ];
  const chooseSource = (source: Source) => set({ source, strategy: source === "window" ? draft.strategy : "history" });

  return (
    <>
      <FlowHeader
        title={t("v2.tasks.newTitle")}
        onBack={() => setParams({})}
        steps={<Steps steps={[t("v2.tasks.stepData"), t("v2.tasks.stepEvaluators")]} current={step} onSelect={setStep} />}
        end={
          <>
            <Button disabled={step === 0} onClick={() => setStep(0)}>
              {t("v2.common.prev")}
            </Button>
            {step === 0 ? (
              <Button kind="primary" onClick={next} testId="v2-task-next">
                {t("v2.common.next")}
              </Button>
            ) : (
              <Button kind="primary" disabled={submitting} onClick={() => void submit()} testId="v2-task-submit">
                {draft.strategy === "continuous" ? t("v2.tasks.submitOnline") : t("v2.tasks.submitRun")}
              </Button>
            )}
          </>
        }
      />
      {error && <Alert tone="error">{error}</Alert>}

      {step === 0 && (
        <>
          <Card title={t("v2.tasks.basic")}>
            <div className="v2-form cols-2">
              <Field label={t("v2.tasks.colName")} required>
                <input className="v2-input" value={draft.name} maxLength={64} onChange={(e) => set({ name: e.target.value })} data-testid="v2-task-name" />
              </Field>
              <Field label={t("v2.tasks.colAgent")} required hint={agents.loading ? undefined : activeAgents.length === 0 ? t("v2.tasks.noAgents") : undefined}>
                <select className="v2-select" value={draft.agentId} onChange={(e) => set({ agentId: e.target.value, sessionIds: [] })} data-testid="v2-task-agent">
                  <option value="">{t("v2.common.choose")}</option>
                  {activeAgents.map((a) => (
                    <option key={a.id} value={a.id}>
                      {a.name}
                    </option>
                  ))}
                </select>
              </Field>
              <Field label={t("v2.tasks.description")} full>
                <textarea className="v2-textarea" rows={2} value={draft.description} maxLength={1000} placeholder={t("v2.tasks.descPlaceholder")} onChange={(e) => set({ description: e.target.value })} />
              </Field>
            </div>
          </Card>

          <Card title={t("v2.tasks.dataConfig")}>
            <div className="v2-options">
              {sourceCards.map(({ key, disabled }) => (
                <OptionCard
                  key={key}
                  title={t(`v2.tasks.src.${key}`)}
                  desc={t(`v2.tasks.src.${key}Desc`)}
                  on={draft.source === key}
                  disabled={disabled}
                  badge={disabled ? <Tag tone="gray">{t("v2.common.soon")}</Tag> : undefined}
                  onClick={() => chooseSource(key)}
                  testId={`v2-task-src-${key}`}
                />
              ))}
            </div>

            <div className="v2-grid-2" style={{ marginTop: 20 }}>
              <div>
                <h3 className="v2-sec-title" style={{ fontSize: 14 }}>
                  {t("v2.tasks.strategy")}
                </h3>
                <div className="v2-options" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
                  <OptionCard
                    title={t("v2.tasks.strategyHistory")}
                    desc={t(draft.source === "dataset" ? "v2.tasks.strategyReplayDesc" : "v2.tasks.strategyHistoryDesc")}
                    on={draft.strategy === "history"}
                    onClick={() => set({ strategy: "history" })}
                    testId="v2-task-strategy-history"
                  />
                  <OptionCard
                    title={t("v2.tasks.strategyContinuous")}
                    desc={t("v2.tasks.strategyContinuousDesc")}
                    on={draft.strategy === "continuous"}
                    disabled={draft.source !== "window"}
                    onClick={() => set({ strategy: "continuous" })}
                    testId="v2-task-strategy-continuous"
                  />
                </div>
              </div>
              <div>
                <h3 className="v2-sec-title" style={{ fontSize: 14 }}>
                  {draft.strategy === "continuous" ? t("v2.tasks.sampling") : t("v2.tasks.scope")}
                </h3>
                {draft.strategy === "continuous" ? (
                  <div className="v2-form cols-2">
                    <Field label={t("v2.tasks.samplingRate")} hint={t("v2.tasks.samplingHint")}>
                      <input className="v2-input" type="number" min={0.01} max={100} step={1} value={draft.sampling} onChange={(e) => set({ sampling: Math.max(0.01, Math.min(100, Number(e.target.value) || 10)) })} />
                    </Field>
                    <Field label={t("v2.tasks.sessionTimeout")} hint={t("v2.tasks.sessionTimeoutHint")}>
                      <input className="v2-input" type="number" min={1} max={1440} value={draft.sessionTimeout} onChange={(e) => set({ sessionTimeout: Math.max(1, Math.min(1440, Number(e.target.value) || 15)) })} />
                    </Field>
                  </div>
                ) : draft.source === "window" ? (
                  <Field label={t("v2.tasks.lookback")} hint={t("v2.tasks.lookbackHint")}>
                    <select className="v2-select" value={draft.lookbackHours} onChange={(e) => set({ lookbackHours: Number(e.target.value) })}>
                      {LOOKBACKS.map((h) => (
                        <option key={h} value={h}>
                          {t("v2.tasks.hours", { count: h })}
                        </option>
                      ))}
                    </select>
                  </Field>
                ) : draft.source === "dataset" ? (
                  <Field label={t("v2.tasks.dataset")} required hint={selectedDataset && !selectedDataset.has_ground_truth ? t("v2.tasks.noGroundTruth") : undefined}>
                    <select className="v2-select" value={draft.dataset} onChange={(e) => set({ dataset: e.target.value })} data-testid="v2-task-dataset">
                      <option value="">{t("v2.common.choose")}</option>
                      {draft.dataset && !selectedDataset && <option value={draft.dataset}>{draft.dataset}</option>}
                      {localDatasets.map((d) => (
                        <option key={d.id} value={d.id}>
                          {d.name} · {t("v2.datasets.items", { count: d.item_count })}
                        </option>
                      ))}
                    </select>
                  </Field>
                ) : (
                  <p className="v2-muted">{t("v2.tasks.sessionsPicked", { count: draft.sessionIds.length })}</p>
                )}
              </div>
            </div>
          </Card>

          <Card title={t("v2.tasks.preview")} sub={t("v2.tasks.previewSub")}>
            {!agent ? (
              <p className="v2-muted">{t("v2.tasks.previewPickAgent")}</p>
            ) : draft.source === "dataset" ? (
              selectedDataset ? (
                <Table
                  columns={[
                    { key: "n", title: "#", width: 48, render: (r: { i: number }) => r.i + 1 },
                    { key: "input", title: "Input", render: (r: { input: string }) => <span className="clip">{r.input}</span> },
                    { key: "expected", title: t("v2.datasets.expected"), render: (r: { expected: string }) => (r.expected ? <span className="clip">{r.expected}</span> : "—") },
                  ]}
                  rows={selectedDataset.items.slice(0, 5).map((item, i) => {
                    const turns = item.turns as { input?: unknown; expected_response?: unknown }[] | undefined;
                    return {
                      i,
                      input: String(turns?.[0]?.input ?? item.prompt ?? item.input ?? ""),
                      expected: String(turns?.[0]?.expected_response ?? item.expected ?? ""),
                    };
                  })}
                  rowKey={(r) => String(r.i)}
                />
              ) : (
                <p className="v2-muted">{t("v2.tasks.previewPickDataset")}</p>
              )
            ) : draft.strategy === "continuous" ? (
              <Alert>{t("v2.tasks.previewContinuous", { rate: draft.sampling })}</Alert>
            ) : (
              <>
                {draft.source === "window" && (
                  <Alert>{t("v2.tasks.previewWindow", { count: windowSessions.length, hours: draft.lookbackHours })}</Alert>
                )}
                <Table
                  columns={[
                    ...(draft.source === "sessions"
                      ? [
                          {
                            key: "sel",
                            title: "",
                            width: 36,
                            render: (s: (typeof agentSessions)[number]) => (
                              <input
                                type="checkbox"
                                aria-label={s.session_id}
                                checked={draft.sessionIds.includes(s.session_id)}
                                onChange={() =>
                                  set({
                                    sessionIds: draft.sessionIds.includes(s.session_id)
                                      ? draft.sessionIds.filter((id) => id !== s.session_id)
                                      : [...draft.sessionIds, s.session_id],
                                  })
                                }
                              />
                            ),
                          },
                        ]
                      : []),
                    { key: "sid", title: t("v2.traces.colSession"), render: (s: (typeof agentSessions)[number]) => <span className="mono">{s.session_id}</span> },
                    { key: "traces", title: t("v2.pipelines.traceCount"), className: "num", render: (s: (typeof agentSessions)[number]) => s.traces },
                    {
                      key: "status",
                      title: t("v2.traces.colStatus"),
                      render: (s: (typeof agentSessions)[number]) => (s.errors ? <Tag tone="red">{t("v2.traces.error", { count: s.errors })}</Tag> : <Tag tone="green">{t("v2.traces.ok")}</Tag>),
                    },
                    { key: "last", title: t("v2.pipelines.lastActive"), className: "nowrap", render: (s: (typeof agentSessions)[number]) => fmtTime(s.last) },
                  ]}
                  rows={(draft.source === "window" ? windowSessions : agentSessions).slice(0, draft.source === "sessions" ? 50 : 8)}
                  rowKey={(s) => s.session_id}
                  loading={sessions.loading}
                  error={sessions.error}
                  empty={t("v2.tasks.previewNoSessions")}
                />
              </>
            )}
          </Card>
        </>
      )}

      {step === 1 && (
        <Card title={t("v2.tasks.pickEvaluators")} sub={t("v2.tasks.pickedCount", { count: draft.evaluators.length, max: MAX_EVALUATORS })}>
          <div className="v2-toolbar">
            <FilterSelect
              label={t("v2.evaluators.colLevel")}
              value={evalLevel}
              allLabel={t("v2.common.all")}
              onChange={setEvalLevel}
              options={(["SESSION", "TRACE", "TOOL_CALL"] as EvaluatorLevel[]).map((l) => ({ value: l, label: t(`v2.level.${l}`) }))}
            />
            <div className="end">
              <SearchInput value={evalQ} onChange={setEvalQ} placeholder={t("v2.evaluators.search")} />
            </div>
          </div>
          {draft.evaluators.length > 0 && (
            <div className="v2-tags" style={{ marginBottom: 12 }}>
              {draft.evaluators.map((id) => (
                <Tag key={id} tone="blue">
                  {evaluatorLabel(t, id)}
                </Tag>
              ))}
            </div>
          )}
          <Table
            columns={[
              {
                key: "sel",
                title: "",
                width: 36,
                render: (e: EvaluatorRow) => (
                  <input
                    type="checkbox"
                    aria-label={e.id}
                    checked={draft.evaluators.includes(e.id)}
                    disabled={!draft.evaluators.includes(e.id) && draft.evaluators.length >= MAX_EVALUATORS}
                    onChange={() => toggleEvaluator(e.id)}
                    data-testid={`v2-task-eval-${e.id}`}
                  />
                ),
              },
              {
                key: "name",
                title: t("v2.evaluators.colName"),
                render: (e: EvaluatorRow) => (
                  <>
                    {e.source === "custom" ? (e.name ?? e.id) : evaluatorLabel(t, e.id)}
                    <span className="sub mono">{e.id}</span>
                  </>
                ),
              },
              { key: "source", title: t("v2.evaluators.colSource"), render: (e: EvaluatorRow) => t(`v2.evaluators.source.${e.source}`) },
              { key: "level", title: t("v2.evaluators.colLevel"), render: (e: EvaluatorRow) => t(`v2.level.${e.level}`, { defaultValue: e.level }) },
              {
                key: "gt",
                title: t("v2.evaluators.colGroundTruth"),
                render: (e: EvaluatorRow) =>
                  e.requires_ground_truth ? (
                    <Tag tone={draft.source === "dataset" && selectedDataset?.has_ground_truth ? "green" : "orange"}>{t("v2.evaluators.needsGt")}</Tag>
                  ) : (
                    <span className="v2-muted">{t("v2.common.no")}</span>
                  ),
              },
            ]}
            rows={evalRows}
            rowKey={(e) => e.id}
            loading={evaluators.loading}
            error={evaluators.error}
            onRetry={evaluators.reload}
            selectedKey={null}
          />
          {draft.evaluators.some((id) => allEvaluators.find((e) => e.id === id)?.requires_ground_truth) &&
            !(draft.source === "dataset" && selectedDataset?.has_ground_truth) && (
              <div style={{ marginTop: 12 }}>
                <Alert tone="warn">{t("v2.tasks.gtWarning")}</Alert>
              </div>
            )}
        </Card>
      )}
    </>
  );
}
