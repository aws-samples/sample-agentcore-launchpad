import { Plus, RotateCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  type GovernanceAuthorizationModel,
  type GovernanceDecisionResponse,
  type GovernanceGatewayDetail,
  type GovernanceGatewayMode,
  type GovernanceGatewayTarget,
  type GovernancePolicy,
  type GovernancePolicyListResponse,
  type GovernanceRegistryPreview,
} from "../../../lib/api";
import {
  governanceError,
  isGatewayReady,
  TARGET_SYNC_POLL_INTERVAL_MS,
  TARGET_SYNC_POLL_MS,
} from "../../../lib/governance";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  Field,
  FlowHeader,
  LinkButton,
  Segmented,
  Spin,
  SubTabs,
  Table,
  Tag,
} from "../../ui";
import { AuditSection } from "./Audit";
import { type GatewaySection, GATEWAY_SECTIONS, targetKind, useCopy, useGovernanceOperation } from "./common";
import { JsonBlock, OperationAlert, SharedEngineAck, StatusTag } from "./widgets";
import { DecisionsSection } from "./Decisions";
import { PolicyTestCard } from "./PolicyTest";
import { RateLimitsSection } from "./RateLimits";

type ConfirmAction = "manage" | "unmanage" | "import" | "retire" | "engine" | "logOnly" | "enforce" | "deletePolicy";

const DANGER: ConfirmAction[] = ["unmanage", "retire", "deletePolicy"];

interface LoadErrors {
  registry?: string;
  policies?: string;
  decisions?: string;
}

