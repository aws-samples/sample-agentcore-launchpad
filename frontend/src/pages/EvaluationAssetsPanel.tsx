import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Btn, Chip, ConfirmDialog, Panel } from "../components";
import type { ChipTone } from "../components";
import type {
  AssistantEvalOperation,
  AssistantEvalPlan,
  AssistantEvalPlanContent,
  AssistantEvalPlanState,
  AssistantEvalResource,
  AssistantProposal,
} from "../lib/api";
import { api } from "../lib/api";

/**
 * SE-047 — the reviewed evaluation-assets plan of one assistant conversation.
 *
 * Prepare (platform draft from a proposal revision) → review the structured mapping
 * (golden tests → scenarios, recommendations → evaluator kinds, human/runner
 * obligations) → edit as JSON (a new plan revision) → an administrator who owns the
 * conversation confirms the disclosure and CREATES the assets (local Dataset, AgentCore
 * evaluators, one Lambda + role for code rules). Creation is separate from testing:
 * nothing here runs an evaluation, deploys an agent, syncs to AWS Datasets or invokes
 * a model. Status is a ledger read; retry/cleanup are explicit administrator actions.
 */

const POLL_MS = 3000;

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
  skipped: "muted",
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

/** A rule without its empty / default members (what a reviewer needs to read). */
function compactRule(rule: Record<string, unknown>): string {
  const kept = Object.fromEntries(
    Object.entries(rule).filter(
      ([, v]) => !(v === null || v === "" || v === false || (Array.isArray(v) && v.length === 0)),
    ),
  );
  return JSON.stringify(kept);
}

function short(text: unknown, n = 90): string {
  const s = String(text ?? "");
  return s.length > n ? `${s.slice(0, n - 1)}…` : s;
}

