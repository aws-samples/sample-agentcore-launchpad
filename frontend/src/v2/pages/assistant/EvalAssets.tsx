import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  api,
  ApiError,
  type AssistantEvalOperation,
  type AssistantEvalPlan,
  type AssistantEvalPlanContent,
  type AssistantEvalPlanEvaluator,
  type AssistantEvalPlanRepair,
  type AssistantEvalPlanState,
  type AssistantEvalResource,
  type AssistantProposal,
} from "../../../lib/api";
import {
  asArray,
  asRecord,
  compactRule,
  type DeployedAgent,
  planBlockGoldenTest,
  planConfirmScenarios,
  planUnblockGoldenTest,
} from "../../../lib/assistant";
import { Alert, Button, Confirm, LinkButton, Table, Tag, type TagTone } from "../../ui";
import { NextStepsCard } from "./NextSteps";
import { SECTION_IDS } from "./common";

/**
 * SE-047 — the reviewed evaluation-assets plan of one assistant conversation.
 * Prepare (platform draft from a proposal revision) → review the mapping (golden
 * tests → scenarios or blocked; recommendations → evaluator kinds) → confirm / block
 * scenarios or edit as JSON (each a new plan revision) → an administrator CREATES the
 * assets. Nothing here runs an evaluation or deploys an agent. Asking the assistant to
 * repair an invalid plan invokes one discussion turn, then prepares the returned revision.
 */

const POLL_MS = 3000;
const POLL_BACKOFF_MAX_MS = 15000;

const OP_TONE: Record<string, TagTone> = {
  queued: "gray",
  running: "blue",
  succeeded: "green",
  partial: "orange",
  failed: "red",
  cleaning: "blue",
  cleaned: "gray",
};

const RES_TONE: Record<string, TagTone> = {
  pending: "gray",
  accepted: "blue",
  ready: "green",
  failed: "red",
  conflict: "red",
  blocked: "gray",
  skipped: "gray",
  retained: "orange",
  deleted: "gray",
  delete_failed: "red",
};

const PLAN_TONE: Record<string, TagTone> = {
  draft: "orange",
  invalid: "red",
  approved: "green",
  superseded: "gray",
};

const CLOUD_KINDS = new Set(["judge", "derived", "code"]);

type Scenario = NonNullable<AssistantEvalPlanContent["scenarios"]>[number];
type Blocked = NonNullable<AssistantEvalPlanContent["blocked_golden_tests"]>[number];
type Recommendation = NonNullable<AssistantEvalPlanContent["recommendations"]>[number];

