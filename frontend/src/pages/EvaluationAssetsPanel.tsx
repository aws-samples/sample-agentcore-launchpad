import type { CSSProperties, ReactNode } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Btn, Chip, ConfirmDialog, Panel } from "../components";
import type { ChipTone } from "../components";
import type {
  AssistantEvalOperation,
  AssistantEvalPlan,
  AssistantEvalPlanContent,
  AssistantEvalPlanEvaluator,
  AssistantEvalPlanRepair,
  AssistantEvalPlanState,
  AssistantEvalResource,
  AssistantProposal,
} from "../lib/api";
import { api, ApiError } from "../lib/api";
import { AssistantNextSteps } from "./AssistantNextSteps";
import type { DeployedAgent } from "./AssistantNextSteps";

/**
 * SE-047 — the reviewed evaluation-assets plan of one assistant conversation.
 *
 * Prepare (platform draft from a proposal revision) → review the structured mapping
 * (golden tests → single-session scenarios with their turns and references, or blocked
 * with a reason; recommendations → AgentCore evaluator kinds) → confirm or block
 * review-required scenarios / edit as JSON (a new plan revision) → an administrator who
 * owns the conversation confirms the disclosure and CREATES the assets. Creation is
 * separate from testing: nothing here runs an evaluation, deploys an agent, syncs to
 * AWS Datasets. Asking the assistant to repair an invalid plan explicitly invokes
 * one discussion turn, then prepares the returned proposal revision for review.
 * Every request pins the DISPLAYED workspace.
 */

const POLL_MS = 3000;
const POLL_BACKOFF_MAX_MS = 15000;

const OP_TONE: Record<string, ChipTone> = {
  queued: "muted",
  running: "warn",
  succeeded: "good",
  partial: "warn",
  failed: "crit",
  cleaning: "warn",
  cleaned: "muted",
};

const RES_TONE: Record<string, ChipTone> = {
  pending: "muted",
  accepted: "warn",
  ready: "good",
  failed: "crit",
  conflict: "crit",
  blocked: "muted",
  skipped: "muted",
  retained: "warn",
  deleted: "muted",
  delete_failed: "crit",
};

const PLAN_TONE: Record<string, ChipTone> = {
  draft: "muted",
  invalid: "crit",
  approved: "good",
  superseded: "muted",
};

const CLOUD_KINDS = new Set(["judge", "derived", "code"]);

function asArray<T>(v: unknown): T[] {
  return Array.isArray(v) ? (v as T[]) : [];
}

function asRecord(v: unknown): Record<string, unknown> {
  return v && typeof v === "object" && !Array.isArray(v) ? (v as Record<string, unknown>) : {};
}

/** A rule without its empty / default members (what a reviewer needs to read). */
function compactRule(rule: unknown): string {
  if (!rule || typeof rule !== "object" || Array.isArray(rule)) return JSON.stringify(rule ?? null);
  const kept = Object.fromEntries(
    Object.entries(rule as Record<string, unknown>).filter(
      ([, v]) => !(v === null || v === "" || v === false || (Array.isArray(v) && v.length === 0)),
    ),
  );
  return JSON.stringify(kept);
}

function Wrap({ children, testid }: { children: ReactNode; testid: string }) {
  return (
    <div className="assist-table-wrap" data-testid={testid}>
      {children}
    </div>
  );
}