export function EvaluationAssetsPanel({
  conversationId,
  proposals,
  canMaterialize,
  workspaceId,
  apiMessage,
  onError,
  index,
}: {
  conversationId: string;
  proposals: AssistantProposal[];
  canMaterialize: boolean;
  workspaceId: string | null;
  apiMessage: (err: unknown) => string;
  onError: (message: string) => void;
  index: number;
}) {
  const { t } = useTranslation();
  const [state, setState] = useState<AssistantEvalPlanState | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [sourceRevision, setSourceRevision] = useState<number>(
    proposals.length ? proposals[proposals.length - 1].revision : 1,
  );
  const [jsonDraft, setJsonDraft] = useState<string | null>(null);
  const [jsonError, setJsonError] = useState<string | null>(null);
  const [confirmPlan, setConfirmPlan] = useState<AssistantEvalPlan | null>(null);
  const [confirmCleanup, setConfirmCleanup] = useState<AssistantEvalOperation | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);

  // staleness: a response for another conversation / workspace never lands here
  const key = useRef(`${workspaceId}:${conversationId}`);
  key.current = `${workspaceId}:${conversationId}`;
  const stillCurrent = useCallback(
    (started: string) => key.current === started,
    [],
  );

  const load = useCallback(() => {
    const started = key.current;
    setLoadError(null);
    void api
      .assistantEvalPlan(conversationId)
      .then((res) => {
        if (stillCurrent(started)) setState(res);
      })
      .catch((err) => {
        if (stillCurrent(started)) setLoadError(apiMessage(err));
      });
  }, [conversationId, apiMessage, stillCurrent]);

  useEffect(() => {
    setState(null);
    setJsonDraft(null);
    setJsonError(null);
    setActionError(null);
    setConfirmPlan(null);
    setConfirmCleanup(null);
    load();
  }, [conversationId, workspaceId, load]);

  const plans = state?.plans ?? [];
  const current = plans.length ? plans[plans.length - 1] : null;
  const operations = state?.operations ?? [];
  const operation =
    operations.find((o) => o.plan_id === current?.id) ??
    (operations.length ? operations[operations.length - 1] : null);

  // poll a live operation (ledger read only)
  useEffect(() => {
    if (!operation || !(operation.status === "queued" || operation.status === "running")) return;
    const started = key.current;
    const opId = operation.id;
    const timer = window.setTimeout(() => {
      void api
        .assistantEvalOperation(conversationId, opId)
        .then((res) => {
          if (!stillCurrent(started)) return;
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
        .catch(() => undefined);
    }, POLL_MS);
    return () => window.clearTimeout(timer);
  }, [operation, conversationId, stillCurrent]);

  const run = useCallback(
    async (fn: () => Promise<AssistantEvalPlanState | { operation: AssistantEvalOperation }>) => {
      const started = key.current;
      setBusy(true);
      setActionError(null);
      try {
        const res = await fn();
        if (!stillCurrent(started)) return;
        if ("plans" in res) setState(res);
        else {
          // the plan's own status (draft → approved) changed too — refetch it
          load();
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
        }
      } catch (err) {
        if (!stillCurrent(started)) return;
        const message = apiMessage(err);
        setActionError(message);
        onError(message);
      } finally {
        if (stillCurrent(started)) setBusy(false);
      }
    },
    [apiMessage, onError, stillCurrent, load],
  );

  const prepare = () =>
    run(async () => {
      const res = await api.assistantEvalPlanPrepare(conversationId, sourceRevision);
      setJsonDraft(null);
      return res;
    });

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
    void run(async () => {
      const res = await api.assistantEvalPlanEdit(conversationId, parsed);
      setJsonDraft(null);
      return res;
    });
  };

  const materialize = (plan: AssistantEvalPlan) =>
    run(() =>
      api.assistantEvalPlanMaterialize(conversationId, plan.revision, plan.content_hash),
    );

  const content = (current?.content ?? {}) as Partial<AssistantEvalPlanContent>;
  const evaluators = content.evaluators ?? [];
  const recommendations = content.recommendations ?? [];
  const scenarios = content.scenarios ?? [];
  const blocked = content.blocked_golden_tests ?? [];
  const summary = current?.summary;
  const canCreate =
    !!current &&
    current.status === "draft" &&
    !current.validation_errors.length &&
    canMaterialize &&
    !busy &&
    !operation;
  const opResources = operation?.resources ?? [];

  return (
    <>
      <Panel
        brk
        title={t("assistantEval.title")}
        sub={t("assistantEval.sub")}
        data-testid="evaluation-assets"
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
            <span>{loadError}</span>
          </div>
        )}

        {/* prepare */}
        <div className="row" style={{ gap: 8, alignItems: "center", flexWrap: "wrap" }}>
          <label className="dim" style={{ fontSize: 11 }}>
            {t("assistantEval.sourceRevision")}
            <select
              className="mono"
              style={{ marginLeft: 6 }}
              value={sourceRevision}
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
          <Btn
            disabled={busy || !proposals.length}
            data-testid="eval-prepare"
            onClick={() => void prepare()}
          >
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
        </div>

        {!current && (
          <div className="empty" data-testid="eval-plan-empty" style={{ marginTop: 10 }}>
            {t("assistantEval.empty")}
          </div>
        )}

        {current && current.validation_errors.length > 0 && (
          <div
            className="note"
            style={{ borderColor: "var(--crit)", marginTop: 10 }}
            data-testid="eval-plan-invalid"
          >
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span>
              {t("assistantEval.invalidNote")}
              <ul style={{ margin: "6px 0 0", paddingLeft: 18 }}>
                {current.validation_errors.map((e, i) => (
                  <li key={i} className="mono" style={{ fontSize: 11 }}>{e}</li>
                ))}
              </ul>
            </span>
          </div>
        )}

        {/* JSON editor */}
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
              <Btn disabled={busy} data-testid="eval-json-save" onClick={saveJson}>
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

        {/* structured review */}
        {current && jsonDraft === null && (
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
            <div className="assist-section" data-testid="eval-scenarios">
              <h4>{t("assistantEval.scenarios")}</h4>
              <table className="assist-gt">
                <thead>
                  <tr>
                    <th>{t("assistantEval.col.goldenTest")}</th>
                    <th>{t("assistantEval.col.scenario")}</th>
                    <th>{t("assistantEval.col.turns")}</th>
                    <th>{t("assistantEval.col.procedure")}</th>
                  </tr>
                </thead>
                <tbody>
                  {scenarios.map((s) => (
                    <tr key={s.scenario_id} data-testid={`eval-scenario-${s.golden_test_id}`}>
                      <td className="mono">{s.golden_test_id}</td>
                      <td className="mono">{s.scenario_id}</td>
                      <td>{s.turns.length}</td>
                      <td className="mono">
                        {s.execution
                          ? t("assistantEval.procedureRunner")
                          : t("assistantEval.procedureSingle")}
                      </td>
                    </tr>
                  ))}
                  {blocked.map((b) => (
                    <tr key={`b-${b.golden_test_id}`} data-testid={`eval-blocked-${b.golden_test_id}`}>
                      <td className="mono">{b.golden_test_id}</td>
                      <td colSpan={3}>
                        <Chip tone="warn">{t("assistantEval.blocked")}</Chip> {b.reason}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
            <div className="assist-section" data-testid="eval-evaluators">
              <h4>{t("assistantEval.evaluators")}</h4>
              <table className="assist-gt">
                <thead>
                  <tr>
                    <th>{t("assistantEval.col.key")}</th>
                    <th>{t("assistantEval.col.kind")}</th>
                    <th>{t("assistantEval.col.definition")}</th>
                    <th>{t("assistantEval.col.goldenTests")}</th>
                    <th>{t("assistantEval.col.gate")}</th>
                    <th>{t("assistantEval.col.status")}</th>
                  </tr>
                </thead>
                <tbody>
                  {evaluators.map((e) => {
                    const res = opResources.find((r) => r.plan_key === e.key);
                    return (
                      <tr key={e.key} data-testid={`eval-evaluator-${e.key}`}>
                        <td className="mono">{e.key}</td>
                        <td>
                          <Chip tone={CLOUD_KINDS.has(e.kind) ? "good" : e.kind === "existing" ? "muted" : "warn"}>
                            {t(`assistantEval.kind.${e.kind}`)}
                          </Chip>
                          {e.draft && <Chip tone="warn">{t("assistantEval.draftRubric")}</Chip>}
                        </td>
                        <td style={{ fontSize: 11 }}>
                          <div>{e.title}</div>
                          {e.kind === "existing" && <div className="mono">{e.evaluator_id}</div>}
                          {e.kind === "judge" && (
                            <div className="mono dim">
                              {e.name} · {e.level} · {e.model_id}
                              <div style={{ whiteSpace: "pre-wrap" }}>{short(e.instructions, 240)}</div>
                            </div>
                          )}
                          {e.kind === "derived" && (
                            <div className="mono dim">{e.name} · {e.base_evaluator_id} · {e.model_id}</div>
                          )}
                          {e.kind === "code" && (
                            <div className="mono dim">
                              {e.name} · {e.level} · {t("assistantEval.rulesCount", { n: e.rules?.checks.length ?? 0 })}
                              <div style={{ whiteSpace: "pre-wrap" }}>
                                {(e.rules?.checks ?? []).map((c) => compactRule(c)).join("\n")}
                              </div>
                            </div>
                          )}
                          {!CLOUD_KINDS.has(e.kind) && e.kind !== "existing" && (
                            <div className="dim">
                              {e.reason}
                              {e.obligation ? ` — ${e.obligation}` : ""}
                            </div>
                          )}
                          {e.note && <div className="dim">{e.note}</div>}
                        </td>
                        <td className="mono">{e.golden_test_ids.join(", ") || "—"}</td>
                        <td className="mono">
                          {e.blocking ? t("assistantEval.blocking") : t("assistantEval.informational")}
                          {e.threshold !== null && e.threshold !== undefined ? ` · ≥ ${e.threshold}` : ""}
                        </td>
                        <td>
                          {res ? (
                            <ResourceStatus res={res} />
                          ) : CLOUD_KINDS.has(e.kind) ? (
                            <Chip tone="muted">{t("assistantEval.notCreated")}</Chip>
                          ) : e.kind === "existing" ? (
                            <Chip tone="muted">{t("assistantEval.existingRef")}</Chip>
                          ) : (
                            <Chip tone="warn">{t(`assistantEval.obligation.${e.kind}`)}</Chip>
                          )}
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
            <div className="assist-section" data-testid="eval-recommendations">
              <h4>{t("assistantEval.recommendations")}</h4>
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
                  {recommendations.map((r) => (
                    <tr key={r.index} data-testid={`eval-recommendation-${r.index}`}>
                      <td className="mono">{r.index + 1}</td>
                      <td>{r.text}</td>
                      <td className="mono">{r.mapped_to.join(", ") || "—"}</td>
                      <td>
                        <Chip tone={r.status === "mapped" ? "good" : r.status === "declined" ? "muted" : "warn"}>
                          {t(`assistantEval.recStatus.${r.status}`)}
                        </Chip>
                        {r.note && <div className="dim" style={{ fontSize: 11 }}>{r.note}</div>}
                      </td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>

            {/* create */}
            {!operation && (
              <div className="row" style={{ gap: 8, marginTop: 12, alignItems: "center", flexWrap: "wrap" }}>
                <Btn
                  primary
                  disabled={!canCreate}
                  disabledReason={
                    !canMaterialize
                      ? t("assistantEval.adminOnly")
                      : current.validation_errors.length
                        ? t("assistantEval.fixErrors")
                        : undefined
                  }
                  data-testid="eval-create"
                  onClick={() => setConfirmPlan(current)}
                >
                  {t("assistantEval.create")}
                </Btn>
                {!canMaterialize && (
                  <span className="dim" style={{ fontSize: 11 }} data-testid="eval-admin-only">
                    {t("assistantEval.adminOnly")}
                  </span>
                )}
                {current.status === "draft" && summary && summary.unresolved_recommendations > 0 && (
                  <span className="dim" style={{ fontSize: 11 }}>
                    {t("assistantEval.unresolvedHint", { n: summary.unresolved_recommendations })}
                  </span>
                )}
              </div>
            )}
          </>
        )}

        {actionError && (
          <div className="note" style={{ borderColor: "var(--crit)", marginTop: 10 }} data-testid="eval-action-error">
            <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
            <span>{actionError}</span>
          </div>
        )}

        {/* operation outcome */}
        {operation && (
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
              <div className="mono" style={{ color: "var(--crit)", fontSize: 11, marginTop: 4 }} data-testid="eval-operation-error">
                {operation.error}
              </div>
            )}
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
                {operation.resources.map((r) => (
                  <tr key={r.key} data-testid={`eval-resource-${r.key}`} data-status={r.status}>
                    <td className="mono">{t(`assistantEval.resource.${r.kind}`, r.kind)}</td>
                    <td className="mono">{r.name}</td>
                    <td><ResourceStatus res={r} /></td>
                    <td className="mono" style={{ fontSize: 11 }}>
                      <ResourceDetails res={r} />
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
            <div className="note" style={{ marginTop: 8 }} data-testid="eval-registered-note">
              <span className="i">[i]</span>
              <span>{t("assistantEval.registeredNote")}</span>
            </div>
            {canMaterialize && (
              <div className="row" style={{ gap: 8, marginTop: 8 }}>
                {(operation.status === "partial" || operation.status === "failed") &&
                  operation.attempts < operation.max_attempts && (
                    <Btn
                      disabled={busy}
                      data-testid="eval-retry"
                      onClick={() => void run(() => api.assistantEvalOperationRetry(conversationId, operation.id))}
                    >
                      {t("assistantEval.retry")}
                    </Btn>
                  )}
                {operation.status !== "cleaned" &&
                  operation.status !== "running" &&
                  operation.status !== "queued" && (
                    <Btn
                      disabled={busy}
                      data-testid="eval-cleanup"
                      onClick={() => setConfirmCleanup(operation)}
                    >
                      {t("assistantEval.cleanup")}
                    </Btn>
                  )}
              </div>
            )}
          </div>
        )}
      </Panel>

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
          })
        }
        confirmLabel={t("assistantEval.confirmCreate")}
        onConfirm={() => {
          const plan = confirmPlan;
          setConfirmPlan(null);
          if (plan && current && plan.id === current.id) void materialize(plan);
          else onError(t("assistantEval.dialogStale"));
        }}
        onCancel={() => setConfirmPlan(null)}
      />
      <ConfirmDialog
        open={confirmCleanup !== null}
        title={t("assistantEval.cleanupTitle")}
        body={t("assistantEval.cleanupBody")}
        confirmLabel={t("assistantEval.cleanupConfirm")}
        onConfirm={() => {
          const op = confirmCleanup;
          setConfirmCleanup(null);
          if (op) void run(() => api.assistantEvalOperationCleanup(conversationId, op.id));
        }}
        onCancel={() => setConfirmCleanup(null)}
      />
    </>
  );
}

function ResourceStatus({ res }: { res: AssistantEvalResource }) {
  const { t } = useTranslation();
  return (
    <>
      <Chip tone={RES_TONE[res.status] ?? "muted"}>
        {t(`assistantEval.resStatus.${res.status}`, res.status)}
      </Chip>
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
  const r = (res.result ?? {}) as Record<string, unknown>;
  const readback = (r.readback ?? null) as Record<string, unknown> | null;
  return (
    <>
      {res.link && (
        <div>
          <Link to={res.link} data-testid={`eval-link-${res.key}`}>
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
          {readback ? (
            <div>
              {String(readback.Runtime ?? "")} · {String(readback.Handler ?? "")} · v{String(readback.Version ?? "")}
            </div>
          ) : null}
        </div>
      )}
      {res.kind === "lambda_role" && r.role_arn ? <div>{String(r.role_arn)}</div> : null}
      {res.kind === "role_grant" && r.role_arn ? (
        <div>{String(r.role_arn)} · {String(r.policy_name ?? "")}</div>
      ) : null}
      {res.kind === "lambda_permission" && r.principal ? (
        <div>{String(r.principal)} · SourceAccount {String(r.source_account ?? "")}</div>
      ) : null}
      {res.kind === "log_group" && r.retention_days ? (
        <div>{t("assistantEval.retention", { n: Number(r.retention_days) })}</div>
      ) : null}
      {res.cleanup && res.cleanup.note ? <div className="dim">{res.cleanup.note}</div> : null}
    </>
  );
}
