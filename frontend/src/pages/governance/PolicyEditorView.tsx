import {
  ArrowLeft,
  Check,
  CircleHelp,
  ClipboardCopy,
  Plus,
  RefreshCw,
  RotateCcw,
  Save,
  ShieldAlert,
  Sparkles,
  TriangleAlert,
  X,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, ConfirmDialog, Panel, useToast } from "../../components";
import {
  api,
  type GovernanceAuthorizationModel,
  type GovernanceDecisionResponse,
  type GovernanceGatewayAction,
  type GovernanceGatewayDetail,
  type GovernanceGeneration,
  type GovernanceOperation,
  type GovernancePolicyListResponse,
} from "../../lib/api";
import {
  governanceError,
  isGatewayReady,
  isOperationPending,
  statusTone,
  type GovernanceView,
} from "./types";

interface Props {
  gatewayId: string;
  policyId?: string;
  onNavigate: (view: GovernanceView, gatewayId?: string, policyId?: string) => void;
}

type ConfirmAction = "save" | "promote" | "rollback";

function cedarString(value: string): string {
  return value.replaceAll("\\", "\\\\").replaceAll('"', '\\"');
}

function buildAllowlistStatement(
  gateway: GovernanceGatewayDetail,
  actions: string[],
): string {
  const principal =
    gateway.authorizer_type === "AWS_IAM"
      ? "AgentCore::IamEntity"
      : "AgentCore::OAuthUser";
  const actionClause =
    actions.length === 1
      ? `action == AgentCore::Action::"${cedarString(actions[0])}"`
      : `action in [${actions
          .map((action) => `AgentCore::Action::"${cedarString(action)}"`)
          .join(", ")}]`;
  return `permit(
  principal is ${principal},
  ${actionClause},
  resource == AgentCore::Gateway::"${cedarString(gateway.arn)}"
);`;
}

function buildPreserveTrafficStatement(gateway: GovernanceGatewayDetail): string {
  const principal =
    gateway.authorizer_type === "AWS_IAM"
      ? "AgentCore::IamEntity"
      : "AgentCore::OAuthUser";
  return `permit(
  principal is ${principal},
  action,
  resource == AgentCore::Gateway::"${cedarString(gateway.arn)}"
);`;
}