export function EvaluationAssetsPanel({
  conversationId,
  proposals,
  canMaterialize,
  workspaceId,
  apiMessage,
  onError,
  index,
  deployed = null,
  onRepair,
  repairDisabledReason,
}: {
  conversationId: string;
  proposals: AssistantProposal[];
  canMaterialize: boolean;
  workspaceId: string | null;
  apiMessage: (err: unknown) => string;
  onError: (message: string) => void;
  index: number;
  /** live status of the Agent deployed from this conversation (for NEXT STEPS) */
  deployed?: DeployedAgent | null;
  /** Resolves only after the shared turn finishes and its new proposal is read back. */
  onRepair: (repair: AssistantEvalPlanRepair) => Promise<AssistantProposal | null>;
  repairDisabledReason?: string;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<AssistantEvalPlanState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const busyRef = useRef(false);
  const [repairStatus, setRepairStatus] = useState<
    "asking" | "preparing" | "ready" | "invalid" | null
  >(null);
  const [sourceRevision, setSourceRevision] = useState<number>(
    proposals.length ? proposals[proposals.length - 1].revision : 1,
  );
  const [jsonDraft, setJsonDraft] = useState<string | null>(null);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [confirmPlan, setConfirmPlan] = useState<AssistantEvalPlan | null>(null);
  const [confirmCleanup, setConfirmCleanup] = useState<AssistantEvalOperation | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  // per-row BLOCK: the golden test whose reason is being typed (inline, one at a time)
  const [blocking, setBlocking] = useState<{ goldenTestId: string; reason: string } | null>(null);
  const [pollError, setPollError] = useState<string | null>(null);
  const [pollFailures, setPollFailures] = useState(0);

  // staleness: a generation bumps on every conversation/workspace change and on
  // unmount, so a late response (even A→B→A) can never land in another context
  const generation = useRef(0);
  const alive = useRef(true);
  const ctx = useRef({ conversationId, workspaceId });
  ctx.current = { conversationId, workspaceId };
  const stillCurrent = useCallback(
    (gen: number) => alive.current && generation.current === gen,
    [],
  );

  const load = useCallback(() => {
    const gen = generation.current;
    const { conversationId: cid, workspaceId: wid } = ctx.current;
    setLoadError(null);
    void api
      .assistantEvalPlan(cid, wid)
      .then((res) => {
        if (stillCurrent(gen)) setState(res);
      })
      .catch((err) => {
        if (stillCurrent(gen)) setLoadError(apiMessage(err));
      });
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

  // poll a live operation (ledger read only), retrying after failures with backoff
  // and a visible error instead of silently stopping
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
          setState((prev) =>
            prev
              ? {
                  ...prev,
                  operations: prev.operations.map((o) =>
                    o.id === res.operation.id ? res.operation : o,
                  ),
                }
              : prev,
          );
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
          setState((prev) =>
            prev
              ? {
                  ...prev,
                  operations: prev.operations.some((o) => o.id === res.operation.id)
                    ? prev.operations.map((o) => (o.id === res.operation.id ? res.operation : o))
                    : [...prev.operations, res.operation],
                }
              : prev,
          );
          load(); // the plan's own status (draft → approved) changed too
        }
      } catch (err) {
        if (!stillCurrent(gen)) return;
        const message = apiMessage(err);
        setActionError(message);
        onError(message);
      } finally {
        if (stillCurrent(gen)) {
          busyRef.current = false;
          setBusy(false);
        }
      }
    },
    [apiMessage, onError, stillCurrent, load],
  );

  const prepare = () =>
    run(
      () => api.assistantEvalPlanPrepare(conversationId, sourceRevision, workspaceId),
      () => setJsonDraft(null),
    );

  const repairReason = jsonDraft !== null
    ? t("assistantEval.repairJsonOpen")
    : busy
      ? t("assistantEval.repairBusy")
      : repairDisabledReason;

  const repair = async () => {
    if (!current || current.status !== "invalid" || busyRef.current || repairReason) return;
    const gen = generation.current;
    const { conversationId: cid, workspaceId: wid } = ctx.current;
    busyRef.current = true;
    setBusy(true);
    setActionError(null);
    setRepairStatus("asking");
    try {
      const proposal = await onRepair({
        plan_revision: current.revision,
        plan_hash: current.content_hash,
      });
      if (!stillCurrent(gen)) return;
      if (!proposal) throw new Error(t("assistantEval.repairNoProposal"));
      // Use the exact returned revision, never a possibly stale dropdown selection.
      setSourceRevision(proposal.revision);
      setRepairStatus("preparing");
      const res = await api.assistantEvalPlanPrepare(cid, proposal.revision, wid);
      if (!stillCurrent(gen)) return;
      setState(res);
      setRepairStatus(
        res.plan.status === "invalid" || res.plan.validation_errors.length > 0 ? "invalid" : "ready",
      );
    } catch (err) {
      if (!stillCurrent(gen)) return;
      setRepairStatus(null);
      const message = apiMessage(err);
      setActionError(message);
      onError(message);
      if (
        err instanceof ApiError &&
        (err.code === "assistant.evaluation_repair_stale" ||
          err.code === "assistant.evaluation_repair_not_needed")
      ) load();
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
    void run(
      () => api.assistantEvalPlanEdit(conversationId, parsed, workspaceId),
      () => setJsonDraft(null),
    );
  };

  /** Confirm every review-required scenario as typed steps (a new revision). */
  const confirmScenarios = () => {
    if (!current) return;
    const content = asRecord(current.content);
    const scenarios = asArray<Record<string, unknown>>(content.scenarios).map((s) => ({
      ...s,
      review_required: false,
    }));
    void run(() =>
      api.assistantEvalPlanEdit(conversationId, { ...content, scenarios }, workspaceId),
    );
  };

  /** Confirm ONE review-required scenario (a new revision). */
  const confirmOne = (goldenTestId: string) => {
    if (!current) return;
    const content = asRecord(current.content);
    const scenarios = asArray<Record<string, unknown>>(content.scenarios).map((s) =>
      String(s.golden_test_id) === goldenTestId ? { ...s, review_required: false } : s,
    );
    void run(() =>
      api.assistantEvalPlanEdit(conversationId, { ...content, scenarios }, workspaceId),
    );
  };

  /** BLOCK one golden test: it leaves `scenarios`, lands in `blocked_golden_tests` with
   *  the member's reason, and is dropped from every evaluator's `golden_test_ids` (an
   *  evaluator may only target all remaining tests). A new plan revision — nothing on
   *  AWS changes; the test stays visible in the proposal as a manual task. */
  const blockOne = (goldenTestId: string, reason: string) => {
    if (!current) return;
    const content = asRecord(current.content);
    const scenarios = asArray<Record<string, unknown>>(content.scenarios).filter(
      (s) => String(s.golden_test_id) !== goldenTestId,
    );
    const blockedList = asArray<Record<string, unknown>>(content.blocked_golden_tests).filter(
      (b) => String(b.golden_test_id) !== goldenTestId,
    );
    const evaluators = asArray<Record<string, unknown>>(content.evaluators).map((e) => ({
      ...e,
      golden_test_ids: asArray<string>(e.golden_test_ids).filter((g) => g !== goldenTestId),
    }));
    setBlocking(null);
    void run(() =>
      api.assistantEvalPlanEdit(
        conversationId,
        {
          ...content,
          scenarios,
          evaluators,
          blocked_golden_tests: [...blockedList, { golden_test_id: goldenTestId, reason }],
        },
        workspaceId,
      ),
    );
  };

  /** UNBLOCK: draft the golden test again as ONE single-turn scenario marked
   *  review-required — the same shape the platform draft uses — from the proposal
   *  revision this plan is bound to. */
  const unblockOne = (goldenTestId: string) => {
    if (!current) return;
    const content = asRecord(current.content);
    const source = proposals.find((p) => p.revision === Number(content.source_revision));
    const gt = asArray<Record<string, unknown>>(source?.content.golden_tests).find(
      (g) => String(g.id) === goldenTestId,
    );
    if (!gt) {
      onError(t("assistantEval.unblockMissing", { id: goldenTestId }));
      return;
    }
    const assertions: string[] = [];
    if (gt.pass_criteria) assertions.push(String(gt.pass_criteria).slice(0, 1000));
    if (gt.forbidden_behavior) {
      assertions.push(`Must not: ${String(gt.forbidden_behavior)}`.slice(0, 1000));
    }
    const scenario = {
      scenario_id: goldenTestId.replace(/[^A-Za-z0-9_.-]+/g, "-").replace(/^[-.]+|[-.]+$/g, "") || "gt",
      golden_test_id: goldenTestId,
      turns: [{
        input: String(gt.input ?? "").slice(0, 8000),
        expected_response: String(gt.expected_response ?? "").slice(0, 2000),
      }],
      expected_trajectory: asArray<unknown>(gt.expected_tools).map((x) => String(x).slice(0, 200)),
      assertions,
      note: "re-drafted from the golden test after unblocking — confirm, rewrite or block it",
      review_required: true,
    };
    const blockedList = asArray<Record<string, unknown>>(content.blocked_golden_tests).filter(
      (b) => String(b.golden_test_id) !== goldenTestId,
    );
    void run(() =>
      api.assistantEvalPlanEdit(
        conversationId,
        {
          ...content,
          scenarios: [...asArray<Record<string, unknown>>(content.scenarios), scenario],
          blocked_golden_tests: blockedList,
        },
        workspaceId,
      ),
    );
  };

  const materialize = (plan: AssistantEvalPlan) =>
    run(() =>
      api.assistantEvalPlanMaterialize(conversationId, plan.revision, plan.content_hash, workspaceId),
    );

  const content = asRecord(current?.content) as Partial<AssistantEvalPlanContent>;
  const evaluators = asArray<AssistantEvalPlanEvaluator>(content.evaluators);
  const recommendations = asArray<NonNullable<AssistantEvalPlanContent["recommendations"]>[number]>(
    content.recommendations,
  );
  const scenarios = asArray<NonNullable<AssistantEvalPlanContent["scenarios"]>[number]>(
    content.scenarios,
  );
  const blocked = asArray<NonNullable<AssistantEvalPlanContent["blocked_golden_tests"]>[number]>(
    content.blocked_golden_tests,
  );
  const invalid = !!current && current.validation_errors.length > 0;
  const reviewPending = scenarios.filter((s) => s?.review_required).length;
  // A plan whose members have the expected shape is shown as tables even while it
  // does not validate yet (a fresh draft always needs review; a routing error names
  // an evaluator): the row actions ARE the way to fix it. Only a malformed plan
  // (JSON edited into another shape) falls back to the raw dump.
  const structured =
    !!current && Array.isArray(content.scenarios) && Array.isArray(content.evaluators);
  const summary = current?.summary;
  const canCreate =
    !!current && current.status === "draft" && !invalid && canMaterialize && !busy && !operation;
  const createReason = !canMaterialize
    ? t("assistantEval.adminOnly")
    : invalid
      ? reviewPending
        ? t("assistantEval.reviewPending", { n: reviewPending })
        : t("assistantEval.fixErrors")
      : undefined;
  const opResources = asArray<AssistantEvalResource>(operation?.resources);

  return (
    <>
      <Panel
        brk
        className="assist-span"
        title={t("assistantEval.title")}
        sub={t("assistantEval.sub")}
        data-testid="evaluation-assets"
        data-workspace={workspaceId ?? ""}
        end={
          current ? (
            <>
              <Chip tone="muted" className="mono">
                {t("assistantEval.planRevision", { n: current.revision })}
              </Chip>
              <Chip tone={PLAN_TONE[current.status] ?? "muted"}>
                {t(`assistantEval.planStatus.${current.status}`)}
              </Chip>
            </>
          ) : undefined
        }
        style={{ "--i": index } as CSSProperties}
      >
        <div className="note" style={{ marginBottom: 10 }} data-testid="eval-scope-note">
          <span className="i">[i]</span>
          <span>{t("assistantEval.scopeNote")}</span>
        </div>
        {loadError && (
          <div className="note" style={{ borderColor: "var(--crit)" }} data-testid="eval-load-error">
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span>
              {loadError}{" "}
              <Btn data-testid="eval-reload" onClick={load}>{t("assistantEval.refresh")}</Btn>
            </span>
          </div>
        )}

        <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <label className="dim" style={{ fontSize: 11 }}>
            {t("assistantEval.sourceRevision")}
            <select
              className="input mono"
              style={{ marginLeft: 6, width: "auto", padding: "6px 10px", cursor: "pointer" }}
              value={sourceRevision}
              disabled={busy}
              data-testid="eval-source-revision"
              onChange={(e) => setSourceRevision(Number(e.target.value))}
            >
              {proposals
                .filter((p) => p.status !== "invalid")
                .map((p) => (
                  <option key={p.id} value={p.revision}>
                    r{p.revision} · {t(`assistantPage.status.${p.status}`)}
                  </option>
                ))}
            </select>
          </label>
          <Btn disabled={busy || !proposals.length} data-testid="eval-prepare" onClick={() => void prepare()}>
            {current ? t("assistantEval.prepareAgain") : t("assistantEval.prepare")}
          </Btn>
          {current && jsonDraft === null && !operation && (
            <Btn
              disabled={busy}
              data-testid="eval-edit-json"
              onClick={() => setJsonDraft(JSON.stringify(current.content, null, 2))}
            >
              {t("assistantEval.editJson")}
            </Btn>
          )}
          {current && !operation && reviewPending > 0 && jsonDraft === null && (
            <Btn disabled={busy} data-testid="eval-confirm-scenarios" onClick={confirmScenarios}>
              {t("assistantEval.confirmScenarios", { n: reviewPending })}
            </Btn>
          )}
        </div>

        {!current && (
          <div className="empty" data-testid="eval-plan-empty" style={{ marginTop: 10 }}>
            {t("assistantEval.empty")}
          </div>
        )}

        {invalid && (
          <div
            className="note"
            style={{ borderColor: "var(--crit)", marginTop: 10 }}
            data-testid="eval-plan-invalid"
          >
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span>
              {reviewPending ? t("assistantEval.reviewNote") : t("assistantEval.invalidNote")}
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {current!.validation_errors.map((e, i) => (
                  <li key={i} className="mono" style={{ fontSize: 11, wordBreak: "break-word" }}>{e}</li>
                ))}
              </ul>
              {current?.status === "invalid" && (
                <div style={{ marginTop: 10 }} data-testid="eval-repair-actions">
                  <Btn
                    disabled={!!repairReason}
                    disabledReason={repairReason}
                    data-testid="eval-repair"
                    onClick={() => void repair()}
                  >
                    {t("assistantEval.repair")}
                  </Btn>
                  <div className="dim" style={{ fontSize: 11, marginTop: 6 }} data-testid="eval-repair-hint">
                    {t("assistantEval.repairHint")}
                  </div>
                </div>
              )}
            </span>
          </div>
        )}

        {repairStatus && (
          <div
            className="note"
            role="status"
            aria-live="polite"
            style={{ marginTop: 10 }}
            data-testid="eval-repair-status"
            data-status={repairStatus}
          >
            <span>{t(`assistantEval.repairStatus.${repairStatus}`)}</span>
          </div>
        )}

        {actionError && (
          <div className="note" role="alert" style={{ borderColor: "var(--crit)", marginTop: 10 }} data-testid="eval-action-error">
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span>{actionError}</span>
          </div>
        )}

        {current && jsonDraft !== null && (
          <div className="assist-section" data-testid="eval-json-editor">
            <h4>{t("assistantEval.jsonTitle")}</h4>
            <textarea
              className="mono"
              style={{ width: "100%", minHeight: 320, fontSize: 11 }}
              value={jsonDraft}
              data-testid="eval-json"
              onChange={(e) => setJsonDraft(e.target.value)}
            />
            {jsonError && (
              <div className="mono" style={{ color: "var(--crit)", fontSize: 11 }} data-testid="eval-json-error">
                {jsonError}
              </div>
            )}
            <div className="row" style={{ gap: 8, marginTop: 6 }}>
              <Btn primary disabled={busy} data-testid="eval-json-save" onClick={saveJson}>
                {t("assistantEval.saveRevision")}
              </Btn>
              <Btn
                data-testid="eval-json-cancel"
                onClick={() => {
                  setJsonDraft(null);
                  setJsonError(null);
                }}
              >
                {t("assistantEval.cancel")}
              </Btn>
            </div>
          </div>
        )}

        {current && !structured && jsonDraft === null && (
          <div className="assist-section" data-testid="eval-invalid-raw">
            <h4>{t("assistantEval.rawTitle")}</h4>
            <pre className="assist-pre" data-testid="eval-invalid-raw-json">
              {JSON.stringify(current.content, null, 2)}
            </pre>
          </div>
        )}
        {current && structured && jsonDraft === null && (
          <>
            {summary && (
              <div className="dim mono" style={{ fontSize: 11, marginTop: 10 }} data-testid="eval-summary">
                {t("assistantEval.summaryLine", {
                  scenarios: summary.scenarios,
                  blocked: summary.blocked_golden_tests,
                  cloud: summary.cloud_evaluators,
                  lambdas: summary.lambda_functions,
                  roles: summary.iam_roles + summary.role_grants,
                  unresolved: summary.unresolved_recommendations,
                })}
                {" · "}
                <span data-testid="eval-plan-hash">{current.content_hash.slice(0, 12)}</span>
              </div>
            )}
            {(scenarios.length > 0 || blocked.length > 0) && (
              <div className="assist-section" data-testid="eval-scenarios">
                <h4>{t("assistantEval.scenarios")}</h4>
                <Wrap testid="eval-scenarios-table">
                  <table className="assist-gt">
                    <thead>
                      <tr>
                        <th>{t("assistantEval.col.goldenTest")}</th>
                        <th>{t("assistantEval.col.scenario")}</th>
                        <th>{t("assistantEval.col.turns")}</th>
                        <th>{t("assistantEval.col.status")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {scenarios.map((s, i) => (
                        <tr key={`${s?.scenario_id ?? i}`} data-testid={`eval-scenario-${s?.golden_test_id ?? i}`}>
                          <td className="mono">{String(s?.golden_test_id ?? "")}</td>
                          <td className="mono">{String(s?.scenario_id ?? "")}</td>
                          <td>
                            <details>
                              <summary>{t("assistantEval.turnsCount", { n: asArray(s?.turns).length })}</summary>
                              <ol className="assist-steps">
                                {asArray<{ input?: string; expected_response?: string }>(s?.turns).map((turn, k) => (
                                  <li key={k}>
                                    <div>{String(turn?.input ?? "")}</div>
                                    {turn?.expected_response ? (
                                      <div className="dim">→ {turn.expected_response}</div>
                                    ) : null}
                                  </li>
                                ))}
                              </ol>
                              {asArray<string>(s?.assertions).length > 0 && (
                                <div className="dim" style={{ fontSize: 11 }}>
                                  {t("assistantEval.assertions")}: {asArray<string>(s?.assertions).join(" · ")}
                                </div>
                              )}
                              {asArray<string>(s?.expected_trajectory).length > 0 && (
                                <div className="dim mono" style={{ fontSize: 11 }}>
                                  {t("assistantEval.expectedTrajectory")}: {asArray<string>(s?.expected_trajectory).join(" → ")}
                                </div>
                              )}
                            </details>
                          </td>
                          <td>
                            {s?.review_required ? (
                              <Chip tone="warn">{t("assistantEval.reviewRequired")}</Chip>
                            ) : (
                              <Chip tone="good">{t("assistantEval.confirmed")}</Chip>
                            )}
                            {s?.note ? <div className="dim" style={{ fontSize: 11 }}>{s.note}</div> : null}
                            {!operation && jsonDraft === null && (
                              blocking?.goldenTestId === String(s?.golden_test_id) ? (
                                <div className="assist-row-actions" data-testid={`eval-block-form-${s?.golden_test_id}`}>
                                  <input
                                    className="input mono"
                                    style={{ width: "100%", padding: "6px 8px" }}
                                    value={blocking.reason}
                                    placeholder={t("assistantEval.blockReasonPlaceholder")}
                                    onChange={(e) => setBlocking({ ...blocking, reason: e.target.value })}
                                    data-testid="eval-block-reason"
                                  />
                                  <div className="row" style={{ gap: 6, marginTop: 6 }}>
                                    <Btn
                                      primary
                                      disabled={busy || !blocking.reason.trim()}
                                      disabledReason={t("assistantEval.blockReasonRequired")}
                                      onClick={() => blockOne(blocking.goldenTestId, blocking.reason.trim())}
                                      data-testid="eval-block-confirm"
                                    >
                                      {t("assistantEval.blockConfirm")}
                                    </Btn>
                                    <Btn disabled={busy} onClick={() => setBlocking(null)} data-testid="eval-block-cancel">
                                      {t("assistantEval.cancel")}
                                    </Btn>
                                  </div>
                                </div>
                              ) : (
                                <div className="assist-row-actions">
                                  {s?.review_required && (
                                    <button
                                      type="button"
                                      className="rowact"
                                      disabled={busy}
                                      onClick={() => confirmOne(String(s?.golden_test_id))}
                                      data-testid={`eval-confirm-${s?.golden_test_id}`}
                                    >
                                      {t("assistantEval.confirmOne")}
                                    </button>
                                  )}
                                  <button
                                    type="button"
                                    className="rowact"
                                    disabled={busy}
                                    onClick={() => setBlocking({ goldenTestId: String(s?.golden_test_id), reason: "" })}
                                    data-testid={`eval-block-${s?.golden_test_id}`}
                                  >
                                    {t("assistantEval.block")}
                                  </button>
                                </div>
                              )
                            )}
                          </td>
                        </tr>
                      ))}
                      {blocked.map((b, i) => (
                        <tr key={`b-${b?.golden_test_id ?? i}`} data-testid={`eval-blocked-${b?.golden_test_id ?? i}`}>
                          <td className="mono">{String(b?.golden_test_id ?? "")}</td>
                          <td colSpan={3}>
                            <Chip tone="warn">{t("assistantEval.blocked")}</Chip> {String(b?.reason ?? "")}
                            {!operation && jsonDraft === null && (
                              <>
                                {" "}
                                <button
                                  type="button"
                                  className="rowact"
                                  disabled={busy}
                                  onClick={() => unblockOne(String(b?.golden_test_id))}
                                  data-testid={`eval-unblock-${b?.golden_test_id}`}
                                >
                                  {t("assistantEval.unblock")}
                                </button>
                              </>
                            )}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Wrap>
              </div>
            )}
            {evaluators.length > 0 && (
              <div className="assist-section" data-testid="eval-evaluators">
                <h4>{t("assistantEval.evaluators")}</h4>
                <Wrap testid="eval-evaluators-table">
                  <table className="assist-gt assist-gt-wide">
                    <thead>
                      <tr>
                        <th>{t("assistantEval.col.key")}</th>
                        <th>{t("assistantEval.col.kind")}</th>
                        <th className="col-def">{t("assistantEval.col.definition")}</th>
                        <th>{t("assistantEval.col.goldenTests")}</th>
                        <th className="col-gate">{t("assistantEval.col.gate")}</th>
                        <th className="col-status">{t("assistantEval.col.status")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {evaluators.map((e, i) => {
                        const res = opResources.find((r) => r.plan_key === e?.key);
                        const kind = String(e?.kind ?? "");
                        const gts = asArray<string>(e?.golden_test_ids);
                        return (
                          <tr key={`${e?.key ?? i}`} data-testid={`eval-evaluator-${e?.key ?? i}`}>
                            <td className="mono">{String(e?.key ?? "")}</td>
                            <td>
                              <Chip tone={CLOUD_KINDS.has(kind) ? "good" : "muted"}>
                                {t(`assistantEval.kind.${kind}`, kind)}
                              </Chip>
                              {e?.draft && <Chip tone="warn">{t("assistantEval.draftRubric")}</Chip>}
                            </td>
                            <td className="col-def" style={{ fontSize: 11 }}>
                              <EvaluatorDefinition e={e} />
                            </td>
                            <td className="mono">{gts.length ? gts.join(", ") : t("assistantEval.allGoldenTests")}</td>
                            <td className="mono col-gate">
                              {e?.blocking ? t("assistantEval.blocking") : t("assistantEval.informational")}
                              {e?.threshold !== null && e?.threshold !== undefined ? ` · ≥ ${e.threshold}` : ""}
                            </td>
                            <td>
                              {res ? (
                                <ResourceStatus res={res} />
                              ) : CLOUD_KINDS.has(kind) ? (
                                <Chip tone="muted">{t("assistantEval.notCreated")}</Chip>
                              ) : (
                                <Chip tone="muted">{t("assistantEval.existingRef")}</Chip>
                              )}
                            </td>
                          </tr>
                        );
                      })}
                    </tbody>
                  </table>
                </Wrap>
              </div>
            )}
            {recommendations.length > 0 && (
              <div className="assist-section" data-testid="eval-recommendations">
                <h4>{t("assistantEval.recommendations")}</h4>
                <Wrap testid="eval-recommendations-table">
                  <table className="assist-gt">
                    <thead>
                      <tr>
                        <th>#</th>
                        <th>{t("assistantEval.col.recommendation")}</th>
                        <th>{t("assistantEval.col.mappedTo")}</th>
                        <th>{t("assistantEval.col.status")}</th>
                      </tr>
                    </thead>
                    <tbody>
                      {recommendations.map((r, i) => (
                        <tr key={r?.index ?? i} data-testid={`eval-recommendation-${r?.index ?? i}`}>
                          <td className="mono">{Number(r?.index ?? i) + 1}</td>
                          <td>{String(r?.text ?? "")}</td>
                          <td className="mono">{asArray<string>(r?.mapped_to).join(", ") || "—"}</td>
                          <td>
                            <Chip tone={r?.status === "mapped" ? "good" : r?.status === "declined" ? "muted" : "warn"}>
                              {t(`assistantEval.recStatus.${r?.status ?? "unresolved"}`)}
                            </Chip>
                            {r?.note && <div className="dim" style={{ fontSize: 11 }}>{r.note}</div>}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </Wrap>
              </div>
            )}

            {!operation && (
              <div className="row" style={{ gap: 8, marginTop: 12, alignItems: "center", flexWrap: "wrap" }}>
                <Btn
                  primary
                  disabled={!canCreate}
                  disabledReason={createReason}
                  data-testid="eval-create"
                  onClick={() => setConfirmPlan(current)}
                >
                  {t("assistantEval.create")}
                </Btn>
                {current.status === "draft" && summary && summary.unresolved_recommendations > 0 && (
                  <span className="dim" style={{ fontSize: 11 }}>
                    {t("assistantEval.unresolvedHint", { n: summary.unresolved_recommendations })}
                  </span>
                )}
              </div>
            )}
          </>
        )}

        {operation && (
          <OperationView
            operation={operation}
            pollError={pollError}
            canMaterialize={canMaterialize}
            busy={busy}
            onRefresh={() => {
              setPollFailures(0);
              load();
            }}
            onRetry={() => void run(() => api.assistantEvalOperationRetry(conversationId, operation.id, workspaceId))}
            onCleanup={() => setConfirmCleanup(operation)}
          />
        )}

        {history.length > 0 && (
          <div className="assist-section" data-testid="eval-operation-history">
            <h4>{t("assistantEval.history")}</h4>
            <ul className="mono" style={{ fontSize: 11 }}>
              {history.map((o) => (
                <li key={o.id} data-testid={`eval-history-${o.id}`}>
                  {t("assistantEval.historyLine", {
                    plan: o.plan_revision,
                    status: t(`assistantEval.opStatus.${o.status}`),
                    by: o.approved_by,
                    hash: o.plan_hash.slice(0, 12),
                  })}
                  {canMaterialize && o.status !== "cleaned" && o.status !== "running" && o.status !== "queued" && (
                    <>
                      {" "}
                      <Btn disabled={busy} data-testid={`eval-history-cleanup-${o.id}`} onClick={() => setConfirmCleanup(o)}>
                        {t("assistantEval.cleanup")}
                      </Btn>
                    </>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
      </Panel>

      {operation && operation.status === "succeeded" && (
        <AssistantNextSteps
          operation={operation}
          proposal={proposals.find((p) => p.revision === operation.proposal_revision) ?? null}
          deployed={deployed}
          index={index + 1}
        />
      )}

      <ConfirmDialog
        open={confirmPlan !== null}
        title={t("assistantEval.confirmTitle", { n: confirmPlan?.revision ?? 0 })}
        body={
          (state?.disclosure ?? "") +
          "\n\n" +
          t("assistantEval.confirmBody", {
            hash: confirmPlan?.content_hash.slice(0, 16) ?? "",
            cloud: confirmPlan?.summary?.cloud_evaluators ?? 0,
            lambdas: confirmPlan?.summary?.lambda_functions ?? 0,
            roles: (confirmPlan?.summary?.iam_roles ?? 0) + (confirmPlan?.summary?.role_grants ?? 0),
            workspace: workspaceId ?? "",
          })
        }
        confirmLabel={t("assistantEval.confirmCreate")}
        onConfirm={() => {
          const plan = confirmPlan;
          setConfirmPlan(null);
          if (plan && current && plan.id === current.id && plan.content_hash === current.content_hash)
            void materialize(plan);
          else onError(t("assistantEval.dialogStale"));
        }}
        onCancel={() => setConfirmPlan(null)}
      />
      <ConfirmDialog
        open={confirmCleanup !== null}
        title={t("assistantEval.cleanupTitle")}
        body={t("assistantEval.cleanupBody", { plan: confirmCleanup?.plan_revision ?? 0 })}
        confirmLabel={t("assistantEval.cleanupConfirm")}
        onConfirm={() => {
          const op = confirmCleanup;
          setConfirmCleanup(null);
          if (op) void run(() => api.assistantEvalOperationCleanup(conversationId, op.id, workspaceId));
        }}
        onCancel={() => setConfirmCleanup(null)}
      />
    </>
  );
}

function EvaluatorDefinition({ e }: { e: AssistantEvalPlanEvaluator }) {
  const { t } = useTranslation();
  const kind = String(e?.kind ?? "");
  return (
    <>
      <div>{String(e?.title ?? "")}</div>
      {kind === "existing" && <div className="mono">{String(e?.evaluator_id ?? "")}</div>}
      {kind === "judge" && (
        <details className="mono dim">
          <summary>
            {e?.name} · {e?.level} · {e?.model_id} · {t("assistantEval.fullRubric")}
          </summary>
          <pre className="assist-pre" data-testid={`eval-rubric-${e?.key}`}>{String(e?.instructions ?? "")}</pre>
          {asArray<{ value: number; label: string; definition: string }>(e?.rating_scale).length > 0 && (
            <ul>
              {asArray<{ value: number; label: string; definition: string }>(e?.rating_scale).map((r, k) => (
                <li key={k}>
                  {r.value} · {r.label} — {r.definition}
                </li>
              ))}
            </ul>
          )}
        </details>
      )}
      {kind === "derived" && (
        <div className="mono dim">{e?.name} · {e?.base_evaluator_id} · {e?.model_id}</div>
      )}
      {kind === "code" && (
        <details className="mono dim">
          <summary>
            {e?.name} · {e?.level} · {t("assistantEval.rulesCount", { n: asArray(e?.rules?.checks).length })}
          </summary>
          <pre className="assist-pre">
            {asArray<Record<string, unknown>>(e?.rules?.checks).map((c) => compactRule(c)).join("\n")}
          </pre>
        </details>
      )}
      {e?.note && <div className="dim">{e.note}</div>}
    </>
  );
}

function OperationView({
  operation,
  pollError,
  canMaterialize,
  busy,
  onRefresh,
  onRetry,
  onCleanup,
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
    <div className="assist-section" data-testid="eval-operation" data-status={operation.status}>
      <h4>
        {t("assistantEval.operation")}{" "}
        <Chip tone={OP_TONE[operation.status] ?? "muted"}>
          {t(`assistantEval.opStatus.${operation.status}`)}
        </Chip>
      </h4>
      <div className="dim mono" style={{ fontSize: 11 }}>
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
      {operation.error && (
        <div className="mono" style={{ color: "var(--crit)", fontSize: 11, marginTop: 4, whiteSpace: "pre-wrap" }} data-testid="eval-operation-error">
          {operation.error}
        </div>
      )}
      {pollError && (
        <div className="note" style={{ borderColor: "var(--crit)", marginTop: 6 }} data-testid="eval-poll-error">
          <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
          <span>
            {t("assistantEval.pollError", { message: pollError })}{" "}
            <Btn data-testid="eval-refresh" onClick={onRefresh}>{t("assistantEval.refresh")}</Btn>
          </span>
        </div>
      )}
      {live && !pollError && (
        <div className="row" style={{ marginTop: 6 }}>
          <Btn data-testid="eval-refresh" onClick={onRefresh}>{t("assistantEval.refresh")}</Btn>
        </div>
      )}
      <Wrap testid="eval-resources-table">
        <table className="assist-gt" style={{ marginTop: 8 }}>
          <thead>
            <tr>
              <th>{t("assistantEval.col.resource")}</th>
              <th>{t("assistantEval.col.name")}</th>
              <th>{t("assistantEval.col.status")}</th>
              <th>{t("assistantEval.col.details")}</th>
            </tr>
          </thead>
          <tbody>
            {asArray<AssistantEvalResource>(operation.resources).map((r) => (
              <tr key={r.key} data-testid={`eval-resource-${r.key}`} data-status={r.status}>
                <td className="mono">{t(`assistantEval.resource.${r.kind}`, r.kind)}</td>
                <td className="mono" style={{ wordBreak: "break-all" }}>{r.name}</td>
                <td><ResourceStatus res={r} /></td>
                <td className="mono" style={{ fontSize: 11, wordBreak: "break-all" }}>
                  <ResourceDetails res={r} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Wrap>
      <div className="note" style={{ marginTop: 8 }} data-testid="eval-registered-note">
        <span className="i">[i]</span>
        <span>{t("assistantEval.registeredNote")}</span>
      </div>
      {canMaterialize && (
        <div className="row" style={{ gap: 8, marginTop: 8 }}>
          {(operation.status === "partial" || operation.status === "failed") &&
            operation.attempts < operation.max_attempts && (
              <Btn disabled={busy} data-testid="eval-retry" onClick={onRetry}>
                {t("assistantEval.retry")}
              </Btn>
            )}
          {operation.status !== "cleaned" && !live && operation.status !== "cleaning" && (
            <Btn disabled={busy} data-testid="eval-cleanup" onClick={onCleanup}>
              {t("assistantEval.cleanup")}
            </Btn>
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
      <Chip tone={RES_TONE[res.status] ?? "muted"}>
        {t(`assistantEval.resStatus.${res.status}`, res.status)}
      </Chip>
      {res.recovered && <Chip tone="muted">{t("assistantEval.recovered")}</Chip>}
      {res.error && (
        <div className="mono" style={{ color: "var(--crit)", fontSize: 11, whiteSpace: "pre-wrap" }}>
          {res.error}
        </div>
      )}
    </>
  );
}

function ResourceDetails({ res }: { res: AssistantEvalResource }) {
  const { t } = useTranslation();
  const r = asRecord(res.result);
  const readback = asRecord(r.readback);
  return (
    <>
      {res.link && (
        <div>
          <Link
            to={res.link}
            className="mono"
            style={{ color: "var(--amber)", textDecoration: "none" }}
            data-testid={`eval-link-${res.key}`}
          >
            {t("assistantEval.open")} ▸
          </Link>
        </div>
      )}
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
      {res.kind === "log_group" && r.retention_days ? (
        <div>{t("assistantEval.retention", { n: Number(r.retention_days) })}</div>
      ) : null}
      {res.cleanup && res.cleanup.note ? <div className="dim">{res.cleanup.note}</div> : null}
    </>
  );
}