export function EvalAssetsCard({
  conversationId, proposals, canMaterialize, workspaceId, apiMessage, onError, deployed, onRepair, repairDisabledReason,
}: {
  conversationId: string;
  proposals: AssistantProposal[];
  canMaterialize: boolean;
  workspaceId: string | null;
  apiMessage: (err: unknown) => string;
  onError: (message: string) => void;
  deployed: DeployedAgent | null;
  /** Resolves only after the shared turn finishes and its new proposal is read back. */
  onRepair: (repair: AssistantEvalPlanRepair) => Promise<AssistantProposal | null>;
  repairDisabledReason?: string;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<AssistantEvalPlanState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [repairStatus, setRepairStatus] = useState<"asking" | "preparing" | "ready" | "invalid" | null>(null);
  const [sourceRevision, setSourceRevision] = useState<number>(
    proposals.length ? proposals[proposals.length - 1].revision : 1,
  );
  const [jsonDraft, setJsonDraft] = useState<string | null>(null);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [confirmPlan, setConfirmPlan] = useState<AssistantEvalPlan | null>(null);
  const [confirmCleanup, setConfirmCleanup] = useState<AssistantEvalOperation | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [blocking, setBlocking] = useState<{ goldenTestId: string; reason: string } | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [pollFailures, setPollFailures] = useState(0);

  // staleness: a generation bumps on every conversation/workspace change and on unmount
  const generation = useRef(0);
  const alive = useRef(true);
  const ctx = useRef({ conversationId, workspaceId });
  ctx.current = { conversationId, workspaceId };
  const stillCurrent = useCallback((gen: number) => alive.current && generation.current === gen, []);

  const load = useCallback(() => {
    const gen = generation.current;
    const { conversationId: cid, workspaceId: wid } = ctx.current;
    setLoadError(null);
    void api
      .assistantEvalPlan(cid, wid)
      .then((res) => { if (stillCurrent(gen)) setState(res); })
      .catch((err) => { if (stillCurrent(gen)) setLoadError(apiMessage(err)); });
  }, [apiMessage, stillCurrent]);

  useEffect(() => {
    alive.current = true;
    generation.current += 1;
    setState(null);
    setJsonDraft(null);
    setJsonError(null);
    setActionError(null);
    setBusy(false);
    busyRef.current = false;
    setRepairStatus(null);
    setPollError(null);
    setPollFailures(0);
    setConfirmPlan(null);
    setConfirmCleanup(null);
    load();
    return () => {
      alive.current = false;
      generation.current += 1;
    };
  }, [conversationId, workspaceId, load]);

  const plans = asArray<AssistantEvalPlan>(state?.plans);
  const current = plans.length ? plans[plans.length - 1] : null;
  const operations = asArray<AssistantEvalOperation>(state?.operations);
  // the operation of THIS plan revision only — never a fallback to an older one
  const operation = current ? operations.find((o) => o.plan_id === current.id) ?? null : null;
  const history = operations.filter((o) => o.id !== operation?.id);
  const requiresNewPlan = operation?.requires_new_plan === true;
  const operationPending = operations.some((o) =>
    o.running || o.status === "queued" || o.status === "running" || o.status === "cleaning");
  const replacementReason = jsonDraft !== null
    ? t("assistantEval.replacementJsonOpen")
    : busy || operationPending || confirmPlan || confirmCleanup || repairDisabledReason
      ? t("assistantEval.replacementPending")
      : undefined;

  // poll a live operation, retrying after failures with backoff and a visible error
  useEffect(() => {
    if (!operation || !(operation.status === "queued" || operation.status === "running")) {
      setPollError(null);
      return;
    }
    const gen = generation.current;
    const { conversationId: cid, workspaceId: wid } = ctx.current;
    const opId = operation.id;
    const delay = Math.min(POLL_MS * 2 ** pollFailures, POLL_BACKOFF_MAX_MS);
    const timer = window.setTimeout(() => {
      void api
        .assistantEvalOperation(cid, opId, wid)
        .then((res) => {
          if (!stillCurrent(gen)) return;
          setPollError(null);
          setPollFailures(0);
          setState((prev) => prev
            ? { ...prev, operations: prev.operations.map((o) => (o.id === res.operation.id ? res.operation : o)) }
            : prev);
        })
        .catch((err) => {
          if (!stillCurrent(gen)) return;
          setPollError(apiMessage(err));
          setPollFailures((n) => n + 1);
        });
    }, delay);
    return () => window.clearTimeout(timer);
  }, [operation, pollFailures, apiMessage, stillCurrent]);

  const run = useCallback(
    async (
      fn: () => Promise<AssistantEvalPlanState | { operation: AssistantEvalOperation }>,
      after?: () => void,
    ) => {
      if (busyRef.current) return;
      const gen = generation.current;
      busyRef.current = true;
      setBusy(true);
      setActionError(null);
      setRepairStatus(null);
      try {
        const res = await fn();
        if (!stillCurrent(gen)) return;
        after?.();
        if ("plans" in res) setState(res);
        else {
          setState((prev) => prev
            ? {
              ...prev,
              operations: prev.operations.some((o) => o.id === res.operation.id)
                ? prev.operations.map((o) => (o.id === res.operation.id ? res.operation : o))
                : [...prev.operations, res.operation],
            }
            : prev);
          load(); // the plan's own status (draft → approved) changed too
        }
      } catch (err) {
        if (!stillCurrent(gen)) return;
        const message = apiMessage(err);
        setActionError(message);
        onError(message);
        if (err instanceof ApiError && err.code === "assistant.evaluation_assets_new_plan_required") load();
      } finally {
        if (stillCurrent(gen)) {
          busyRef.current = false;
          setBusy(false);
        }
      }
    },
    [apiMessage, onError, stillCurrent, load],
  );

  const edit = (content: Record<string, unknown>, after?: () => void) =>
    void run(() => api.assistantEvalPlanEdit(conversationId, content, workspaceId), after);

  const prepare = () =>
    run(() => api.assistantEvalPlanPrepare(conversationId, sourceRevision, workspaceId), () => setJsonDraft(null));

  const prepareReplacement = () => {
    if (!current || !requiresNewPlan || busyRef.current || replacementReason) return;
    // Copy the saved plan exactly: preparing from a proposal would discard member edits.
    edit(current.content);
  };

  const repairReason = jsonDraft !== null
    ? t("assistantEval.repairJsonOpen")
    : busy ? t("assistantEval.repairBusy") : repairDisabledReason;

  const repair = async () => {
    if (!current || current.status !== "invalid" || busyRef.current || repairReason) return;
    const gen = generation.current;
    const { conversationId: cid, workspaceId: wid } = ctx.current;
    busyRef.current = true;
    setBusy(true);
    setActionError(null);
    setRepairStatus("asking");
    try {
      const proposal = await onRepair({ plan_revision: current.revision, plan_hash: current.content_hash });
      if (!stillCurrent(gen)) return;
      if (!proposal) throw new Error(t("assistantEval.repairNoProposal"));
      // Use the exact returned revision, never a possibly stale dropdown selection.
      setSourceRevision(proposal.revision);
      setRepairStatus("preparing");
      const res = await api.assistantEvalPlanPrepare(cid, proposal.revision, wid);
      if (!stillCurrent(gen)) return;
      setState(res);
      setRepairStatus(res.plan.status === "invalid" || res.plan.validation_errors.length > 0 ? "invalid" : "ready");
    } catch (err) {
      if (!stillCurrent(gen)) return;
      setRepairStatus(null);
      const message = apiMessage(err);
      setActionError(message);
      onError(message);
      if (err instanceof ApiError && (err.code === "assistant.evaluation_repair_stale"
        || err.code === "assistant.evaluation_repair_not_needed")) load();
    } finally {
      if (stillCurrent(gen)) {
        busyRef.current = false;
        setBusy(false);
      }
    }
  };

  const saveJson = () => {
    if (jsonDraft === null) return;
    let parsed: Record<string, unknown>;
    try {
      parsed = JSON.parse(jsonDraft) as Record<string, unknown>;
    } catch (err) {
      setJsonError(err instanceof Error ? err.message : String(err));
      return;
    }
    setJsonError(null);
    edit(parsed, () => setJsonDraft(null));
  };

  const content = asRecord(current?.content) as Partial<AssistantEvalPlanContent>;
  const evaluators = asArray<AssistantEvalPlanEvaluator>(content.evaluators);
  const recommendations = asArray<Recommendation>(content.recommendations);
  const scenarios = asArray<Scenario>(content.scenarios);
  const blocked = asArray<Blocked>(content.blocked_golden_tests);
  const invalid = !!current && current.validation_errors.length > 0;
  const reviewPending = scenarios.filter((s) => s?.review_required).length;
  // A plan whose members have the expected shape is shown as tables even while it
  // does not validate yet; only a malformed plan falls back to the raw dump.
  const structured = !!current && Array.isArray(content.scenarios) && Array.isArray(content.evaluators);
  const summary = current?.summary;
  const canCreate = !!current && current.status === "draft" && !invalid && canMaterialize && !busy && !operation;
  const createReason = !canMaterialize
    ? t("assistantEval.adminOnly")
    : invalid
      ? reviewPending ? t("assistantEval.reviewPending", { n: reviewPending }) : t("assistantEval.fixErrors")
      : undefined;
  const opResources = asArray<AssistantEvalResource>(operation?.resources);
  const rowEditable = !operation && jsonDraft === null;
  const currentContent = asRecord(current?.content);

  // scenario rows and blocked golden tests share one table
  type Row = { kind: "scenario"; s: Scenario } | { kind: "blocked"; b: Blocked };
  const rows: Row[] = [
    ...scenarios.map((s): Row => ({ kind: "scenario", s })),
    ...blocked.map((b): Row => ({ kind: "blocked", b })),
  ];

  return (
    <>
      <section id={SECTION_IDS.evaluation} className="v2-card" data-testid="v2-assistant-eval" data-workspace={workspaceId ?? ""}>
        <div className="v2-card-body">
          <h2 className="v2-sec-title">
            {t("assistantEval.title")}
            <span className="sub">{t("assistantEval.sub")}</span>
            {current && (
              <span className="end">
                <Tag tone="outline">{t("assistantEval.planRevision", { n: current.revision })}</Tag>
                <Tag tone={PLAN_TONE[current.status] ?? "gray"} dot>{t(`assistantEval.planStatus.${current.status}`)}</Tag>
              </span>
            )}
          </h2>
          <Alert>{t("assistantEval.scopeNote")}</Alert>
          {loadError && (
            <Alert tone="error" action={<LinkButton onClick={load}>{t("assistantEval.refresh")}</LinkButton>}>
              {loadError}
            </Alert>
          )}

          <div className="v2-toolbar">
            <label className="v2-row" style={{ fontSize: 13, color: "var(--v2-ink-2)" }}>
              {t("assistantEval.sourceRevision")}
              <select
                className="v2-select"
                style={{ width: "auto" }}
                value={sourceRevision}
                disabled={busy}
                data-testid="v2-assistant-eval-source"
                onChange={(e) => setSourceRevision(Number(e.target.value))}
              >
                {proposals.filter((p) => p.status !== "invalid").map((p) => (
                  <option key={p.id} value={p.revision}>
                    r{p.revision} · {t(`assistantPage.status.${p.status}`)}
                  </option>
                ))}
              </select>
            </label>
            <Button kind={current ? undefined : "primary"} disabled={busy || !proposals.length}
              onClick={() => void prepare()} testId="v2-assistant-eval-prepare">
              {current ? t("assistantEval.prepareAgain") : t("assistantEval.prepare")}
            </Button>
            {current && jsonDraft === null && !operation && (
              <Button disabled={busy} onClick={() => setJsonDraft(JSON.stringify(current.content, null, 2))}
                testId="v2-assistant-eval-edit-json">
                {t("assistantEval.editJson")}
              </Button>
            )}
            {current && !operation && reviewPending > 0 && jsonDraft === null && (
              <Button disabled={busy} onClick={() => edit(planConfirmScenarios(currentContent))}
                testId="v2-assistant-eval-confirm-all">
                {t("assistantEval.confirmScenarios", { n: reviewPending })}
              </Button>
            )}
            {current && summary && (
              <div className="end">
                <span className="v2-muted mono" style={{ fontSize: 12 }} data-testid="v2-assistant-eval-summary">
                  {t("assistantEval.summaryLine", {
                    scenarios: summary.scenarios,
                    blocked: summary.blocked_golden_tests,
                    cloud: summary.cloud_evaluators,
                    lambdas: summary.lambda_functions,
                    roles: summary.iam_roles + summary.role_grants,
                    unresolved: summary.unresolved_recommendations,
                  })}
                  {" · "}
                  {current.content_hash.slice(0, 12)}
                </span>
              </div>
            )}
          </div>

          {!current && !loadError && (
            <div className="v2-muted" style={{ padding: "12px 0" }} data-testid="v2-assistant-eval-empty">
              {state ? t("assistantEval.empty") : t("common.loading")}
            </div>
          )}

          {invalid && (
            <Alert tone="error">
              {reviewPending ? t("assistantEval.reviewNote") : t("assistantEval.invalidNote")}
              <ul className="v2-list">
                {current!.validation_errors.map((e, i) => (
                  <li key={i} className="mono" style={{ fontSize: 12, wordBreak: "break-word" }}>{e}</li>
                ))}
              </ul>
              {current?.status === "invalid" && (
                <div style={{ marginTop: 10 }}>
                  <Button size="sm" disabled={!!repairReason} title={repairReason}
                    onClick={() => void repair()} testId="v2-assistant-eval-repair">
                    {t("assistantEval.repair")}
                  </Button>
                  <div style={{ fontSize: 12, marginTop: 6 }}>{t("assistantEval.repairHint")}</div>
                </div>
              )}
            </Alert>
          )}
          {repairStatus && (
            <Alert tone={repairStatus === "invalid" ? "warn" : repairStatus === "ready" ? "success" : "info"}>
              <span data-testid="v2-assistant-eval-repair-status">{t(`assistantEval.repairStatus.${repairStatus}`)}</span>
            </Alert>
          )}
          {actionError && <Alert tone="error">{actionError}</Alert>}

          {current && jsonDraft !== null && (
            <div className="v2-assistant-section" data-testid="v2-assistant-eval-json">
              <h3>{t("assistantEval.jsonTitle")}</h3>
              <textarea className="v2-textarea code" style={{ width: "100%", minHeight: 320 }}
                value={jsonDraft} onChange={(e) => setJsonDraft(e.target.value)} />
              {jsonError && <div style={{ marginTop: 8 }}><Alert tone="error"><span className="mono">{jsonError}</span></Alert></div>}
              <div className="v2-row" style={{ marginTop: 8 }}>
                <Button kind="primary" disabled={busy} onClick={saveJson}>{t("assistantEval.saveRevision")}</Button>
                <Button onClick={() => { setJsonDraft(null); setJsonError(null); }}>{t("assistantEval.cancel")}</Button>
              </div>
            </div>
          )}

          {current && !structured && jsonDraft === null && (
            <div className="v2-assistant-section">
              <h3>{t("assistantEval.rawTitle")}</h3>
              <pre className="v2-pre">{JSON.stringify(current.content, null, 2)}</pre>
            </div>
          )}

          {current && structured && jsonDraft === null && (
            <>
              {rows.length > 0 && (
                <div className="v2-assistant-section" data-testid="v2-assistant-eval-scenarios">
                  <h3>{t("assistantEval.scenarios")}</h3>
                  <Table
                    density="dense"
                    rows={rows}
                    rowKey={(r) => (r.kind === "scenario" ? `s-${r.s?.scenario_id}` : `b-${r.b?.golden_test_id}`)}
                    columns={[
                      {
                        key: "gt",
                        title: t("assistantEval.col.goldenTest"),
                        render: (r) => <span className="mono">{String(r.kind === "scenario" ? r.s?.golden_test_id ?? "" : r.b?.golden_test_id ?? "")}</span>,
                      },
                      {
                        key: "scenario",
                        title: t("assistantEval.col.scenario"),
                        render: (r) => r.kind === "blocked"
                          ? <span className="v2-muted">{String(r.b?.reason ?? "")}</span>
                          : <span className="mono">{String(r.s?.scenario_id ?? "")}</span>,
                      },
                      {
                        key: "turns",
                        title: t("assistantEval.col.turns"),
                        width: "28%",
                        render: (r) => r.kind === "blocked" ? "—" : <ScenarioTurns s={r.s} />,
                      },
                      {
                        key: "status",
                        title: t("assistantEval.col.status"),
                        render: (r) => r.kind === "blocked"
                          ? <Tag tone="orange">{t("assistantEval.blocked")}</Tag>
                          : (
                            <>
                              <Tag tone={r.s?.review_required ? "orange" : "green"}>
                                {r.s?.review_required ? t("assistantEval.reviewRequired") : t("assistantEval.confirmed")}
                              </Tag>
                              {r.s?.note && <span className="sub">{r.s.note}</span>}
                            </>
                          ),
                      },
                      {
                        key: "ops",
                        title: t("v2.common.actions"),
                        className: "right",
                        render: (r) => {
                          if (!rowEditable) return null;
                          if (r.kind === "blocked") {
                            const id = String(r.b?.golden_test_id);
                            return (
                              <div className="v2-actions">
                                <LinkButton disabled={busy} testId={`v2-assistant-eval-unblock-${id}`} onClick={() => {
                                  const next = planUnblockGoldenTest(currentContent, proposals, id);
                                  if (next) edit(next);
                                  else onError(t("assistantEval.unblockMissing", { id }));
                                }}>
                                  {t("assistantEval.unblock")}
                                </LinkButton>
                              </div>
                            );
                          }
                          const id = String(r.s?.golden_test_id);
                          if (blocking?.goldenTestId === id) {
                            return (
                              <div className="v2-assistant-inline-form">
                                <input className="v2-input" value={blocking.reason} autoFocus
                                  placeholder={t("assistantEval.blockReasonPlaceholder")}
                                  onChange={(e) => setBlocking({ ...blocking, reason: e.target.value })}
                                  data-testid="v2-assistant-eval-block-reason" />
                                <div className="v2-actions">
                                  <LinkButton disabled={busy} onClick={() => setBlocking(null)}>{t("assistantEval.cancel")}</LinkButton>
                                  <LinkButton danger disabled={busy || !blocking.reason.trim()}
                                    title={t("assistantEval.blockReasonRequired")}
                                    onClick={() => {
                                      const reason = blocking.reason.trim();
                                      setBlocking(null);
                                      edit(planBlockGoldenTest(currentContent, id, reason));
                                    }}>
                                    {t("assistantEval.blockConfirm")}
                                  </LinkButton>
                                </div>
                              </div>
                            );
                          }
                          return (
                            <div className="v2-actions">
                              {r.s?.review_required && (
                                <LinkButton disabled={busy} testId={`v2-assistant-eval-confirm-${id}`}
                                  onClick={() => edit(planConfirmScenarios(currentContent, id))}>
                                  {t("assistantEval.confirmOne")}
                                </LinkButton>
                              )}
                              <LinkButton danger disabled={busy} testId={`v2-assistant-eval-block-${id}`}
                                onClick={() => setBlocking({ goldenTestId: id, reason: "" })}>
                                {t("assistantEval.block")}
                              </LinkButton>
                            </div>
                          );
                        },
                      },
                    ]}
                  />
                </div>
              )}
              {evaluators.length > 0 && (
                <div className="v2-assistant-section" data-testid="v2-assistant-eval-evaluators">
                  <h3>{t("assistantEval.evaluators")}</h3>
                  <Table
                    density="dense"
                    rows={evaluators}
                    rowKey={(e) => String(e?.key)}
                    columns={[
                      {
                        key: "key",
                        title: t("assistantEval.col.key"),
                        render: (e) => (
                          <>
                            <span className="mono">{String(e?.key ?? "")}</span>
                            <span className="sub">{String(e?.title ?? "")}</span>
                          </>
                        ),
                      },
                      {
                        key: "kind",
                        title: t("assistantEval.col.kind"),
                        render: (e) => {
                          const kind = String(e?.kind ?? "");
                          return (
                            <span className="v2-tags">
                              <Tag tone={CLOUD_KINDS.has(kind) ? "blue" : "gray"}>{t(`assistantEval.kind.${kind}`, kind)}</Tag>
                              {e?.draft && <Tag tone="orange">{t("assistantEval.draftRubric")}</Tag>}
                            </span>
                          );
                        },
                      },
                      { key: "def", title: t("assistantEval.col.definition"), render: (e) => <EvaluatorDefinition e={e} /> },
                      {
                        key: "gts",
                        title: t("assistantEval.col.goldenTests"),
                        render: (e) => {
                          const gts = asArray<string>(e?.golden_test_ids);
                          return <span className="mono">{gts.length ? gts.join(", ") : t("assistantEval.allGoldenTests")}</span>;
                        },
                      },
                      {
                        key: "gate",
                        title: t("assistantEval.col.gate"),
                        className: "nowrap",
                        render: (e) => (
                          <span className="mono">
                            {e?.blocking ? t("assistantEval.blocking") : t("assistantEval.informational")}
                            {e?.threshold !== null && e?.threshold !== undefined ? ` · ≥ ${e.threshold}` : ""}
                          </span>
                        ),
                      },
                      {
                        key: "status",
                        title: t("assistantEval.col.status"),
                        render: (e) => {
                          const res = opResources.find((r) => r.plan_key === e?.key);
                          if (res) return <ResourceStatus res={res} />;
                          return <Tag tone="gray">
                            {CLOUD_KINDS.has(String(e?.kind ?? "")) ? t("assistantEval.notCreated") : t("assistantEval.existingRef")}
                          </Tag>;
                        },
                      },
                    ]}
                  />
                </div>
              )}
              {recommendations.length > 0 && (
                <div className="v2-assistant-section" data-testid="v2-assistant-eval-recommendations">
                  <h3>{t("assistantEval.recommendations")}</h3>
                  <Table
                    density="dense"
                    rows={recommendations}
                    rowKey={(r) => String(r?.index)}
                    columns={[
                      { key: "i", title: "#", width: 48, render: (r) => <span className="mono">{Number(r?.index ?? 0) + 1}</span> },
                      { key: "text", title: t("assistantEval.col.recommendation"), render: (r) => String(r?.text ?? "") },
                      {
                        key: "map",
                        title: t("assistantEval.col.mappedTo"),
                        render: (r) => <span className="mono">{asArray<string>(r?.mapped_to).join(", ") || "—"}</span>,
                      },
                      {
                        key: "status",
                        title: t("assistantEval.col.status"),
                        render: (r) => (
                          <>
                            <Tag tone={r?.status === "mapped" ? "green" : r?.status === "declined" ? "gray" : "orange"}>
                              {t(`assistantEval.recStatus.${r?.status ?? "unresolved"}`)}
                            </Tag>
                            {r?.note && <span className="sub">{r.note}</span>}
                          </>
                        ),
                      },
                    ]}
                  />
                </div>
              )}

              {!operation && (
                <div className="v2-assistant-actions">
                  <Button kind="primary" disabled={!canCreate} title={createReason}
                    onClick={() => setConfirmPlan(current)} testId="v2-assistant-eval-create">
                    {t("assistantEval.create")}
                  </Button>
                  {createReason && <span className="v2-muted" style={{ fontSize: 12.5 }}>{createReason}</span>}
                  {current.status === "draft" && summary && summary.unresolved_recommendations > 0 && (
                    <span className="v2-muted" style={{ fontSize: 12.5 }}>
                      {t("assistantEval.unresolvedHint", { n: summary.unresolved_recommendations })}
                    </span>
                  )}
                </div>
              )}
            </>
          )}

          {requiresNewPlan && (
            <div style={{ marginTop: 12 }}>
              <Alert
                tone="warn"
                action={
                  <Button size="sm" kind="primary" disabled={!!replacementReason} title={replacementReason}
                    onClick={prepareReplacement} testId="v2-assistant-eval-replacement">
                    {t("assistantEval.prepareReplacement")}
                  </Button>
                }
              >
                <strong>{t("assistantEval.newPlanTitle")}</strong>
                <div>{t("assistantEval.newPlanNotice")}</div>
              </Alert>
            </div>
          )}

          {operation && (
            <OperationView
              operation={operation}
              pollError={pollError}
              canMaterialize={canMaterialize}
              busy={busy}
              onRefresh={() => { setPollFailures(0); load(); }}
              onRetry={() => {
                if (!requiresNewPlan) void run(() => api.assistantEvalOperationRetry(conversationId, operation.id, workspaceId));
              }}
              onCleanup={() => setConfirmCleanup(operation)}
            />
          )}

          {history.length > 0 && (
            <div className="v2-assistant-section" data-testid="v2-assistant-eval-history">
              <h3>{t("assistantEval.history")}</h3>
              <Table
                density="dense"
                rows={history}
                rowKey={(o) => o.id}
                columns={[
                  { key: "plan", title: t("v2.assistant.colPlan"), render: (o) => <span className="mono">r{o.plan_revision} · {o.plan_hash.slice(0, 12)}</span> },
                  { key: "status", title: t("assistantEval.col.status"), render: (o) => <Tag tone={OP_TONE[o.status] ?? "gray"}>{t(`assistantEval.opStatus.${o.status}`)}</Tag> },
                  { key: "by", title: t("v2.assistant.colApprovedBy"), render: (o) => o.approved_by },
                  {
                    key: "ops",
                    title: t("v2.common.actions"),
                    className: "right",
                    render: (o) => canMaterialize && o.status !== "cleaned" && o.status !== "running" && o.status !== "queued" ? (
                      <div className="v2-actions">
                        <LinkButton danger disabled={busy} onClick={() => setConfirmCleanup(o)}>{t("assistantEval.cleanup")}</LinkButton>
                      </div>
                    ) : null,
                  },
                ]}
              />
            </div>
          )}
        </div>
      </section>

      {operation && operation.status === "succeeded" && !requiresNewPlan && (
        <NextStepsCard
          operation={operation}
          proposal={proposals.find((p) => p.revision === operation.proposal_revision) ?? null}
          deployed={deployed}
        />
      )}

      <Confirm
        open={confirmPlan !== null}
        title={t("assistantEval.confirmTitle", { n: confirmPlan?.revision ?? 0 })}
        confirmLabel={t("assistantEval.confirmCreate")}
        body={
          <div className="v2-stack">
            {state?.disclosure && <pre className="v2-pre" style={{ maxHeight: 260 }}>{state.disclosure}</pre>}
            <Alert tone="warn">
              {t("assistantEval.confirmBody", {
                hash: confirmPlan?.content_hash.slice(0, 16) ?? "",
                cloud: confirmPlan?.summary?.cloud_evaluators ?? 0,
                lambdas: confirmPlan?.summary?.lambda_functions ?? 0,
                roles: (confirmPlan?.summary?.iam_roles ?? 0) + (confirmPlan?.summary?.role_grants ?? 0),
                workspace: workspaceId ?? "",
              })}
            </Alert>
          </div>
        }
        onConfirm={() => {
          const plan = confirmPlan;
          setConfirmPlan(null);
          if (plan && current && plan.id === current.id && plan.content_hash === current.content_hash) {
            void run(() => api.assistantEvalPlanMaterialize(conversationId, plan.revision, plan.content_hash, workspaceId));
          } else onError(t("assistantEval.dialogStale"));
        }}
        onClose={() => setConfirmPlan(null)}
      />
      <Confirm
        open={confirmCleanup !== null}
        danger
        title={t("assistantEval.cleanupTitle")}
        body={t("assistantEval.cleanupBody", { plan: confirmCleanup?.plan_revision ?? 0 })}
        confirmLabel={t("assistantEval.cleanupConfirm")}
        onConfirm={() => {
          const op = confirmCleanup;
          setConfirmCleanup(null);
          if (op) void run(() => api.assistantEvalOperationCleanup(conversationId, op.id, workspaceId));
        }}
        onClose={() => setConfirmCleanup(null)}
      />
    </>
  );
}

function ScenarioTurns({ s }: { s: Scenario }) {
  const { t } = useTranslation();
  const turns = asArray<{ input?: string; expected_response?: string }>(s?.turns);
  return (
    <details>
      <summary style={{ cursor: "pointer" }}>{t("assistantEval.turnsCount", { n: turns.length })}</summary>
      <ol className="v2-assistant-turns">
        {turns.map((turn, k) => (
          <li key={k}>
            <div>{String(turn?.input ?? "")}</div>
            {turn?.expected_response ? <div className="v2-muted">→ {turn.expected_response}</div> : null}
          </li>
        ))}
      </ol>
      {asArray<string>(s?.assertions).length > 0 && (
        <div className="v2-muted" style={{ fontSize: 12 }}>
          {t("assistantEval.assertions")}: {asArray<string>(s?.assertions).join(" · ")}
        </div>
      )}
      {asArray<string>(s?.expected_trajectory).length > 0 && (
        <div className="v2-muted mono" style={{ fontSize: 12 }}>
          {t("assistantEval.expectedTrajectory")}: {asArray<string>(s?.expected_trajectory).join(" → ")}
        </div>
      )}
    </details>
  );
}

function EvaluatorDefinition({ e }: { e: AssistantEvalPlanEvaluator }) {
  const { t } = useTranslation();
  const kind = String(e?.kind ?? "");
  return (
    <div style={{ fontSize: 12.5, minWidth: 180 }}>
      {kind === "existing" && <div className="mono">{String(e?.evaluator_id ?? "")}</div>}
      {kind === "judge" && (
        <details className="mono">
          <summary style={{ cursor: "pointer" }}>
            {e?.name} · {e?.level} · {e?.model_id} · {t("assistantEval.fullRubric")}
          </summary>
          <pre className="v2-pre" style={{ marginTop: 6 }}>{String(e?.instructions ?? "")}</pre>
          {asArray<{ value: number; label: string; definition: string }>(e?.rating_scale).length > 0 && (
            <ul className="v2-list">
              {asArray<{ value: number; label: string; definition: string }>(e?.rating_scale).map((r, k) => (
                <li key={k}>{r.value} · {r.label} — {r.definition}</li>
              ))}
            </ul>
          )}
        </details>
      )}
      {kind === "derived" && <div className="mono v2-muted">{e?.name} · {e?.base_evaluator_id} · {e?.model_id}</div>}
      {kind === "code" && (
        <details className="mono">
          <summary style={{ cursor: "pointer" }}>
            {e?.name} · {e?.level} · {t("assistantEval.rulesCount", { n: asArray(e?.rules?.checks).length })}
          </summary>
          <pre className="v2-pre" style={{ marginTop: 6 }}>
            {asArray<Record<string, unknown>>(e?.rules?.checks).map((c) => compactRule(c)).join("\n")}
          </pre>
        </details>
      )}
      {e?.note && <div className="v2-muted">{e.note}</div>}
    </div>
  );
}

function OperationView({
  operation, pollError, canMaterialize, busy, onRefresh, onRetry, onCleanup,
}: {
  operation: AssistantEvalOperation;
  pollError: string | null;
  canMaterialize: boolean;
  busy: boolean;
  onRefresh: () => void;
  onRetry: () => void;
  onCleanup: () => void;
}) {
  const { t } = useTranslation();
  const live = operation.status === "running" || operation.status === "queued";
  return (
    <div className="v2-assistant-section" data-testid="v2-assistant-eval-operation" data-status={operation.status}>
      <h3 className="v2-row">
        {t("assistantEval.operation")}
        <Tag tone={OP_TONE[operation.status] ?? "gray"} dot>{t(`assistantEval.opStatus.${operation.status}`)}</Tag>
        {live && !pollError && <LinkButton onClick={onRefresh}>{t("assistantEval.refresh")}</LinkButton>}
      </h3>
      <div className="v2-muted mono" style={{ fontSize: 12, marginBottom: 8 }}>
        {t("assistantEval.operationMeta", {
          by: operation.approved_by,
          account: operation.account_id,
          region: operation.region,
          plan: operation.plan_revision,
          hash: operation.plan_hash.slice(0, 12),
          attempts: operation.attempts,
          max: operation.max_attempts,
        })}
      </div>
      {operation.error && <Alert tone="error"><span className="mono" style={{ whiteSpace: "pre-wrap" }}>{operation.error}</span></Alert>}
      {pollError && (
        <Alert tone="error" action={<LinkButton onClick={onRefresh}>{t("assistantEval.refresh")}</LinkButton>}>
          {t("assistantEval.pollError", { message: pollError })}
        </Alert>
      )}
      <Table
        density="dense"
        rows={asArray<AssistantEvalResource>(operation.resources)}
        rowKey={(r) => r.key}
        testId="v2-assistant-eval-resources"
        columns={[
          { key: "kind", title: t("assistantEval.col.resource"), render: (r) => <span className="mono">{t(`assistantEval.resource.${r.kind}`, r.kind)}</span> },
          {
            key: "name",
            title: t("assistantEval.col.name"),
            render: (r) => (
              <>
                <span className="mono" style={{ wordBreak: "break-all" }}>{r.name}</span>
                {r.code_group && <span className="sub">{t("assistantEval.forEvaluator", { name: r.code_group })}</span>}
              </>
            ),
          },
          { key: "status", title: t("assistantEval.col.status"), render: (r) => <ResourceStatus res={r} /> },
          { key: "details", title: t("assistantEval.col.details"), render: (r) => <ResourceDetails res={r} /> },
        ]}
      />
      <div style={{ marginTop: 8 }}><Alert>{t("assistantEval.registeredNote")}</Alert></div>
      {canMaterialize && (
        <div className="v2-row">
          {!operation.requires_new_plan && (operation.status === "partial" || operation.status === "failed")
            && operation.attempts < operation.max_attempts && (
            <Button disabled={busy} onClick={onRetry} testId="v2-assistant-eval-retry">{t("assistantEval.retry")}</Button>
          )}
          {operation.status !== "cleaned" && !live && operation.status !== "cleaning" && (
            <Button kind="danger" disabled={busy} onClick={onCleanup} testId="v2-assistant-eval-cleanup">
              {t("assistantEval.cleanup")}
            </Button>
          )}
        </div>
      )}
    </div>
  );
}

function ResourceStatus({ res }: { res: AssistantEvalResource }) {
  const { t } = useTranslation();
  return (
    <>
      <span className="v2-tags">
        <Tag tone={RES_TONE[res.status] ?? "gray"}>{t(`assistantEval.resStatus.${res.status}`, res.status)}</Tag>
        {res.recovered && <Tag tone="gray">{t("assistantEval.recovered")}</Tag>}
      </span>
      {res.error && <div className="mono" style={{ color: "var(--v2-danger)", fontSize: 12, whiteSpace: "pre-wrap" }}>{res.error}</div>}
    </>
  );
}

function ResourceDetails({ res }: { res: AssistantEvalResource }) {
  const { t } = useTranslation();
  const r = asRecord(res.result);
  const readback = asRecord(r.readback);
  return (
    <div className="mono" style={{ fontSize: 12, wordBreak: "break-all" }}>
      {res.link && <div><Link to={res.link}>{t("assistantEval.open")} ›</Link></div>}
      {res.kind === "dataset" && r.dataset_id ? (
        <div>{String(r.dataset_id)} · {t("assistantEval.items", { n: Number(r.item_count ?? 0) })}</div>
      ) : null}
      {(res.kind === "evaluator" || res.kind === "existing") && r.evaluator_id ? (
        <div>
          {String(r.evaluator_id)}
          {r.level ? ` · ${String(r.level)}` : ""}
          {res.reference_dependent ? ` · ${t("assistantEval.referenceDependent")}` : ""}
        </div>
      ) : null}
      {res.kind === "lambda_function" && (
        <div>
          {r.version_arn ? String(r.version_arn) : String(r.function_arn ?? "")}
          {res.digest ? <div>sha256 {String(res.digest).slice(0, 16)}…</div> : null}
          {Object.keys(readback).length ? (
            <div>
              {String(readback.Runtime ?? "")} · {String(readback.Handler ?? "")} · v{String(readback.Version ?? "")}
              {" · "}{String(readback.Timeout ?? "")}s · {String(readback.MemorySize ?? "")}MB
              {" · "}{t("assistantEval.reserved", { n: Number(readback.ReservedConcurrentExecutions ?? 0) })}
            </div>
          ) : null}
        </div>
      )}
      {res.kind === "lambda_role" && r.role_arn ? <div>{String(r.role_arn)}</div> : null}
      {res.kind === "role_grant" && r.role_arn ? (
        <div>
          {String(r.role_arn)} · {String(r.policy_name ?? "")}
          {asArray<string>(asRecord(asArray<Record<string, unknown>>(asRecord(r.policy_document).Statement)[0]).Resource).map((arn) => (
            <div key={arn}>→ {arn}</div>
          ))}
        </div>
      ) : null}
      {res.kind === "lambda_permission" && r.principal ? (
        <div>{String(r.principal)} · SourceAccount {String(r.source_account ?? "")} · v{String(r.qualifier ?? "")}</div>
      ) : null}
      {res.kind === "log_group" && r.retention_days ? <div>{t("assistantEval.retention", { n: Number(r.retention_days) })}</div> : null}
      {res.cleanup && res.cleanup.note ? <div className="v2-muted">{res.cleanup.note}</div> : null}
    </div>
  );
}