export function PolicyEditorView({ gatewayId, policyId, onNavigate }: Props) {
  const { t } = useTranslation();
  const toast = useToast();
  const [gateway, setGateway] = useState<GovernanceGatewayDetail | null>(null);
  const [policyData, setPolicyData] = useState<GovernancePolicyListResponse | null>(
    null,
  );
  const [evidence, setEvidence] = useState<GovernanceDecisionResponse | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
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
  const [operation, setOperation] = useState<GovernanceOperation | null>(null);
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
      api.governanceDecisions(gatewayId, "24h", policyId),
    ]);
    if (gatewayResult.status === "rejected") {
      setError(governanceError(gatewayResult.reason));
      setRefreshing(false);
      return;
    }
    setGateway(gatewayResult.value);
    if (policiesResult.status === "fulfilled") {
      setPolicyData(policiesResult.value);
      const selected = policiesResult.value.policies.find(
        (policy) => policy.id === policyId,
      );
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

  useEffect(() => {
    if (!operation || !isOperationPending(operation)) return;
    const timer = window.setTimeout(() => {
      void api
        .governanceOperation(operation.id)
        .then((next) => {
          setOperation(next);
          if (!isOperationPending(next)) void load();
        })
        .catch((pollError) => toast(governanceError(pollError), "crit"));
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [load, operation, toast]);

  useEffect(() => {
    if (!generation) return;
    const settled =
      generation.status === "GENERATED" || generation.status.toUpperCase().includes("FAILED");
    if (settled) return;
    const timer = window.setTimeout(() => {
      void api
        .getGovernanceGeneration(gatewayId, generation.id)
        .then(setGeneration)
        .catch((pollError) => toast(governanceError(pollError), "crit"));
    }, 3000);
    return () => window.clearTimeout(timer);
  }, [gatewayId, generation, toast]);

  const existingPolicy = useMemo(
    () => policyData?.policies.find((policy) => policy.id === policyId) ?? null,
    [policyData, policyId],
  );
  const allSelectedActions = useMemo(
    () => [...selectedActions, ...manualActions],
    [manualActions, selectedActions],
  );
  const evidenceCount = evidence?.decisions.length ?? 0;
  const needsSharedAck = (gateway?.shared_gateways.length ?? 0) > 1;
  const sharedReady = !needsSharedAck || sharedAcknowledged;
  const operationBusy = busy !== null || isOperationPending(operation);
  const nameValid = /^[A-Za-z][A-Za-z0-9_]{0,47}$/.test(name);
  const gatewayReady =
    !!gateway &&
    gateway.managed &&
    isGatewayReady(gateway) &&
    !!gateway.policy_engine &&
    sharedReady;
  // Unmet preconditions, in the order an operator would fix them. A disabled
  // primary button swallows the click and reads as "nothing happened"
  // (ISSUE-010), so the reasons are rendered next to it.
  const gatewayBlockers: string[] = [];
  if (gateway && !gateway.managed) gatewayBlockers.push("notManaged");
  else if (gateway && !isGatewayReady(gateway)) gatewayBlockers.push("gatewayNotReady");
  if (gateway && !gateway.policy_engine) gatewayBlockers.push("noEngine");
  if (!sharedReady) gatewayBlockers.push("sharedAck");

  const saveBlockers = [...gatewayBlockers];
  if (!nameValid) saveBlockers.push("nameInvalid");
  if (statement.trim().length === 0) saveBlockers.push("noStatement");
  if (model === "preserve_traffic" && !highRiskAcknowledged) {
    saveBlockers.push("highRiskAck");
  }
  const saveReady = saveBlockers.length === 0 && !operationBusy;

  const overrideReady =
    confirmationName === gateway?.name && overrideReason.trim().length > 0;
  const transitionBlockers = [...gatewayBlockers];
  if (!existingPolicy) transitionBlockers.push("noPolicy");
  if (confirmationName !== gateway?.name) transitionBlockers.push("confirmName");
  if (evidenceCount === 0 && !overrideReady) {
    transitionBlockers.push("evidenceOrOverride");
  }
  const transitionReady = transitionBlockers.length === 0 && !operationBusy;

  const toggleAction = (action: GovernanceGatewayAction) => {
    setSelectedActions((current) =>
      current.includes(action.name)
        ? current.filter((name) => name !== action.name)
        : [...current, action.name],
    );
  };

  const applyTemplate = () => {
    if (!gateway) return;
    if (model === "allowlist") {
      if (allSelectedActions.length === 0) return;
      setStatement(buildAllowlistStatement(gateway, allSelectedActions));
      return;
    }
    if (model === "preserve_traffic" && highRiskAcknowledged) {
      setStatement(buildPreserveTrafficStatement(gateway));
    }
  };

  const startGeneration = async () => {
    if (!gateway || naturalLanguage.trim().length < 10) return;
    setBusy("generation");
    try {
      setGeneration(
        await api.startGovernanceGeneration(gateway.id, {
          expected_gateway_updated_at: gateway.updated_at,
          acknowledged_gateway_ids: needsSharedAck
            ? gateway.shared_gateways.map((item) => item.id)
            : [],
          text: naturalLanguage,
          name,
        }),
      );
    } catch (generationError) {
      toast(governanceError(generationError), "crit");
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
        acknowledged_gateway_ids: needsSharedAck
          ? gateway.shared_gateways.map((item) => item.id)
          : [],
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
      } else if (confirmAction === "promote" && existingPolicy) {
        result = await api.promoteGovernancePolicy(
          gateway.id,
          existingPolicy.id,
          {
            ...envelope,
            evidence_range: "24h",
            audit_id: existingPolicy.audit_id ?? null,
          },
        );
      } else if (existingPolicy) {
        result = await api.rollbackGovernancePolicy(
          gateway.id,
          existingPolicy.id,
          {
            ...envelope,
            evidence_range: "24h",
            audit_id: existingPolicy.audit_id ?? null,
          },
        );
      } else {
        return;
      }
      setOperation(result);
      toast(t("governance.messages.requestAccepted"), "good");
    } catch (mutationError) {
      toast(governanceError(mutationError), "crit");
    } finally {
      setBusy(null);
      setConfirmAction(null);
    }
  };

  if (error && !gateway) {
    return (
      <Panel brk>
        <div className="gov-state gov-state-error">
          <TriangleAlert size={20} aria-hidden="true" />
          <strong>{t("governance.states.unavailable")}</strong>
          <span>{error}</span>
          <Btn onClick={() => onNavigate("gateway", gatewayId)}>
            <ArrowLeft size={14} aria-hidden="true" />
            {t("governance.actions.back")}
          </Btn>
        </div>
      </Panel>
    );
  }

  if (!gateway || !policyData) {
    return <div className="loading-line">{t("common.loading")}</div>;
  }

  const confirmBody =
    confirmAction === "save"
      ? existingPolicy?.enforcement_mode === "ACTIVE"
        ? t("governance.confirm.activeCandidate", {
            policy: existingPolicy.name,
            gateways: gateway.shared_gateways.map((item) => item.name).join(", "),
          })
        : t("governance.confirm.savePolicy", {
            policy: name,
            gateway: gateway.name,
            gateways: gateway.shared_gateways.map((item) => item.name).join(", "),
          })
      : confirmAction === "rollback"
        ? t("governance.confirm.rollbackPolicy", { policy: existingPolicy?.name })
        : t("governance.confirm.promotePolicy", {
            policy: existingPolicy?.name,
            count: evidenceCount,
          });

  return (
    <>
      <div className="gov-toolbar">
        <Btn onClick={() => onNavigate("gateway", gateway.id)}>
          <ArrowLeft size={14} aria-hidden="true" />
          {t("governance.actions.back")}
        </Btn>
        <div className="gov-toolbar-title">
          <strong>
            {existingPolicy
              ? existingPolicy.name
              : t("governance.policyEditor.newTitle")}
          </strong>
          <span>{gateway.name}</span>
        </div>
        {existingPolicy ? (
          <>
            <Chip tone={statusTone(existingPolicy.status)}>{existingPolicy.status}</Chip>
            <Chip tone={statusTone(existingPolicy.enforcement_mode)}>
              {existingPolicy.enforcement_mode}
            </Chip>
          </>
        ) : (
          <Chip tone="warn">LOG_ONLY</Chip>
        )}
        <Btn disabled={refreshing} onClick={() => void load()}>
          <RefreshCw size={14} aria-hidden="true" />
          {t("governance.actions.refresh")}
        </Btn>
      </div>

      {!gateway.managed ? (
        <div className="gov-alert gov-alert-warn">
          <ShieldAlert size={15} aria-hidden="true" />
          {t("governance.policyEditor.unmanaged")}
        </div>
      ) : null}
      {existingPolicy?.enforcement_mode === "ACTIVE" ? (
        <div className="gov-alert gov-alert-warn">
          <TriangleAlert size={15} aria-hidden="true" />
          {t("governance.policyEditor.activeCreatesCandidate")}
        </div>
      ) : null}
      {operation?.status === "partial" ? (
        <div className="gov-alert gov-alert-error">
          <TriangleAlert size={15} aria-hidden="true" />
          {t("governance.policyEditor.partialState")}
        </div>
      ) : null}

      {needsSharedAck ? (
        <div className="gov-alert gov-alert-warn">
          <TriangleAlert size={15} aria-hidden="true" />
          <div>
            <strong>{t("governance.detail.sharedEngine")}</strong>
            <ul>
              {gateway.shared_gateways.map((item) => (
                <li key={item.id}>
                  {item.name} <span className="mono">{item.id}</span>
                </li>
              ))}
            </ul>
            <label className="gov-check-row">
              <input
                type="checkbox"
                checked={sharedAcknowledged}
                onChange={(event) => setSharedAcknowledged(event.target.checked)}
              />
              <span>{t("governance.detail.sharedAck")}</span>
            </label>
          </div>
        </div>
      ) : null}

      <div className="gov-editor-grid">
        <div className="gov-editor-main">
          <Panel title={t("governance.policyEditor.policyDefinition")} brk>
            {!existingPolicy ? (
              <div className="gov-policy-primer" role="note">
                <CircleHelp size={16} aria-hidden="true" />
                <div>
                  <strong>{t("governance.policyEditor.primerTitle")}</strong>
                  <span>{t("governance.policyEditor.primerBody")}</span>
                </div>
              </div>
            ) : null}

            <div className="gov-grid">
              <div className="field">
                <label>{t("governance.policyEditor.name")}</label>
                <input
                  className="input mono"
                  value={name}
                  disabled={!!existingPolicy}
                  onChange={(event) => setName(event.target.value)}
                />
                {!nameValid ? (
                  <div className="gov-field-error">
                    {t("governance.policyEditor.invalidName")}
                  </div>
                ) : null}
              </div>
              <div className="field">
                <label>{t("governance.policyEditor.description")}</label>
                <input
                  className="input"
                  value={description}
                  onChange={(event) => setDescription(event.target.value)}
                />
              </div>
            </div>

            {!existingPolicy ? (
              <div className="field">
                <label>{t("governance.policyEditor.authorizationModel")}</label>
                <div className="gov-mode-control">
                  {(["allowlist", "preserve_traffic", "custom"] as const).map(
                    (option) => (
                      <button
                        key={option}
                        type="button"
                        className={`selchip${model === option ? " on" : ""}`}
                        onClick={() => setModel(option)}
                      >
                        {t(
                          option === "preserve_traffic"
                            ? "governance.models.preserveTraffic"
                            : `governance.models.${option}`,
                        )}
                      </button>
                    ),
                  )}
                </div>
                <div className="gov-field-help">
                  {t(`governance.policyEditor.modelHelp.${model}`)}
                </div>
              </div>
            ) : null}

            {model === "preserve_traffic" && !existingPolicy ? (
              <label className="gov-check-row gov-alert gov-alert-error">
                <input
                  type="checkbox"
                  checked={highRiskAcknowledged}
                  onChange={(event) => setHighRiskAcknowledged(event.target.checked)}
                />
                <span>{t("governance.models.highRiskAck")}</span>
              </label>
            ) : null}

            {model !== "custom" && !existingPolicy ? (
              <div className="field">
                <label>{t("governance.policyEditor.exactActions")}</label>
                <div className="gov-action-picker">
                  {gateway.actions.map((action) => (
                    <button
                      key={action.name}
                      type="button"
                      className={`selchip${
                        selectedActions.includes(action.name) ? " on" : ""
                      }`}
                      onClick={() => toggleAction(action)}
                    >
                      {selectedActions.includes(action.name) ? (
                        <Check size={12} aria-hidden="true" />
                      ) : null}
                      {action.name}
                      <small>
                        {action.verified
                          ? t("governance.states.verified")
                          : t("governance.states.unverified")}
                      </small>
                    </button>
                  ))}
                  {gateway.actions.length === 0 ? (
                    <span className="dim">{t("governance.policyEditor.noActions")}</span>
                  ) : null}
                </div>
              </div>
            ) : null}

            <div className="field">
              <label>{t("governance.policyEditor.manualAction")}</label>
              <div className="gov-inline-field">
                <input
                  className="input mono"
                  value={manualInput}
                  placeholder={t("governance.policyEditor.manualActionPlaceholder")}
                  onChange={(event) => setManualInput(event.target.value)}
                />
                <Btn
                  disabled={!manualInput || manualActions.includes(manualInput)}
                  onClick={() => {
                    setManualActions((current) => [...current, manualInput]);
                    setManualInput("");
                  }}
                >
                  <Plus size={14} aria-hidden="true" />
                  {t("governance.actions.add")}
                </Btn>
              </div>
              <div className="gov-field-help">
                {t("governance.policyEditor.manualActionHelp")}
              </div>
              {manualActions.map((action) => (
                <span className="selchip on" key={action}>
                  {action}
                  <Chip tone="warn">{t("governance.states.unverified")}</Chip>
                  <button
                    type="button"
                    className="gov-icon-button"
                    title={t("governance.actions.remove")}
                    onClick={() =>
                      setManualActions((current) =>
                        current.filter((item) => item !== action),
                      )
                    }
                  >
                    <X size={12} aria-hidden="true" />
                  </button>
                </span>
              ))}
            </div>

            {!existingPolicy && model !== "custom" ? (
              <Btn
                disabled={
                  model === "allowlist"
                    ? allSelectedActions.length === 0
                    : !highRiskAcknowledged
                }
                onClick={applyTemplate}
              >
                <ShieldAlert size={14} aria-hidden="true" />
                {t("governance.actions.buildDraft")}
              </Btn>
            ) : null}

            <div className="field gov-cedar-field">
              <label>CEDAR</label>
              <textarea
                className="input mono"
                rows={16}
                value={statement}
                onChange={(event) => setStatement(event.target.value)}
              />
            </div>
          </Panel>

          <Panel
            title={t("governance.policyEditor.diff")}
            sub={t("governance.policyEditor.liveVsDraft")}
          >
            <div className="gov-diff">
              <div>
                <h4>{t("governance.policyEditor.live")}</h4>
                <pre className="code gov-code-wrap">
                  {existingPolicy?.statement ?? t("governance.states.newPolicy")}
                </pre>
              </div>
              <div>
                <h4>{t("governance.policyEditor.draft")}</h4>
                <pre className="code gov-code-wrap">
                  {statement || t("governance.states.emptyDraft")}
                </pre>
              </div>
            </div>
          </Panel>
        </div>

        <aside className="gov-editor-side">
          <Panel title={t("governance.policyEditor.generate")}>
            <div className="field">
              <label>{t("governance.policyEditor.naturalLanguage")}</label>
              <textarea
                className="input"
                rows={6}
                value={naturalLanguage}
                onChange={(event) => setNaturalLanguage(event.target.value)}
              />
            </div>
            <Btn
              disabled={
                !gatewayReady ||
                naturalLanguage.trim().length < 10 ||
                busy === "generation"
              }
              onClick={() => void startGeneration()}
            >
              <Sparkles size={14} aria-hidden="true" />
              {t("governance.actions.generate")}
            </Btn>
            {generation ? (
              <div className="gov-generation">
                <Chip tone={statusTone(generation.status)}>{generation.status}</Chip>
                <span className="mono">{generation.id}</span>
                {generation.status_reasons.length > 0 ? (
                  <div className="gov-inline-error">
                    {generation.status_reasons.join("; ")}
                  </div>
                ) : null}
                {generation.assets.map((asset, index) => (
                  <div
                    className="gov-generated-asset"
                    key={`${asset.id ?? "asset"}-${index}`}
                  >
                    <pre className="code gov-code-wrap">{asset.statement}</pre>
                    {asset.findings ? (
                      <div className="gov-inline-error">
                        {JSON.stringify(asset.findings)}
                      </div>
                    ) : null}
                    <Btn onClick={() => setStatement(asset.statement)}>
                      <Save size={14} aria-hidden="true" />
                      {t("governance.actions.useGenerated")}
                    </Btn>
                  </div>
                ))}
              </div>
            ) : null}
          </Panel>

          <Panel title={t("governance.policyEditor.rollout")}>
            <div className="gov-kv-list">
              <div className="kv">
                <span className="k">{t("governance.policyEditor.gatewayMode")}</span>
                <span className="v">{gateway.policy_engine?.mode ?? "-"}</span>
              </div>
              <div className="kv">
                <span className="k">{t("governance.policyEditor.policyMode")}</span>
                <span className="v">
                  {existingPolicy?.enforcement_mode ?? "LOG_ONLY"}
                </span>
              </div>
              <div className="kv">
                <span className="k">{t("governance.policyEditor.evidence")}</span>
                <span className="v">{evidenceCount} / 24h</span>
              </div>
            </div>
            <div className="gov-field-help gov-rollout-help">
              {t("governance.policyEditor.rolloutHelp")}
            </div>
            <div className="field">
              <label>{t("governance.detail.confirmGatewayName")}</label>
              <input
                className="input mono"
                value={confirmationName}
                placeholder={gateway.name}
                onChange={(event) => setConfirmationName(event.target.value)}
              />
              {/* only the cutover actions gate on these two fields — saving a
                  LOG_ONLY draft never has, so saying so stops the operator
                  hunting a create-time gate that does not exist (ISSUE-010) */}
              <div className="mono dim" style={{ fontSize: 10 }}>
                {t("governance.policyEditor.cutoverFieldsOnly")}
              </div>
            </div>
            {evidenceCount === 0 ? (
              <div className="field">
                <label>{t("governance.detail.overrideReason")}</label>
                <textarea
                  className="input"
                  rows={3}
                  value={overrideReason}
                  onChange={(event) => setOverrideReason(event.target.value)}
                />
                <div className="mono dim" style={{ fontSize: 10 }}>
                  {t("governance.policyEditor.overrideAudited")}
                </div>
              </div>
            ) : null}
            {saveBlockers.length > 0 ? (
              <div className="gov-inline-error" data-testid="save-blockers">
                {t("governance.policyEditor.blockedPrefix")}{" "}
                {saveBlockers.map((key) => t(`governance.blockers.${key}`)).join(" · ")}
              </div>
            ) : null}
            <div className="gov-actions">
              <Btn
                primary
                disabled={!saveReady}
                onClick={() => setConfirmAction("save")}
              >
                <Save size={14} aria-hidden="true" />
                {existingPolicy
                  ? t("governance.actions.saveDraft")
                  : t("governance.actions.createLogOnly")}
              </Btn>
              {existingPolicy ? (
                <Btn
                  disabled={!transitionReady}
                  onClick={() => setConfirmAction("promote")}
                >
                  <ShieldAlert size={14} aria-hidden="true" />
                  {operation?.status === "partial"
                    ? t("governance.actions.retryCutover")
                    : t("governance.actions.promote")}
                </Btn>
              ) : null}
              {existingPolicy?.candidate_for || existingPolicy?.candidate_id ? (
                <Btn
                  disabled={!transitionReady}
                  onClick={() => setConfirmAction("rollback")}
                >
                  <RotateCcw size={14} aria-hidden="true" />
                  {t("governance.actions.rollback")}
                </Btn>
              ) : null}
            </div>
            {existingPolicy && transitionBlockers.length > 0 ? (
              <div className="gov-inline-error" data-testid="transition-blockers">
                {t("governance.policyEditor.cutoverBlockedPrefix")}{" "}
                {transitionBlockers
                  .map((key) => t(`governance.blockers.${key}`))
                  .join(" · ")}
              </div>
            ) : null}
          </Panel>

          {gateway.external_tools_list_command ? (
            <Panel title={t("governance.policyEditor.externalDiscovery")}>
              <pre className="code gov-code-wrap">
                {gateway.external_tools_list_command}
              </pre>
              <Btn
                onClick={() => {
                  void navigator.clipboard.writeText(
                    gateway.external_tools_list_command ?? "",
                  );
                  toast(t("governance.messages.copied"), "good");
                }}
              >
                <ClipboardCopy size={14} aria-hidden="true" />
                {t("governance.actions.copy")}
              </Btn>
            </Panel>
          ) : null}
        </aside>
      </div>

      {operation ? (
        <div className={`gov-operation gov-operation-${operation.status}`}>
          <Chip tone={statusTone(operation.status)}>{operation.status}</Chip>
          <span>{operation.operation}</span>
          <span className="mono">{operation.id}</span>
          {operation.error ? <strong>{operation.error}</strong> : null}
        </div>
      ) : null}

      <ConfirmDialog
        open={confirmAction !== null}
        title={t(`governance.confirmTitles.${confirmAction ?? "save"}`)}
        body={confirmBody}
        confirmLabel={t("governance.actions.confirm")}
        onCancel={() => setConfirmAction(null)}
        onConfirm={() => void submitConfirmed()}
      />
    </>
  );
}
