import { Check } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import {
  api,
  type PolicyDetail,
  type PolicyEnforcementMode,
  type PolicyEngine,
  type PolicyEntities,
  type PolicyEntityTool,
  type PolicyValidationReport,
  type PolicyVersion,
} from "../../../lib/api";
import {
  buildAllowlistStatement,
  buildForbidToolForMembers,
  buildPermitUsersCallTools,
  buildPermitWorkspaceAgentsCallTools,
  governanceError,
  policyValidationError,
  isToolPattern,
  POLICY_NAME_RE,
  POLICY_PREFILLS,
  type PolicyPrefill,
  serverTemplate,
  toolPolicyPrefill,
} from "../../../lib/governance";
import { useWorkspace } from "../../../workspace/workspace-context";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Descriptions, Field, FlowHeader, LinkButton, Modal, OptionCard, Spin, Tag } from "../../ui";
import { engineHref } from "./common";
import { PolicyEnforcementTag, PolicyStatusTag, ValidationReportView } from "./widgets";

type ConfirmAction = "save" | "publish" | "retire" | "delete";

/** Selected tool names → catalog rows, so a pattern row (`crm___*`, `pattern: true`) reaches
 *  the builders with its flag and becomes `resource.name like`, never an entity literal. */
function catalogTools(names: string[], catalog: PolicyEntityTool[]): (PolicyEntityTool | string)[] {
  return names.map((name) => catalog.find((tool) => tool.name === name) ?? name);
}

const isPatternRow = (tool: PolicyEntityTool) => tool.pattern ?? isToolPattern(tool.name);

/** The Cedar text the editor starts from for an existing policy: the draft when one
 *  exists, else the ACTIVE version, else whatever `_policy_out` carried. */
function initialStatement(detail: PolicyDetail): string {
  const by = (n: number | null) => detail.versions.find((v) => v.version === n)?.statement;
  return by(detail.draft_version) ?? by(detail.active_version) ?? detail.statement ?? "";
}

/** A temporal example in Global's vocabulary (the HR demo's "a payout needs an approval
 *  within the hour"). */
export const TEMPORAL_EXAMPLE = `forbid (principal, action == AgentCore::Action::"hr-database___create_payout", resource)
unless temporal {
  formerly within 1h AgentCore::Action::"hr-approvals___approve_payout"::response{
    input.employee_id: context.input.employee_id, output.approved: true }
};`;

/**
 * Cedar policy editor of one 策略引擎 (Global's policy editor re-targeted at the self-hosted
 * engine): a new policy is a draft v1; saving an existing one creates a new draft version
 * while the ACTIVE version keeps serving (Global's "ACTIVE edits create a candidate");
 * PUBLISH promotes the draft with a plain confirmation (no workspace id, no reason —
 * N17-obs505/506). VALIDATE / FORMAT call the engine, so a typo is a line:col finding, not a
 * silent default-deny. `target` prefills a tool allowlist for one gateway target.
 */