/** One MCP Gateway: identity, Registry, Policy Engine + mode, IAM, policies, targets, limits, decisions, audit. */
export function GatewayDetail({ gatewayId, section }: { gatewayId: string; section: GatewaySection }) {
  const { t, i18n } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const copy = useCopy(t);
  const [gateway, setGateway] = useState<GovernanceGatewayDetail | null>(null);
  const [policies, setPolicies] = useState<GovernancePolicyListResponse | null>(null);
  const [registry, setRegistry] = useState<GovernanceRegistryPreview | null>(null);
  const [evidence, setEvidence] = useState<GovernanceDecisionResponse | null>(null);
  const [fatalError, setFatalError] = useState<string | null>(null);
  const [loadErrors, setLoadErrors] = useState<LoadErrors>({});
  const [refreshing, setRefreshing] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  const [policyToDelete, setPolicyToDelete] = useState<GovernancePolicy | null>(null);
  const [selectedLegacy, setSelectedLegacy] = useState<string[]>([]);
  const [sharedAcknowledged, setSharedAcknowledged] = useState(false);
  const [confirmationName, setConfirmationName] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [authorizationModel, setAuthorizationModel] = useState<GovernanceAuthorizationModel>("allowlist");
  const [initialEngineMode, setInitialEngineMode] = useState<GovernanceGatewayMode>("ENFORCE");
  const [targetGatewayMode, setTargetGatewayMode] = useState<GovernanceGatewayMode>("ENFORCE");
  const [highRiskAcknowledged, setHighRiskAcknowledged] = useState(false);
  // rate limits / decisions / audit own their fetches; REFRESH reaches them through this tick
  const [refreshTick, setRefreshTick] = useState(0);
  // SynchronizeGatewayTargets answers 202 and the target sits in SYNCHRONIZING with no
  // operation row to poll, so the detail itself is re-fetched until every target settles
  const [syncingTarget, setSyncingTarget] = useState<string | null>(null);
  const [syncPollStartedAt, setSyncPollStartedAt] = useState<number | null>(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    setFatalError(null);
    setRefreshTick((tick) => tick + 1);
    const [gatewayResult, policyResult, registryResult, decisionResult] = await Promise.allSettled([
      api.getGovernanceGateway(gatewayId),
      api.listGovernancePolicies(gatewayId),
      api.governanceRegistryPreview(gatewayId),
      api.governanceDecisions(gatewayId, "24h"),
    ]);
    if (gatewayResult.status === "fulfilled") {
      setGateway(gatewayResult.value);
      const liveMode = gatewayResult.value.policy_engine?.missing ? null : gatewayResult.value.policy_engine?.mode;
      if (liveMode) setTargetGatewayMode(liveMode);
    } else {
      setFatalError(governanceError(gatewayResult.reason));
    }
    if (policyResult.status === "fulfilled") setPolicies(policyResult.value);
    if (registryResult.status === "fulfilled") {
      setRegistry(registryResult.value);
      setSelectedLegacy((current) =>
        current.filter((id) => registryResult.value.legacy_records.some((record) => record.record_id === id)),
      );
    }
    if (decisionResult.status === "fulfilled") setEvidence(decisionResult.value);
    setLoadErrors({
      policies: policyResult.status === "rejected" ? governanceError(policyResult.reason) : undefined,
      registry: registryResult.status === "rejected" ? governanceError(registryResult.reason) : undefined,
      decisions: decisionResult.status === "rejected" ? governanceError(decisionResult.reason) : undefined,
    });
    setRefreshing(false);
  }, [gatewayId]);

  useEffect(() => {
    void load();
  }, [load]);

  const { operation, setOperation, pending: operationPending } = useGovernanceOperation(() => void load());

  const synchronizingTargets = useMemo(
    () => gateway?.targets.filter((target) => target.status === "SYNCHRONIZING") ?? [],
    [gateway],
  );
  useEffect(() => {
    if (syncPollStartedAt === null) return;
    const timedOut = Date.now() - syncPollStartedAt > TARGET_SYNC_POLL_MS;
    if (synchronizingTargets.length === 0 || timedOut) {
      setSyncPollStartedAt(null);
      return;
    }
    const timer = window.setTimeout(() => void load(), TARGET_SYNC_POLL_INTERVAL_MS);
    return () => window.clearTimeout(timer);
  }, [load, syncPollStartedAt, synchronizingTargets]);

  const sharedIds = useMemo(() => gateway?.shared_gateways.map((item) => item.id) ?? [], [gateway]);
  const needsSharedAck = (gateway?.shared_gateways.length ?? 0) > 1;
  const operationBusy = busy !== null || operationPending;
  const evidenceCount = evidence?.log_only_count ?? 0;
  const hasOverrideReason = overrideReason.trim().length > 0;
  const engineReady = gateway?.policy_engine?.status.toUpperCase() === "ACTIVE";
  const iamPass = gateway?.iam_preflight?.status === "pass";
  const commonMutationReady = !!gateway && gateway.managed && isGatewayReady(gateway) && !operationBusy;
  const sharedReady = !needsSharedAck || sharedAcknowledged;

  const mutationEnvelope = () => ({
    expected_gateway_updated_at: gateway?.updated_at,
    acknowledged_gateway_ids: needsSharedAck ? sharedIds : [],
    confirmation_name: confirmationName || null,
    override_reason: evidenceCount === 0 ? overrideReason.trim() || null : null,
  });

  const finishMutation = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    try {
      const result = await action();
      // operation-backed mutations answer with the operation row; the rest are synchronous
      if (result && typeof result === "object" && "operation" in result && "status" in result) {
        setOperation(result as Parameters<typeof setOperation>[0]);
      } else {
        await load();
      }
      toast("success", t("governance.messages.requestAccepted"));
    } catch (error) {
      toast("error", governanceError(error));
    } finally {
      setBusy(null);
      setConfirmAction(null);
      setPolicyToDelete(null);
    }
  };

  const synchronizeTarget = async (target: GovernanceGatewayTarget) => {
    if (!gateway) return;
    setSyncingTarget(target.id);
    try {
      const next = await api.synchronizeGovernanceTarget(gateway.id, target.id);
      setGateway((current) =>
        current ? { ...current, targets: current.targets.map((item) => (item.id === next.id ? next : item)) } : current,
      );
      setSyncPollStartedAt(Date.now());
      toast("success", t("governance.messages.targetSyncAccepted", { name: target.name }));
    } catch (error) {
      toast("error", governanceError(error));
    } finally {
      setSyncingTarget(null);
    }
  };

  const runConfirmedAction = () => {
    if (!gateway || !confirmAction) return;
    switch (confirmAction) {
      case "manage":
        void finishMutation("manage", () => api.manageGovernanceGateway(gateway.id));
        return;
      case "unmanage":
        void finishMutation("unmanage", () => api.unmanageGovernanceGateway(gateway.id));
        return;
      case "import":
        void finishMutation("import", () =>
          api.importGovernanceRegistry(gateway.id, {
            ...mutationEnvelope(),
            record_name: registry?.proposed.name ?? gateway.name,
            apply_update: registry?.outcome === "changed",
          }),
        );
        return;
      case "retire":
        void finishMutation("retire", () =>
          api.retireGovernanceLegacyRecords(gateway.id, { ...mutationEnvelope(), record_ids: selectedLegacy }),
        );
        return;
      case "deletePolicy": {
        const policy = policyToDelete;
        if (!policy) return;
        void finishMutation("deletePolicy", () =>
          api.deleteGovernancePolicy(gateway.id, policy.id, {
            ...mutationEnvelope(),
            expected_policy_updated_at: policy.updated_at,
          }),
        );
        return;
      }
      case "engine":
        void finishMutation("engine", () =>
          api.attachGovernanceEngine(gateway.id, {
            ...mutationEnvelope(),
            mode: initialEngineMode,
            authorization_model: authorizationModel,
            high_risk_acknowledged: highRiskAcknowledged,
          }),
        );
        return;
      default:
        void finishMutation("mode", () =>
          api.setGovernanceGatewayMode(gateway.id, {
            ...mutationEnvelope(),
            acknowledged_gateway_ids: [],
            mode: confirmAction === "enforce" ? "ENFORCE" : "LOG_ONLY",
            evidence_range: "24h",
          }),
        );
    }
  };

  const back = () => setParams({});
  const goSection = (next: GatewaySection) =>
    setParams(next === "overview" ? { view: "gateway", gateway: gatewayId } : { view: "gateway", gateway: gatewayId, section: next });

  if (fatalError || (!gateway && !refreshing)) {
    return (
      <>
        <FlowHeader title={gatewayId} onBack={back} />
        <Alert tone="error" action={<LinkButton onClick={() => void load()}>{t("v2.common.retry")}</LinkButton>}>
          {fatalError ?? t("governance.states.noData")}
        </Alert>
      </>
    );
  }
  if (!gateway) {
    return (
      <>
        <FlowHeader title={gatewayId} onBack={back} />
        <Spin />
      </>
    );
  }

  const registryApproved = registry?.exact_record?.status === "APPROVED";
  // A Gateway keeps its Engine reference after the Engine is deleted out-of-band: neither an
  // attachment nor a clean slate, so the create form is offered and it replaces the stale reference.
  const liveEngine = gateway.policy_engine && !gateway.policy_engine.missing ? gateway.policy_engine : null;
  const danglingEngine = gateway.policy_engine?.missing ? gateway.policy_engine : null;
  const modeBlockers: string[] = [];
  if (!gateway.managed) modeBlockers.push("notManaged");
  if (!isGatewayReady(gateway)) modeBlockers.push("gatewayNotReady");
  if (operationBusy) modeBlockers.push("busy");
  if (!engineReady) modeBlockers.push("engineNotActive");
  if (targetGatewayMode === liveEngine?.mode) modeBlockers.push("modeAlreadyActive");
  if (targetGatewayMode === "ENFORCE") {
    if (!iamPass) modeBlockers.push("iamPreflight");
    if (confirmationName !== gateway.name) modeBlockers.push("confirmName");
    if (evidenceCount === 0 && !hasOverrideReason) modeBlockers.push("evidenceOrOverride");
  }
  const engineBlocked =
    !commonMutationReady || !sharedReady || (authorizationModel === "preserve_traffic" && !highRiskAcknowledged);
  const policyCount = policies?.policies.length;

  const confirmBody =
    t(`governance.confirm.${confirmAction ?? "manage"}`, {
      name: gateway.name,
      engine: liveEngine?.name ?? t("governance.states.newEngine"),
      count: selectedLegacy.length,
      gateways: gateway.shared_gateways.map((item) => item.name).join(", "),
      policy: policyToDelete?.name ?? "",
      mode: initialEngineMode,
    }) +
    (confirmAction === "engine" && danglingEngine
      ? ` ${t("governance.confirm.engineReplaces", { arn: danglingEngine.arn })}`
      : "");

  /* ---------------- overview ---------------- */
  const overview = (
    <>
      <Card title={t("v2.governance.identity")} testId="v2-governance-identity">
        {gateway.status_reasons.length > 0 && <Alert tone="error">{gateway.status_reasons.join("; ")}</Alert>}
        <Descriptions
          items={[
            { label: "ARN", value: <span className="mono">{gateway.arn}</span> },
            { label: t("v2.governance.endpoint"), value: gateway.url ? <span className="mono">{gateway.url}</span> : "—" },
            { label: t("v2.governance.colAuthorizer"), value: <span className="mono">{gateway.authorizer_type}</span> },
            { label: t("v2.governance.protocol"), value: gateway.protocol_type },
            { label: t("v2.governance.role"), value: gateway.role_arn ? <span className="mono">{gateway.role_arn}</span> : "—" },
            { label: t("v2.governance.awsUpdated"), value: fmtTime(gateway.updated_at) },
          ]}
        />
      </Card>

      <div className="v2-grid-2">
        <Card
          title={t("v2.governance.registry")}
          end={
            gateway.attachability.attachable ? (
              <Tag tone="green">{t("v2.governance.attachable")}</Tag>
            ) : (
              <Tag tone="orange">{t("v2.governance.catalogOnly")}</Tag>
            )
          }
          testId="v2-governance-registry"
        >
          <div className="v2-muted v2-governance-note">{t("governance.detail.catalogSeparate")}</div>
          {!gateway.attachability.attachable && (
            <Alert tone="warn">{gateway.attachability.reason ?? t("governance.states.authUnresolved")}</Alert>
          )}
          {loadErrors.registry ? (
            <Alert tone="error">{loadErrors.registry}</Alert>
          ) : registry ? (
            <>
              <Descriptions
                one
                items={[
                  { label: t("v2.governance.previewOutcome"), value: <Tag tone="outline">{registry.outcome}</Tag> },
                  {
                    label: t("v2.governance.gatewayRecord"),
                    value: registry.exact_record ? (
                      <span className="v2-row">
                        {registry.exact_record.name} <StatusTag status={registry.exact_record.status} />
                      </span>
                    ) : (
                      t("v2.governance.notCataloged")
                    ),
                  },
                  { label: t("v2.governance.legacyRecords"), value: registry.legacy_records.length },
                ]}
              />
              {registry.name_conflict && (
                <Alert tone="error">{t("governance.detail.registryConflict", { name: registry.name_conflict.name })}</Alert>
              )}
              <div className="v2-row v2-governance-actions">
                <Button
                  kind="primary"
                  disabled={
                    !commonMutationReady ||
                    !!registry.name_conflict ||
                    (registry.outcome === "reused" && registry.exact_record?.status !== "DRAFT")
                  }
                  onClick={() => setConfirmAction("import")}
                  testId="v2-governance-import"
                >
                  {registry.outcome === "changed" ? t("v2.governance.syncRegistry") : t("v2.governance.importRegistry")}
                </Button>
              </div>
              {registry.legacy_records.length > 0 && (
                <div className="v2-governance-legacy">
                  <div className="v2-sub-title">{t("v2.governance.legacyRecords")}</div>
                  <div className="v2-stack">
                    {registry.legacy_records.map((record) => (
                      <label key={record.record_id} className="v2-check">
                        <input
                          type="checkbox"
                          checked={selectedLegacy.includes(record.record_id)}
                          onChange={(e) =>
                            setSelectedLegacy((current) =>
                              e.target.checked
                                ? [...current, record.record_id]
                                : current.filter((id) => id !== record.record_id),
                            )
                          }
                        />
                        {record.name} <StatusTag status={record.status} />
                      </label>
                    ))}
                  </div>
                  <div className="v2-row v2-governance-actions">
                    <Button
                      kind="danger"
                      disabled={!commonMutationReady || !registryApproved || selectedLegacy.length === 0}
                      title={!registryApproved ? t("v2.governance.retireNeedsApproval") : undefined}
                      onClick={() => setConfirmAction("retire")}
                    >
                      {t("v2.governance.retireLegacy")}
                    </Button>
                  </div>
                </div>
              )}
            </>
          ) : (
            <Spin />
          )}
        </Card>

        <Card
          title={t("v2.governance.iam")}
          end={
            gateway.iam_preflight ? (
              <StatusTag status={gateway.iam_preflight.status.toUpperCase()} />
            ) : (
              <Tag tone="gray">{t("v2.governance.notAvailable")}</Tag>
            )
          }
          testId="v2-governance-iam"
        >
          {gateway.iam_preflight ? (
            <>
              {gateway.iam_preflight.status !== "pass" ? (
                <Alert tone="error">
                  {gateway.iam_preflight.reason}
                  {gateway.iam_preflight.operator_error ? ` / ${gateway.iam_preflight.operator_error}` : ""}
                </Alert>
              ) : (
                <Alert tone="success">{t("governance.detail.iamPass")}</Alert>
              )}
              <JsonBlock value={gateway.iam_preflight.remediation} />
              <div className="v2-row v2-governance-actions">
                <Button size="sm" onClick={() => copy(JSON.stringify(gateway.iam_preflight?.remediation ?? {}, null, 2))}>
                  {t("v2.governance.copy")}
                </Button>
              </div>
            </>
          ) : (
            <div className="v2-muted">{t("governance.detail.iamAfterEngine")}</div>
          )}
        </Card>
      </div>

      <Card
        title={t("v2.governance.engine")}
        end={
          liveEngine ? (
            <StatusTag status={liveEngine.mode} />
          ) : danglingEngine ? (
            <Tag tone="red">{t("v2.governance.engineDeleted")}</Tag>
          ) : (
            <Tag tone="gray">{t("v2.governance.notAttached")}</Tag>
          )
        }
        testId="v2-governance-engine"
      >
        {liveEngine ? (
          <>
            <Descriptions
              items={[
                { label: t("v2.governance.engineName"), value: <span className="mono">{liveEngine.name}</span> },
                { label: t("v2.governance.engineStatus"), value: <StatusTag status={liveEngine.status} /> },
                { label: t("v2.governance.currentMode"), value: <StatusTag status={liveEngine.mode} /> },
                { label: t("v2.governance.policyCount"), value: policyCount ?? "—" },
              ]}
            />
            <div className="v2-sub-title">{t("v2.governance.modeRollout")}</div>
            <div className="v2-form cols-2">
              <Field label={t("v2.governance.targetMode")}>
                <div>
                  <Segmented
                    value={targetGatewayMode}
                    onChange={setTargetGatewayMode}
                    options={(["LOG_ONLY", "ENFORCE"] as GovernanceGatewayMode[]).map((mode) => ({ value: mode, label: mode }))}
                  />
                </div>
              </Field>
              {targetGatewayMode === "ENFORCE" && (
                <Field label={t("v2.governance.confirmName")} required hint={t("v2.governance.confirmNameHint", { name: gateway.name })}>
                  <input
                    className="v2-input mono"
                    value={confirmationName}
                    placeholder={gateway.name}
                    onChange={(e) => setConfirmationName(e.target.value)}
                    data-testid="v2-governance-confirm-name"
                  />
                </Field>
              )}
              {targetGatewayMode === "ENFORCE" &&
                (evidenceCount === 0 ? (
                  <Field label={t("v2.governance.overrideReason")} full>
                    <textarea
                      className="v2-textarea"
                      rows={3}
                      value={overrideReason}
                      onChange={(e) => setOverrideReason(e.target.value)}
                    />
                  </Field>
                ) : (
                  <div className="v2-field full">
                    <Alert tone="success">{t("governance.detail.evidenceReady", { count: evidenceCount })}</Alert>
                  </div>
                ))}
            </div>
            {targetGatewayMode === "ENFORCE" && loadErrors.decisions && <Alert tone="error">{loadErrors.decisions}</Alert>}
            <div className="v2-row v2-governance-actions">
              <Button
                kind="primary"
                disabled={modeBlockers.length > 0}
                onClick={() => setConfirmAction(targetGatewayMode === "ENFORCE" ? "enforce" : "logOnly")}
                testId="v2-governance-apply-mode"
              >
                {t("v2.governance.applyMode")}
              </Button>
              {modeBlockers.length > 0 && (
                <span className="v2-governance-blockers" data-testid="gateway-mode-blockers">
                  {t("governance.detail.modeBlockedPrefix")} {modeBlockers.map((key) => t(`governance.blockers.${key}`)).join(" · ")}
                </span>
              )}
            </div>
          </>
        ) : (
          <>
            {danglingEngine && (
              <Alert tone="error">
                {t("governance.detail.danglingEngine")} <span className="mono">{danglingEngine.arn}</span>
              </Alert>
            )}
            <div className="v2-form cols-2">
              <Field label={t("v2.governance.initialMode")} hint={t("governance.detail.initialGatewayModeHelp")}>
                <div>
                  <Segmented
                    value={initialEngineMode}
                    onChange={setInitialEngineMode}
                    options={(["ENFORCE", "LOG_ONLY"] as GovernanceGatewayMode[]).map((mode) => ({ value: mode, label: mode }))}
                  />
                </div>
              </Field>
              <Field label={t("v2.governance.authorizationModel")} hint={t(`governance.policyEditor.modelHelp.${authorizationModel}`)}>
                <select
                  className="v2-select"
                  value={authorizationModel}
                  onChange={(e) => setAuthorizationModel(e.target.value as GovernanceAuthorizationModel)}
                >
                  <option value="allowlist">{t("v2.governance.model.allowlist")}</option>
                  <option value="preserve_traffic">{t("v2.governance.model.preserve_traffic")}</option>
                  <option value="custom">{t("v2.governance.model.custom")}</option>
                </select>
              </Field>
            </div>
            {initialEngineMode === "ENFORCE" && (
              <Alert tone="warn">
                <span>{t("governance.detail.initialEnforceWarning")}</span>
              </Alert>
            )}
            {authorizationModel === "preserve_traffic" && (
              <Alert tone="error">
                <label className="v2-check">
                  <input
                    type="checkbox"
                    checked={highRiskAcknowledged}
                    onChange={(e) => setHighRiskAcknowledged(e.target.checked)}
                  />
                  {t("governance.models.highRiskAck")}
                </label>
              </Alert>
            )}
            <div className="v2-row v2-governance-actions">
              <Button kind="primary" disabled={engineBlocked} onClick={() => setConfirmAction("engine")} testId="v2-governance-attach-engine">
                <Plus size={14} aria-hidden="true" />
                {t("v2.governance.createAttachEngine", { mode: initialEngineMode })}
              </Button>
              {!gateway.managed && <span className="v2-governance-blockers">{t("governance.blockers.notManaged")}</span>}
            </div>
          </>
        )}
      </Card>
    </>
  );

  /* ---------------- policies ---------------- */
  const enforcedActive = (policy: GovernancePolicy) =>
    policy.enforcement_mode === "ACTIVE" && gateway.policy_engine?.mode === "ENFORCE";
  const policyColumns: Column<GovernancePolicy>[] = [
    {
      key: "name",
      title: t("v2.governance.colPolicy"),
      render: (policy) => (
        <>
          <LinkButton
            disabled={!gateway.managed || operationBusy}
            onClick={() => setParams({ view: "policy", gateway: gateway.id, policy: policy.id })}
            testId={`v2-governance-policy-${policy.id}`}
          >
            {policy.name}
          </LinkButton>
          <span className="sub mono">ID: {policy.id}</span>
        </>
      ),
    },
    {
      key: "status",
      title: t("v2.governance.colStatus"),
      render: (policy) => <StatusTag status={policy.status} title={policy.status_reasons.join("; ") || undefined} />,
    },
    { key: "mode", title: t("v2.governance.colPolicyMode"), render: (policy) => <StatusTag status={policy.enforcement_mode} /> },
    {
      key: "relation",
      title: t("v2.governance.colRelation"),
      render: (policy) => (
        <span className="mono">
          {policy.candidate_for
            ? t("governance.detail.candidateFor", { id: policy.candidate_for })
            : policy.candidate_id
              ? t("governance.detail.hasCandidate", { id: policy.candidate_id })
              : "—"}
        </span>
      ),
    },
    { key: "updated", title: t("v2.governance.colUpdated"), className: "nowrap", render: (policy) => fmtTime(policy.updated_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (policy) => (
        <div className="v2-actions">
          <LinkButton
            disabled={!gateway.managed || operationBusy}
            onClick={() => setParams({ view: "policy", gateway: gateway.id, policy: policy.id })}
          >
            {t("v2.governance.review")}
          </LinkButton>
          <LinkButton
            danger
            disabled={!commonMutationReady || !sharedReady || enforcedActive(policy)}
            title={enforcedActive(policy) ? t("governance.detail.deleteEnforcedHint") : undefined}
            onClick={() => {
              setPolicyToDelete(policy);
              setConfirmAction("deletePolicy");
            }}
          >
            {t("v2.common.delete")}
          </LinkButton>
        </div>
      ),
    },
  ];
  const newPolicyBlocked = !commonMutationReady || !liveEngine || !sharedReady;
  const policiesSection = (
    <>
      <Card
        title={t("v2.governance.section.policies")}
        sub={t("governance.detail.policyModesSeparate")}
        end={
          <Button
            kind="primary"
            size="sm"
            disabled={newPolicyBlocked}
            title={
              !gateway.managed
                ? t("governance.blockers.notManaged")
                : !liveEngine
                  ? t("governance.blockers.noEngine")
                  : !sharedReady
                    ? t("governance.blockers.sharedAck")
                    : undefined
            }
            onClick={() => setParams({ view: "policy", gateway: gateway.id })}
            testId="v2-governance-new-policy"
          >
            <Plus size={14} aria-hidden="true" />
            {t("v2.governance.newPolicy")}
          </Button>
        }
        testId="v2-governance-policies"
      >
        {loadErrors.policies && <Alert tone="error">{loadErrors.policies}</Alert>}
        <Table
          columns={policyColumns}
          rows={policies?.policies ?? []}
          rowKey={(policy) => policy.id}
          loading={!policies && !loadErrors.policies}
          empty={liveEngine ? t("v2.governance.noPolicies") : t("governance.blockers.noEngine")}
        />
      </Card>
      {gateway.policy_test_available && <PolicyTestCard actions={gateway.actions} />}
    </>
  );

  /* ---------------- targets ---------------- */
  const targetSyncBlockers = (target: GovernanceGatewayTarget): string[] => {
    const blockers: string[] = [];
    if (!gateway.managed) blockers.push(t("governance.targetSync.blockers.notManaged"));
    if (target.not_synchronizable_reason === "not_mcp_server" && target.kind.protocol !== "mcp") {
      // same reason code; only the copy names the kind (HTTP passthrough, inference, …)
      blockers.push(
        t("governance.targetSync.blockers.not_mcp_server_kind", {
          kind: targetKind(t, (key) => i18n.exists(key), target.kind).label,
        }),
      );
    } else if (target.not_synchronizable_reason) {
      blockers.push(t(`governance.targetSync.blockers.${target.not_synchronizable_reason}`));
    }
    if (operationBusy || syncingTarget !== null) blockers.push(t("governance.targetSync.blockers.busy"));
    return blockers;
  };
  const targetColumns: Column<GovernanceGatewayTarget>[] = [
    {
      key: "name",
      title: t("v2.governance.colTarget"),
      render: (target) => (
        <>
          <b>{target.name}</b>
          <span className="sub mono">ID: {target.id}</span>
        </>
      ),
    },
    {
      key: "kind",
      title: t("v2.governance.colKind"),
      render: (target) => {
        const kind = targetKind(t, (key) => i18n.exists(key), target.kind);
        return (
          <span className={kind.known ? undefined : "mono"} data-testid={`gateway-target-kind-${target.id}`}>
            {kind.label}
          </span>
        );
      },
    },
    {
      key: "status",
      title: t("v2.governance.colStatus"),
      render: (target) => (
        <>
          <StatusTag status={target.status} />
          {(target.status === "SYNCHRONIZE_UNSUCCESSFUL" || target.status === "FAILED") &&
            target.status_reasons.length > 0 && <span className="sub v2-governance-err">{target.status_reasons.join("; ")}</span>}
        </>
      ),
    },
    { key: "listing", title: t("v2.governance.colListing"), render: (target) => <span className="mono">{target.listing_mode ?? "—"}</span> },
    { key: "sync", title: t("v2.governance.colLastSync"), className: "nowrap", render: (target) => fmtTime(target.last_synchronized_at) },
    {
      key: "actions",
      title: t("v2.governance.colActions"),
      render: (target) => {
        const actions = gateway.actions.filter((action) => action.target_id === target.id);
        if (actions.length === 0) return <span className="v2-muted">—</span>;
        return (
          <div className="v2-tags v2-governance-action-tags">
            {actions.map((action) => (
              <Tag key={action.name} tone={action.verified ? "green" : "orange"} title={action.description || undefined}>
                <span className="mono">{action.name}</span>
                {!action.verified && ` · ${t("v2.governance.unverified")}`}
              </Tag>
            ))}
          </div>
        );
      },
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (target) => {
        const blockers = targetSyncBlockers(target);
        const syncing = syncingTarget === target.id || target.status === "SYNCHRONIZING";
        return (
          <div className="v2-actions">
            <LinkButton
              disabled={blockers.length > 0}
              title={blockers.length > 0 ? blockers.join("; ") : t("governance.targetSync.hint")}
              onClick={() => void synchronizeTarget(target)}
              testId={`v2-governance-sync-${target.id}`}
            >
              <span className="v2-row" style={{ gap: 4 }}>
                <RotateCw size={13} aria-hidden="true" />
                {syncing ? t("v2.governance.syncing") : t("v2.governance.sync")}
              </span>
            </LinkButton>
          </div>
        );
      },
    },
  ];
  const targetsSection = (
    <Card title={t("v2.governance.targets")} testId="v2-governance-targets">
      <Table
        columns={targetColumns}
        rows={gateway.targets}
        rowKey={(target) => target.id}
        empty={t("v2.governance.noTargets")}
      />
      {(gateway.actions_uncovered_targets ?? []).length > 0 && (
        <div style={{ marginTop: 12 }} data-testid="gateway-actions-uncovered">
          <Alert>
            {t("governance.detail.noToolSchema", {
              total: gateway.actions_uncovered_targets.length,
              names: gateway.actions_uncovered_targets.join(", "),
            })}
          </Alert>
        </div>
      )}
      {gateway.external_tools_list_command && (
        <>
          <div className="v2-sub-title">{t("v2.governance.externalDiscovery")}</div>
          <pre className="v2-pre">{gateway.external_tools_list_command}</pre>
          <div className="v2-row v2-governance-actions">
            <Button size="sm" onClick={() => copy(gateway.external_tools_list_command ?? "")}>
              {t("v2.governance.copy")}
            </Button>
          </div>
        </>
      )}
    </Card>
  );

  const counts: Partial<Record<GatewaySection, number | undefined>> = {
    policies: policyCount,
    targets: gateway.targets.length,
  };

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {gateway.name}
            <StatusTag status={gateway.status} />
            <Tag tone={gateway.managed ? "green" : "gray"} dot>
              {gateway.managed ? t("v2.governance.managed") : t("v2.governance.unmanaged")}
            </Tag>
          </span>
        }
        onBack={back}
        end={
          <>
            <Button disabled={refreshing} onClick={() => void load()} testId="v2-governance-detail-refresh">
              {t("v2.common.refresh")}
            </Button>
            {gateway.managed ? (
              <Button kind="danger" disabled={operationBusy} onClick={() => setConfirmAction("unmanage")} testId="v2-governance-unmanage">
                {t("v2.governance.unmanage")}
              </Button>
            ) : (
              <Button kind="primary" disabled={operationBusy} onClick={() => setConfirmAction("manage")} testId="v2-governance-manage">
                {t("v2.governance.manage")}
              </Button>
            )}
          </>
        }
      />
      <div className="v2-governance-sections">
        <SubTabs
          value={section}
          onChange={goSection}
          tabs={GATEWAY_SECTIONS.map((value) => ({
            value,
            label: counts[value] != null ? `${t(`v2.governance.section.${value}`)} (${counts[value]})` : t(`v2.governance.section.${value}`),
          }))}
        />
        <span className="mono v2-muted">{gateway.id}</span>
      </div>

      {!gateway.managed && <Alert tone="warn">{t("governance.policyEditor.unmanaged")}</Alert>}
      <OperationAlert operation={operation} />
      {(needsSharedAck || gateway.shared_engine) && (section === "overview" || section === "policies") && (
        <SharedEngineAck gateway={gateway} checked={sharedAcknowledged} onChange={setSharedAcknowledged} t={t} />
      )}

      {section === "overview" && overview}
      {section === "policies" && policiesSection}
      {section === "targets" && targetsSection}
      {section === "rateLimits" && <RateLimitsSection gateway={gateway} operationBusy={operationBusy} refreshTick={refreshTick} />}
      {section === "decisions" && <DecisionsSection gateway={gateway} policies={policies} refreshTick={refreshTick} />}
      {section === "audit" && <AuditSection gateway={gateway} refreshTick={refreshTick} />}

      <Confirm
        open={confirmAction !== null}
        title={t(`v2.governance.confirmTitle.${confirmAction ?? "manage"}`)}
        body={confirmBody}
        confirmLabel={t("v2.common.confirm")}
        danger={confirmAction !== null && DANGER.includes(confirmAction)}
        busy={busy !== null}
        onConfirm={runConfirmedAction}
        onClose={() => {
          setConfirmAction(null);
          setPolicyToDelete(null);
        }}
      />
    </>
  );
}
