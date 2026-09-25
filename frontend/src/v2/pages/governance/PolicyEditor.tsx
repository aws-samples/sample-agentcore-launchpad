import { Check, Plus, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  type GovernanceAuthorizationModel,
  type GovernanceDecisionResponse,
  type GovernanceGatewayDetail,
  type GovernanceGeneration,
  type GovernanceOperation,
  type GovernancePolicyListResponse,
} from "../../../lib/api";
import {
  buildAllowlistStatement,
  buildPreserveTrafficStatement,
  governanceError,
  isGatewayReady,
  POLICY_NAME_RE,
} from "../../../lib/governance";
import { useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  Confirm,
  Descriptions,
  Field,
  FlowHeader,
  LinkButton,
  OptionCard,
  Spin,
  Tag,
} from "../../ui";
import { useCopy, useGovernanceOperation } from "./common";
import { OperationAlert, SharedEngineAck, StatusTag } from "./widgets";

type ConfirmAction = "save" | "promote" | "rollback";
const MODELS: GovernanceAuthorizationModel[] = ["allowlist", "preserve_traffic", "custom"];
const GENERATION_POLL_MS = 3000;

/** Cedar policy editor: new LOG_ONLY policy (no `policy`) or review / save / promote / rollback. */
export function PolicyEditor({ gatewayId, policyId }: { gatewayId: string; policyId: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const copy = useCopy(t);
  const [gateway, setGateway] = useState<GovernanceGatewayDetail | null>(null);
  const [policyData, setPolicyData] = useState<GovernancePolicyListResponse | null>(null);
  const [evidence, setEvidence] = useState<GovernanceDecisionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [name, setName] = useState("launchpad_policy");
  const [description, setDescription] = useState("");
  const [model, setModel] = useState<GovernanceAuthorizationModel>("allowlist");
  const [highRiskAcknowledged, setHighRiskAcknowledged] = useState(false);
  const [selectedActions, setSelectedActions] = useState<string[]>([]);
  const [manualInput, setManualInput] = useState("");
  const [manualActions, setManualActions] = useState<string[]>([]);
  const [statement, setStatement] = useState("");
  const [naturalLanguage, setNaturalLanguage] = useState("");
  const [generation, setGeneration] = useState<GovernanceGeneration | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  const [confirmationName, setConfirmationName] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [sharedAcknowledged, setSharedAcknowledged] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    const [gatewayResult, policiesResult, evidenceResult] = await Promise.allSettled([
      api.getGovernanceGateway(gatewayId),
      api.listGovernancePolicies(gatewayId),
      api.governanceDecisions(gatewayId, "24h", policyId ?? undefined),
    ]);
    if (gatewayResult.status === "rejected") {
      setError(governanceError(gatewayResult.reason));
      setRefreshing(false);
      return;
    }
    setGateway(gatewayResult.value);
    if (policiesResult.status === "fulfilled") {
      setPolicyData(policiesResult.value);
      const selected = policiesResult.value.policies.find((policy) => policy.id === policyId);
      if (policyId && !selected) {
        setError(t("governance.policyEditor.policyNotFound"));
      } else if (selected) {
        setName(selected.name);
        setDescription(selected.description ?? "");
        setStatement(selected.statement);
      }
    } else {
      setError(governanceError(policiesResult.reason));
    }
    if (evidenceResult.status === "fulfilled") setEvidence(evidenceResult.value);
    setRefreshing(false);
  }, [gatewayId, policyId, t]);

  useEffect(() => {
    void load();
  }, [load]);

  const { operation, setOperation, pending } = useGovernanceOperation(() => void load());

  useEffect(() => {
    if (!generation) return;
    const settled = generation.status === "GENERATED" || generation.status.toUpperCase().includes("FAILED");
    if (settled) return;
    const timer = window.setTimeout(() => {
      api
        .getGovernanceGeneration(gatewayId, generation.id)
        .then(setGeneration)
        .catch((pollError: unknown) => toast("error", governanceError(pollError)));
    }, GENERATION_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [gatewayId, generation, toast]);

  const existingPolicy = useMemo(
    () => policyData?.policies.find((policy) => policy.id === policyId) ?? null,
    [policyData, policyId],
  );
  const allSelectedActions = useMemo(() => [...selectedActions, ...manualActions], [manualActions, selectedActions]);
  const evidenceCount = evidence?.decisions.length ?? 0;
  const needsSharedAck = (gateway?.shared_gateways.length ?? 0) > 1;
  const sharedReady = !needsSharedAck || sharedAcknowledged;
  const operationBusy = busy !== null || pending;
  const nameValid = POLICY_NAME_RE.test(name);
  const gatewayReady =
    !!gateway && gateway.managed && isGatewayReady(gateway) && !!gateway.policy_engine && !gateway.policy_engine.missing && sharedReady;

  // unmet preconditions, in the order an operator would fix them; rendered next to the
  // disabled buttons so a swallowed click never reads as "nothing happened"
  const gatewayBlockers: string[] = [];
  if (gateway && !gateway.managed) gatewayBlockers.push("notManaged");
  else if (gateway && !isGatewayReady(gateway)) gatewayBlockers.push("gatewayNotReady");
  // a dangling reference cannot back a policy either — recovery is on the Gateway detail page
  if (gateway && (!gateway.policy_engine || gateway.policy_engine.missing)) gatewayBlockers.push("noEngine");
  if (!sharedReady) gatewayBlockers.push("sharedAck");

  const saveBlockers = [...gatewayBlockers];
  if (!nameValid) saveBlockers.push("nameInvalid");
  if (statement.trim().length === 0) saveBlockers.push("noStatement");
  if (model === "preserve_traffic" && !highRiskAcknowledged) saveBlockers.push("highRiskAck");
  const saveReady = saveBlockers.length === 0 && !operationBusy;

  const overrideReady = confirmationName === gateway?.name && overrideReason.trim().length > 0;
  const transitionBlockers = [...gatewayBlockers];
  if (!existingPolicy) transitionBlockers.push("noPolicy");
  if (confirmationName !== gateway?.name) transitionBlockers.push("confirmName");
  if (evidenceCount === 0 && !overrideReady) transitionBlockers.push("evidenceOrOverride");
  const transitionReady = transitionBlockers.length === 0 && !operationBusy;

  const toggleAction = (actionName: string) =>
    setSelectedActions((current) =>
      current.includes(actionName) ? current.filter((item) => item !== actionName) : [...current, actionName],
    );

  const applyTemplate = () => {
    if (!gateway) return;
    if (model === "allowlist") {
      if (allSelectedActions.length > 0) setStatement(buildAllowlistStatement(gateway, allSelectedActions));
      return;
    }
    if (model === "preserve_traffic" && highRiskAcknowledged) setStatement(buildPreserveTrafficStatement(gateway));
  };

  const startGeneration = async () => {
    if (!gateway || naturalLanguage.trim().length < 10) return;
    setBusy("generation");
    try {
      setGeneration(
        await api.startGovernanceGeneration(gateway.id, {
          expected_gateway_updated_at: gateway.updated_at,
          acknowledged_gateway_ids: needsSharedAck ? gateway.shared_gateways.map((item) => item.id) : [],
          text: naturalLanguage,
          name,
        }),
      );
    } catch (generationError) {
      toast("error", governanceError(generationError));
    } finally {
      setBusy(null);
    }
  };

  const submitConfirmed = async () => {
    if (!gateway || !confirmAction) return;
    setBusy(confirmAction);
    try {
      const envelope = {
        expected_gateway_updated_at: gateway.updated_at,
        expected_policy_updated_at: existingPolicy?.updated_at,
        acknowledged_gateway_ids: needsSharedAck ? gateway.shared_gateways.map((item) => item.id) : [],
        confirmation_name: confirmationName || null,
        override_reason: evidenceCount === 0 ? overrideReason.trim() || null : null,
      };
      let result: GovernanceOperation;
      if (confirmAction === "save") {
        result = existingPolicy
          ? await api.updateGovernancePolicy(gateway.id, existingPolicy.id, {
              ...envelope,
              statement,
              description: description || null,
              manual_actions: manualActions,
            })
          : await api.createGovernancePolicy(gateway.id, {
              ...envelope,
              name,
              statement,
              description: description || null,
              authorization_model: model,
              high_risk_acknowledged: highRiskAcknowledged,
              manual_actions: manualActions,
            });
      } else if (existingPolicy) {
        const transition = { ...envelope, evidence_range: "24h" as const, audit_id: existingPolicy.audit_id ?? null };
        result =
          confirmAction === "promote"
            ? await api.promoteGovernancePolicy(gateway.id, existingPolicy.id, transition)
            : await api.rollbackGovernancePolicy(gateway.id, existingPolicy.id, transition);
      } else {
        return;
      }
      setOperation(result);
      toast("success", t("governance.messages.requestAccepted"));
    } catch (mutationError) {
      toast("error", governanceError(mutationError));
    } finally {
      setBusy(null);
      setConfirmAction(null);
    }
  };

  const back = () => setParams({ view: "gateway", gateway: gatewayId, section: "policies" });
  const title = existingPolicy ? existingPolicy.name : policyId ?? t("v2.governance.pe.newTitle");

  if (error && !gateway) {
    return (
      <>
        <FlowHeader title={title} onBack={back} />
        <Alert tone="error" action={<LinkButton onClick={() => void load()}>{t("v2.common.retry")}</LinkButton>}>
          {error}
        </Alert>
      </>
    );
  }
  if (!gateway || !policyData) {
    return (
      <>
        <FlowHeader title={title} onBack={back} />
        {refreshing ? <Spin /> : error && <Alert tone="error">{error}</Alert>}
      </>
    );
  }

  const sharedNames = gateway.shared_gateways.map((item) => item.name).join(", ");
  const confirmBody =
    confirmAction === "save"
      ? existingPolicy?.enforcement_mode === "ACTIVE"
        ? t("governance.confirm.activeCandidate", { policy: existingPolicy.name, gateways: sharedNames })
        : t("governance.confirm.savePolicy", { policy: name, gateway: gateway.name, gateways: sharedNames })
      : confirmAction === "rollback"
        ? t("governance.confirm.rollbackPolicy", { policy: existingPolicy?.name })
        : t("governance.confirm.promotePolicy", { policy: existingPolicy?.name, count: evidenceCount });

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {title}
            {existingPolicy ? (
              <>
                <StatusTag status={existingPolicy.status} />
                <StatusTag status={existingPolicy.enforcement_mode} />
              </>
            ) : (
              <Tag tone="orange">LOG_ONLY</Tag>
            )}
            <span className="v2-muted" style={{ fontWeight: 400, fontSize: 13 }}>
              {gateway.name}
            </span>
          </span>
        }
        onBack={back}
        end={
          <>
            <Button disabled={refreshing} onClick={() => void load()}>
              {t("v2.common.refresh")}
            </Button>
            <Button kind="primary" disabled={!saveReady} onClick={() => setConfirmAction("save")} testId="v2-governance-policy-save">
              {existingPolicy ? t("v2.governance.pe.saveDraft") : t("v2.governance.pe.createLogOnly")}
            </Button>
          </>
        }
      />

      {error && <Alert tone="error">{error}</Alert>}
      {!gateway.managed && <Alert tone="warn">{t("governance.policyEditor.unmanaged")}</Alert>}
      {existingPolicy?.enforcement_mode === "ACTIVE" && <Alert tone="warn">{t("governance.policyEditor.activeCreatesCandidate")}</Alert>}
      {operation?.status === "partial" && <Alert tone="error">{t("governance.policyEditor.partialState")}</Alert>}
      <OperationAlert operation={operation} />
      {needsSharedAck && <SharedEngineAck gateway={gateway} checked={sharedAcknowledged} onChange={setSharedAcknowledged} t={t} />}
      {saveBlockers.length > 0 && (
        <Alert tone="warn">
          <span data-testid="save-blockers">
            {t("governance.policyEditor.blockedPrefix")} {saveBlockers.map((key) => t(`governance.blockers.${key}`)).join(" · ")}
          </span>
        </Alert>
      )}

      <div className="v2-governance-editor">
        <div className="v2-governance-editor-main">
          <Card title={t("v2.governance.pe.definition")} testId="v2-governance-policy-definition">
            {!existingPolicy && (
              <Alert>
                <b>{t("governance.policyEditor.primerTitle")}</b> {t("governance.policyEditor.primerBody")}
              </Alert>
            )}
            <div className="v2-form cols-2">
              <Field
                label={t("v2.governance.pe.name")}
                required
                error={!nameValid ? t("governance.policyEditor.invalidName") : null}
                hint={existingPolicy ? t("v2.governance.pe.nameLocked") : undefined}
              >
                <input
                  className="v2-input mono"
                  value={name}
                  disabled={!!existingPolicy}
                  onChange={(e) => setName(e.target.value)}
                  data-testid="v2-governance-policy-name"
                />
              </Field>
              <Field label={t("v2.governance.description")}>
                <input className="v2-input" value={description} onChange={(e) => setDescription(e.target.value)} />
              </Field>

              {!existingPolicy && (
                <Field label={t("v2.governance.authorizationModel")} full hint={t(`governance.policyEditor.modelHelp.${model}`)}>
                  <div className="v2-options">
                    {MODELS.map((option) => (
                      <OptionCard
                        key={option}
                        title={t(`v2.governance.model.${option}`)}
                        desc={t(`v2.governance.modelDesc.${option}`)}
                        on={model === option}
                        onClick={() => setModel(option)}
                        testId={`v2-governance-model-${option}`}
                      />
                    ))}
                  </div>
                </Field>
              )}
              {model === "preserve_traffic" && !existingPolicy && (
                <div className="v2-field full">
                  <Alert tone="error">
                    <label className="v2-check">
                      <input type="checkbox" checked={highRiskAcknowledged} onChange={(e) => setHighRiskAcknowledged(e.target.checked)} />
                      {t("governance.models.highRiskAck")}
                    </label>
                  </Alert>
                </div>
              )}

              {model !== "custom" && !existingPolicy && (
                <Field label={t("v2.governance.pe.exactActions")} full>
                  <div className="v2-tags">
                    {gateway.actions.map((action) => {
                      const on = selectedActions.includes(action.name);
                      return (
                        <button
                          key={action.name}
                          type="button"
                          className={on ? "v2-governance-chip on" : "v2-governance-chip"}
                          aria-pressed={on}
                          onClick={() => toggleAction(action.name)}
                          title={action.description || undefined}
                        >
                          {on && <Check size={12} aria-hidden="true" />}
                          <span className="mono">{action.name}</span>
                          <small>{action.verified ? t("v2.governance.verified") : t("v2.governance.unverified")}</small>
                        </button>
                      );
                    })}
                    {gateway.actions.length === 0 && <span className="v2-muted">{t("governance.policyEditor.noActions")}</span>}
                  </div>
                </Field>
              )}

              <Field label={t("v2.governance.pe.manualAction")} full hint={t("governance.policyEditor.manualActionHelp")}>
                <div className="v2-row" style={{ flexWrap: "nowrap" }}>
                  <input
                    className="v2-input mono"
                    value={manualInput}
                    placeholder={t("governance.policyEditor.manualActionPlaceholder")}
                    onChange={(e) => setManualInput(e.target.value)}
                  />
                  <Button
                    disabled={!manualInput || manualActions.includes(manualInput)}
                    onClick={() => {
                      setManualActions((current) => [...current, manualInput]);
                      setManualInput("");
                    }}
                  >
                    <Plus size={14} aria-hidden="true" />
                    {t("v2.governance.pe.add")}
                  </Button>
                </div>
                {manualActions.length > 0 && (
                  <div className="v2-tags" style={{ marginTop: 8 }}>
                    {manualActions.map((action) => (
                      <Tag key={action} tone="orange">
                        <span className="mono">{action}</span> · {t("v2.governance.unverified")}
                        <button
                          type="button"
                          className="v2-governance-tag-x"
                          aria-label={t("v2.governance.pe.remove")}
                          onClick={() => setManualActions((current) => current.filter((item) => item !== action))}
                        >
                          <X size={12} aria-hidden="true" />
                        </button>
                      </Tag>
                    ))}
                  </div>
                )}
              </Field>
            </div>

            {!existingPolicy && model !== "custom" && (
              <div className="v2-row v2-governance-actions">
                <Button
                  kind="soft"
                  disabled={model === "allowlist" ? allSelectedActions.length === 0 : !highRiskAcknowledged}
                  onClick={applyTemplate}
                  testId="v2-governance-build-draft"
                >
                  {t("v2.governance.pe.buildDraft")}
                </Button>
              </div>
            )}

            <div className="v2-field" style={{ marginTop: 18 }}>
              <label>Cedar</label>
              <textarea
                className="v2-textarea code"
                rows={14}
                value={statement}
                spellCheck={false}
                onChange={(e) => setStatement(e.target.value)}
                data-testid="v2-governance-cedar"
              />
            </div>
          </Card>

          <Card title={t("v2.governance.pe.review")} sub={t("governance.policyEditor.liveVsDraft")}>
            <div className="v2-grid-2">
              <div>
                <div className="v2-sub-title" style={{ marginTop: 0 }}>
                  {t("v2.governance.pe.live")}
                </div>
                <pre className="v2-pre">{existingPolicy?.statement ?? t("v2.governance.pe.newPolicy")}</pre>
              </div>
              <div>
                <div className="v2-sub-title" style={{ marginTop: 0 }}>
                  {t("v2.governance.pe.draft")}
                </div>
                <pre className="v2-pre">{statement || t("v2.governance.pe.emptyDraft")}</pre>
              </div>
            </div>
          </Card>
        </div>

        <aside className="v2-governance-editor-side">
          <Card title={t("v2.governance.pe.generate")} testId="v2-governance-generate">
            <Field label={t("v2.governance.pe.intent")} hint={t("v2.governance.pe.intentHint")}>
              <textarea className="v2-textarea" rows={5} value={naturalLanguage} onChange={(e) => setNaturalLanguage(e.target.value)} />
            </Field>
            <div className="v2-row v2-governance-actions">
              <Button
                disabled={!gatewayReady || naturalLanguage.trim().length < 10 || busy === "generation"}
                onClick={() => void startGeneration()}
                testId="v2-governance-generate-run"
              >
                {t("v2.governance.pe.generateRun")}
              </Button>
            </div>
            {generation && (
              <div className="v2-stack" style={{ marginTop: 12 }}>
                <span className="v2-row">
                  <StatusTag status={generation.status} />
                  <span className="mono v2-muted">{generation.id}</span>
                </span>
                {generation.status_reasons.length > 0 && <Alert tone="error">{generation.status_reasons.join("; ")}</Alert>}
                {generation.assets.map((asset, index) => (
                  <div key={`${asset.id ?? "asset"}-${index}`} className="v2-stack">
                    <pre className="v2-pre">{asset.statement}</pre>
                    {asset.findings != null && <Alert tone="warn">{JSON.stringify(asset.findings)}</Alert>}
                    <div>
                      <Button size="sm" onClick={() => setStatement(asset.statement)}>
                        {t("v2.governance.pe.useGenerated")}
                      </Button>
                    </div>
                  </div>
                ))}
              </div>
            )}
          </Card>

          <Card title={t("v2.governance.pe.rollout")} testId="v2-governance-rollout">
            <Descriptions
              one
              items={[
                { label: t("v2.governance.currentMode"), value: <StatusTag status={gateway.policy_engine?.mode} /> },
                { label: t("v2.governance.colPolicyMode"), value: <StatusTag status={existingPolicy?.enforcement_mode ?? "LOG_ONLY"} /> },
                { label: t("v2.governance.pe.evidence"), value: `${evidenceCount} / 24h` },
              ]}
            />
            <div className="v2-muted" style={{ fontSize: 12.5, margin: "12px 0" }}>
              {t("governance.policyEditor.rolloutHelp")}
            </div>
            <div className="v2-form">
              <Field label={t("v2.governance.confirmName")} hint={t("governance.policyEditor.cutoverFieldsOnly")}>
                <input
                  className="v2-input mono"
                  value={confirmationName}
                  placeholder={gateway.name}
                  onChange={(e) => setConfirmationName(e.target.value)}
                />
              </Field>
              {evidenceCount === 0 && (
                <Field label={t("v2.governance.overrideReason")} hint={t("governance.policyEditor.overrideAudited")}>
                  <textarea className="v2-textarea" rows={3} value={overrideReason} onChange={(e) => setOverrideReason(e.target.value)} />
                </Field>
              )}
            </div>
            {existingPolicy && (
              <div className="v2-row v2-governance-actions">
                <Button kind="primary" disabled={!transitionReady} onClick={() => setConfirmAction("promote")} testId="v2-governance-promote">
                  {operation?.status === "partial" ? t("v2.governance.pe.retryCutover") : t("v2.governance.pe.promote")}
                </Button>
                {(existingPolicy.candidate_for || existingPolicy.candidate_id) && (
                  <Button kind="danger" disabled={!transitionReady} onClick={() => setConfirmAction("rollback")} testId="v2-governance-rollback">
                    {t("v2.governance.rollback")}
                  </Button>
                )}
              </div>
            )}
            {existingPolicy && transitionBlockers.length > 0 && (
              <div className="v2-governance-blockers" style={{ marginTop: 8 }} data-testid="transition-blockers">
                {t("governance.policyEditor.cutoverBlockedPrefix")} {transitionBlockers.map((key) => t(`governance.blockers.${key}`)).join(" · ")}
              </div>
            )}
          </Card>

          {gateway.external_tools_list_command && (
            <Card title={t("v2.governance.externalDiscovery")}>
              <pre className="v2-pre">{gateway.external_tools_list_command}</pre>
              <div className="v2-row v2-governance-actions">
                <Button size="sm" onClick={() => copy(gateway.external_tools_list_command ?? "")}>
                  {t("v2.governance.copy")}
                </Button>
              </div>
            </Card>
          )}
        </aside>
      </div>

      <Confirm
        open={confirmAction !== null}
        title={t(`v2.governance.confirmTitle.${confirmAction ?? "save"}`)}
        body={confirmBody}
        confirmLabel={t("v2.common.confirm")}
        danger={confirmAction === "rollback"}
        busy={busy !== null}
        onConfirm={() => void submitConfirmed()}
        onClose={() => setConfirmAction(null)}
      />
    </>
  );
}