export function PolicyEditor({
  engineId,
  policyId,
  target = null,
}: {
  engineId: string;
  policyId: string | null;
  target?: string | null;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const { can, isAdmin } = useAuth();
  const { current } = useWorkspace();
  const manage = can("policy.manage");
  const [engine, setEngine] = useState<PolicyEngine | null>(null);
  const [detail, setDetail] = useState<PolicyDetail | null>(null);
  const [entities, setEntities] = useState<PolicyEntities | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [name, setName] = useState("launchpad_policy");
  const [description, setDescription] = useState("");
  const [prefill, setPrefill] = useState<PolicyPrefill>("custom");
  const [selectedTools, setSelectedTools] = useState<string[]>([]);
  const [statement, setStatement] = useState("");
  const [validation, setValidation] = useState<PolicyValidationReport | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  const [busy, setBusy] = useState<string | null>(null);
  const [prefilled, setPrefilled] = useState(false);

  const load = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    const [engineResult, detailResult] = await Promise.allSettled([
      api.getPolicyEngine(engineId),
      policyId ? api.getPolicy(engineId, policyId) : Promise.resolve(null),
    ]);
    const loadedEngine = engineResult.status === "fulfilled" ? engineResult.value : null;
    if (loadedEngine) setEngine(loadedEngine);
    // the tool catalog of the engine's first attached gateway (every attached gateway's tools
    // when the engine is attached to none / the server decides)
    const [entitiesResult] = await Promise.allSettled([api.policyEntities(engineId, loadedEngine?.attachments[0]?.gateway_id ?? null)]);
    if (detailResult.status === "rejected") {
      setError(governanceError(detailResult.reason));
      setRefreshing(false);
      return;
    }
    if (detailResult.value) {
      const d = detailResult.value;
      setDetail(d);
      setName(d.name);
      setDescription(d.description ?? "");
      setStatement(initialStatement(d));
      setValidation(d.versions.find((v) => v.version === (d.draft_version ?? d.active_version))?.validation ?? d.validation ?? null);
    }
    if (entitiesResult.status === "fulfilled") {
      setEntities(entitiesResult.value);
      if (target && !policyId && !prefilled) {
        // once per mount: a reload must not undo the operator's edits
        setPrefilled(true);
        const pre = toolPolicyPrefill(entitiesResult.value.tools, target);
        setName(pre.name);
        setPrefill("permit_agent_declared_tools");
        setSelectedTools(pre.tools);
        if (pre.tools.length) setStatement(buildAllowlistStatement(catalogTools(pre.tools, entitiesResult.value.tools)));
      }
    } else if (!policyId) {
      setError(governanceError(entitiesResult.reason));
    }
    setRefreshing(false);
  }, [engineId, policyId, prefilled, target]);

  useEffect(() => {
    void load();
  }, [load]);

  const existing = detail;
  const tools = entities?.tools ?? [];
  const nameValid = POLICY_NAME_RE.test(name);
  const draftVersion = existing?.draft_version ?? null;
  const activeVersion = existing?.active_version ?? null;
  const activeStatement = useMemo(
    () => existing?.versions.find((v) => v.version === activeVersion)?.statement ?? null,
    [activeVersion, existing],
  );
  const dirty = existing ? statement !== initialStatement(existing) || description !== (existing.description ?? "") : true;

  const saveBlockers: string[] = [];
  if (!manage) saveBlockers.push("noPermission");
  if (!nameValid) saveBlockers.push("nameInvalid");
  if (statement.trim().length === 0) saveBlockers.push("noStatement");
  if (validation && !validation.ok) saveBlockers.push("validationFailed");
  const saveReady = saveBlockers.length === 0 && busy === null;
  const publishable = !!existing && draftVersion != null && existing.status !== "retired";
  const publishReady = publishable && manage && busy === null;

  // Global's per-policy enforcementMode — `log_only` records what it would deny without
  // denying; an ACTIVE policy is re-installed at once (a refusal rolls back server-side, 409)
  const changeEnforcement = async (mode: PolicyEnforcementMode) => {
    if (!existing) return;
    setBusy("enforcement");
    try {
      await api.setPolicyEnforcementMode(engineId, existing.id, mode);
      toast("success", t("v2.governance.policy.enforcementToggle.saved"));
      await load();
    } catch (err) {
      toast("error", governanceError(err));
    } finally {
      setBusy(null);
    }
  };

  const toggleTool = (tool: string) =>
    setSelectedTools((cur) => (cur.includes(tool) ? cur.filter((x) => x !== tool) : [...cur, tool]));

  const applyTemplate = () => {
    // the two call_tool baselines — Stage A (this workspace's agents may call their declared
    // tools) and Stage BT (members / admins / system) — plus the allowlist and forbid templates
    if (prefill === "permit_workspace_agents_call_tools")
      setStatement(buildPermitWorkspaceAgentsCallTools(current?.id ?? "", serverTemplate(entities, "permit_workspace_agents_call_tools")));
    else if (prefill === "permit_users_call_tools") setStatement(buildPermitUsersCallTools(serverTemplate(entities, "permit_users_call_tools")));
    else if (prefill === "permit_agent_declared_tools" && selectedTools.length)
      setStatement(buildAllowlistStatement(catalogTools(selectedTools, tools)));
    else if (prefill === "forbid_tool_for_members" && selectedTools.length === 1)
      setStatement(buildForbidToolForMembers(catalogTools(selectedTools, tools)[0]));
    setValidation(null);
  };

  const validate = async () => {
    setBusy("validate");
    try {
      setValidation(await api.validatePolicy(engineId, statement, existing?.id));
    } catch (err) {
      setValidation(policyValidationError(err));
      toast("error", governanceError(err));
    } finally {
      setBusy(null);
    }
  };

  const format = async () => {
    setBusy("format");
    try {
      const formatted = await api.formatPolicy(engineId, statement);
      setStatement(formatted.statement);
      toast("success", t("v2.governance.policy.messages.formatted"));
    } catch (err) {
      toast("error", governanceError(err));
    } finally {
      setBusy(null);
    }
  };

  const back = () => navigate(engineHref(engineId));

  const submitConfirmed = async () => {
    if (!confirmAction) return;
    setBusy(confirmAction);
    try {
      if (confirmAction === "save") {
        const saved = existing
          ? await api.updatePolicy(engineId, existing.id, { statement, description: description || null })
          : await api.createPolicy(engineId, {
              name,
              statement,
              description: description || null,
              prefill: prefill === "custom" ? null : prefill,
            });
        toast("success", t(existing ? "v2.governance.policy.messages.draftSaved" : "v2.governance.policy.messages.created", { name: saved.name }));
        if (!existing) {
          navigate(`/v2/policy?view=policy&engine=${encodeURIComponent(engineId)}&policy=${encodeURIComponent(saved.id)}`);
          return;
        }
      } else if (confirmAction === "publish" && existing) {
        await api.publishPolicy(engineId, existing.id, { version: draftVersion ?? undefined });
        toast("success", t("v2.governance.policy.messages.publish", { name: existing.name }));
      } else if (confirmAction === "retire" && existing) {
        await api.retirePolicy(engineId, existing.id);
        toast("success", t("v2.governance.policy.messages.retire", { name: existing.name }));
      } else if (confirmAction === "delete" && existing) {
        await api.deletePolicy(engineId, existing.id);
        toast("success", t("v2.governance.policy.messages.delete", { name: existing.name }));
        back();
        return;
      }
      await load();
    } catch (err) {
      const report = policyValidationError(err);
      if (report) setValidation(report);
      toast("error", governanceError(err));
    } finally {
      setBusy(null);
      setConfirmAction(null);
    }
  };

  const title = existing ? existing.name : policyId ?? t("v2.governance.policy.newTitle");

  if (error && !existing && (policyId || !entities)) {
    return (
      <>
        <FlowHeader title={title} onBack={back} />
        <Alert tone="error" action={<LinkButton onClick={() => void load()}>{t("v2.common.retry")}</LinkButton>}>
          {error}
        </Alert>
      </>
    );
  }
  if (refreshing && !existing && !entities) {
    return (
      <>
        <FlowHeader title={title} onBack={back} />
        <Spin />
      </>
    );
  }

  const attached = engine?.attachments.length ?? 0;
  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {title}
            {existing ? <PolicyStatusTag status={existing.status} /> : <Tag tone="orange">{t("v2.governance.policy.status.draft")}</Tag>}
            {existing && activeVersion != null && <Tag tone="green">v{activeVersion}</Tag>}
            {existing && draftVersion != null && <Tag tone="orange">{t("v2.governance.policy.draftVersion", { version: draftVersion })}</Tag>}
          </span>
        }
        onBack={back}
        end={
          <>
            <Button disabled={refreshing} onClick={() => void load()}>
              {t("v2.common.refresh")}
            </Button>
            <Button kind="primary" disabled={!saveReady || (!!existing && !dirty)} onClick={() => setConfirmAction("save")} testId="v2-policy-save">
              {existing ? t("v2.governance.policy.saveDraft") : t("v2.governance.policy.create")}
            </Button>
          </>
        }
      />

      {error && <Alert tone="error">{error}</Alert>}
      {target && !existing && (
        <Alert>
          <span data-testid="v2-policy-tool-prefill">
            {t(selectedTools.length ? "v2.governance.policy.toolPrefill" : "v2.governance.policy.toolPrefillNone", { target })}
          </span>
        </Alert>
      )}
      {existing?.status === "active" && <Alert tone="warn">{t("v2.governance.policy.activeCreatesDraft")}</Alert>}
      {existing?.status === "retired" && <Alert tone="warn">{t("v2.governance.policy.retiredNote")}</Alert>}
      {saveBlockers.length > 0 && (
        <Alert tone="warn">
          <span data-testid="save-blockers">
            {t("v2.governance.policy.blockedPrefix")} {saveBlockers.map((key) => t(`v2.governance.policy.blockers.${key}`)).join(" · ")}
          </span>
        </Alert>
      )}

      <div className="v2-governance-editor">
        <div className="v2-governance-editor-main">
          <Card title={t("v2.governance.policy.definition")} testId="v2-policy-definition">
            {!existing && (
              <Alert>
                <b>{t("v2.governance.policy.primerTitle")}</b> {t("v2.governance.policy.primerBody")}
              </Alert>
            )}
            <div className="v2-form cols-2">
              <Field
                label={t("v2.governance.policy.colName")}
                required
                error={!nameValid ? t("v2.governance.policy.invalidName") : null}
                hint={existing ? t("v2.governance.policy.nameLocked") : undefined}
              >
                <input className="v2-input mono" value={name} disabled={!!existing} onChange={(e) => setName(e.target.value)} data-testid="v2-policy-name" />
              </Field>
              <Field label={t("v2.governance.description")}>
                <input className="v2-input" value={description} onChange={(e) => setDescription(e.target.value)} />
              </Field>

              <Field label={t("v2.governance.policy.prefill")} full hint={t(`v2.governance.policy.prefillHelp.${prefill}`)}>
                <div className="v2-options">
                  {POLICY_PREFILLS.map((option) => (
                    <OptionCard
                      key={option}
                      title={t(`v2.governance.policy.prefills.${option}`)}
                      desc={t(`v2.governance.policy.prefillDesc.${option}`)}
                      on={prefill === option}
                      onClick={() => setPrefill(option)}
                      testId={`v2-policy-prefill-${option}`}
                    />
                  ))}
                </div>
              </Field>

              {(prefill === "permit_agent_declared_tools" || prefill === "forbid_tool_for_members") && (
                <Field label={t("v2.governance.policy.tools")} full hint={t("v2.governance.policy.toolsHint")}>
                  <div className="v2-tags">
                    {tools.map((tool) => {
                      const on = selectedTools.includes(tool.name);
                      return (
                        <button
                          key={tool.name}
                          type="button"
                          className={on ? "v2-governance-chip on" : "v2-governance-chip"}
                          aria-pressed={on}
                          onClick={() => toggleTool(tool.name)}
                          title={isPatternRow(tool) ? t("v2.governance.policy.toolPatternHint") : tool.description || undefined}
                          data-testid={`v2-policy-tool-${tool.name}`}
                        >
                          {on && <Check size={12} aria-hidden="true" />}
                          <span className="mono">{tool.name}</span>
                          <small>{tool.target}</small>
                          {isPatternRow(tool) && <Tag tone="outline">{t("v2.governance.policy.toolPattern")}</Tag>}
                        </button>
                      );
                    })}
                    {tools.length === 0 && <span className="v2-muted">{t("v2.governance.policy.noTools")}</span>}
                  </div>
                </Field>
              )}
            </div>

            {prefill !== "custom" && (
              <div className="v2-row v2-governance-actions">
                <Button
                  kind="soft"
                  disabled={
                    prefill === "permit_agent_declared_tools"
                      ? selectedTools.length === 0
                      : prefill === "forbid_tool_for_members"
                        ? selectedTools.length !== 1
                        : false
                  }
                  onClick={applyTemplate}
                  testId="v2-policy-build-draft"
                >
                  {t("v2.governance.policy.buildDraft")}
                </Button>
              </div>
            )}

            <div className="v2-field" style={{ marginTop: 18 }}>
              <div className="v2-row">
                <label>{t("v2.policy.cedarStatement")}</label>
                <Button
                  size="sm"
                  kind="soft"
                  onClick={() => {
                    setStatement(TEMPORAL_EXAMPLE);
                    setValidation(null);
                  }}
                  testId="v2-policy-temporal-example"
                >
                  {t("v2.governance.policy.dialect.insertTemporal")}
                </Button>
              </div>
              <textarea
                className="v2-textarea code"
                rows={14}
                value={statement}
                spellCheck={false}
                onChange={(e) => {
                  setStatement(e.target.value);
                  setValidation(null);
                }}
                data-testid="v2-policy-cedar"
              />
            </div>
            <div className="v2-row v2-governance-actions">
              <Button disabled={busy !== null || !statement.trim()} onClick={() => void validate()} testId="v2-policy-validate">
                {busy === "validate" ? t("v2.governance.policy.validating") : t("v2.governance.policy.validate")}
              </Button>
              <Button disabled={busy !== null || !statement.trim()} onClick={() => void format()} testId="v2-policy-format">
                {t("v2.governance.policy.format")}
              </Button>
            </div>
            {/\btemporal\b/.test(statement) && (
              <div style={{ marginTop: 12 }}>
                <Alert tone="warn">
                  <span data-testid="v2-policy-temporal-note">{t("v2.governance.policy.dialect.temporalNote")}</span>
                </Alert>
              </div>
            )}
            <div style={{ marginTop: 12 }}>
              <ValidationReportView report={validation} />
            </div>
          </Card>

          <Card title={t("v2.governance.policy.review")} sub={t("v2.governance.policy.activeVsDraft")}>
            <div className="v2-grid-2">
              <div>
                <div className="v2-sub-title" style={{ marginTop: 0 }}>
                  {t("v2.governance.policy.activeVersion")} {activeVersion != null && <span className="mono v2-muted">v{activeVersion}</span>}
                </div>
                <pre className="v2-pre">{activeStatement ?? t("v2.governance.policy.noActiveVersion")}</pre>
              </div>
              <div>
                <div className="v2-sub-title" style={{ marginTop: 0 }}>
                  {t("v2.governance.policy.draft")}
                </div>
                <pre className="v2-pre">{statement || t("v2.governance.policy.emptyDraft")}</pre>
              </div>
            </div>
          </Card>
        </div>

        <aside className="v2-governance-editor-side">
          <Card title={t("v2.governance.policy.rollout")} testId="v2-policy-rollout">
            <Descriptions
              one
              items={[
                { label: t("v2.policy.engine"), value: <span className="mono">{engine?.name ?? engineId}</span> },
                {
                  label: t("v2.policy.colAttached"),
                  value: attached > 0 ? t("v2.policy.attachedCount", { count: attached }) : <span className="v2-muted">{t("v2.policy.notAttached")}</span>,
                },
                { label: t("v2.governance.colStatus"), value: <PolicyStatusTag status={existing?.status ?? "draft"} /> },
                ...(existing ? [{ label: t("v2.policy.colEnforcement"), value: <PolicyEnforcementTag mode={existing.enforcement_mode} testId="v2-policy-enforcement-tag" /> }] : []),
                { label: t("v2.governance.policy.activeVersion"), value: activeVersion != null ? `v${activeVersion}` : "—" },
                { label: t("v2.governance.policy.draft"), value: draftVersion != null ? `v${draftVersion}` : "—" },
              ]}
            />
            <div className="v2-muted" style={{ fontSize: 12.5, margin: "12px 0" }}>
              {t("v2.policy.rolloutHelp")}
            </div>
            {existing && (
              <div className="v2-row v2-governance-actions">
                <Button kind="primary" disabled={!publishReady} onClick={() => setConfirmAction("publish")} testId="v2-policy-publish">
                  {t("v2.governance.policy.publish")}
                </Button>
                {existing.status === "active" && (
                  <Button kind="danger" disabled={!manage || busy !== null} onClick={() => setConfirmAction("retire")} testId="v2-policy-retire">
                    {t("v2.governance.policy.retire")}
                  </Button>
                )}
                {existing.status !== "active" && (
                  <Button kind="danger" disabled={!manage || busy !== null} onClick={() => setConfirmAction("delete")} testId="v2-policy-delete">
                    {t("v2.common.delete")}
                  </Button>
                )}
              </div>
            )}
            {existing && !publishable && existing.status !== "retired" && (
              <div className="v2-governance-blockers" style={{ marginTop: 8 }}>
                {t("v2.governance.policy.noDraftToPublish")}
              </div>
            )}
            {existing && existing.status !== "retired" && (
              <div className="v2-stack" style={{ marginTop: 12 }} data-testid="v2-policy-enforcement">
                <label className="v2-check">
                  <input
                    type="checkbox"
                    checked={existing.enforcement_mode === "log_only"}
                    disabled={!isAdmin || busy !== null}
                    onChange={(e) => void changeEnforcement(e.target.checked ? "log_only" : "active")}
                    data-testid="v2-policy-enforcement-log-only"
                  />
                  {t("v2.governance.policy.enforcementToggle.label")}
                </label>
                <div className="v2-muted" style={{ fontSize: 12.5 }}>
                  {t(isAdmin ? "v2.governance.policy.enforcementToggle.help" : "v2.policy.attach.adminOnly")}
                </div>
              </div>
            )}
            <div className="v2-row v2-governance-actions">
              <Button size="sm" onClick={() => navigate(engineHref(engineId, "simulate"))}>
                {t("v2.governance.policy.sim.title")}
              </Button>
            </div>
          </Card>

          {existing && (
            <Card title={t("v2.governance.policy.versions")} testId="v2-policy-versions">
              <div className="v2-policy-versions">
                {[...existing.versions]
                  .sort((a, b) => b.version - a.version)
                  .map((v: PolicyVersion) => (
                    <div key={v.id} className="v2-row">
                      <span className="mono">v{v.version}</span>
                      {v.version === activeVersion ? (
                        <Tag tone="green">{t("v2.governance.policy.status.active")}</Tag>
                      ) : v.published_at ? (
                        <Tag tone="gray">{t("v2.governance.policy.published")}</Tag>
                      ) : (
                        <Tag tone="orange">{t("v2.governance.policy.status.draft")}</Tag>
                      )}
                      <span className="v2-muted">{fmtTime(v.published_at ?? v.created_at)}</span>
                      <span className="mono v2-muted">{v.authored_by}</span>
                      <LinkButton onClick={() => setStatement(v.statement)}>{t("v2.governance.policy.loadVersion")}</LinkButton>
                    </div>
                  ))}
              </div>
            </Card>
          )}
        </aside>
      </div>

      <Modal
        open={confirmAction !== null}
        title={t(`v2.governance.policy.confirmTitle.${confirmAction ?? "save"}`)}
        onClose={() => setConfirmAction(null)}
        testId="v2-policy-confirm"
        footer={
          <>
            <Button onClick={() => setConfirmAction(null)}>{t("v2.common.cancel")}</Button>
            <Button
              kind={confirmAction === "retire" || confirmAction === "delete" ? "danger" : "primary"}
              disabled={busy !== null || (confirmAction === "publish" && !publishReady)}
              onClick={() => void submitConfirmed()}
              testId="v2-policy-confirm-ok"
            >
              {t("v2.common.confirm")}
            </Button>
          </>
        }
      >
        <div className="v2-stack">
          <div data-testid="v2-policy-confirm-body">
            {confirmAction === "save"
              ? existing
                ? existing.status === "active"
                  ? t("v2.governance.policy.confirmBody.activeDraft", { name: existing.name })
                  : t("v2.governance.policy.confirmBody.save", { name })
                : t("v2.governance.policy.confirmBody.createDraft", { name })
              : confirmAction === "publish"
                ? t("v2.governance.policy.confirmBody.publish", { name: existing?.name, version: draftVersion })
                : confirmAction === "retire"
                  ? t("v2.governance.policy.confirmBody.retire", { name: existing?.name })
                  : t("v2.governance.policy.confirmBody.delete", { name: existing?.name })}
          </div>
          {confirmAction === "publish" && (
            // Global parity: what a publish does depends on the gateway attachments' modes
            <Alert>{attached > 0 ? t("v2.policy.publishAttachedNote", { count: attached }) : t("v2.policy.publishUnattachedNote")}</Alert>
          )}
        </div>
      </Modal>
    </>
  );
}
