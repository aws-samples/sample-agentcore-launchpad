import {
  ArrowLeft,
  Check,
  ClipboardCopy,
  FileUp,
  Pencil,
  Plus,
  RefreshCw,
  ShieldAlert,
  ShieldCheck,
  Trash2,
  TriangleAlert,
} from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, ConfirmDialog, DataTable, Panel, useToast } from "../../components";
import {
  api,
  type GovernanceAuthorizationModel,
  type GovernanceDecisionResponse,
  type GovernanceGatewayDetail,
  type GovernanceGatewayMode,
  type GovernanceOperation,
  type GovernancePolicy,
  type GovernancePolicyListResponse,
  type GovernanceRegistryPreview,
} from "../../lib/api";
import {
  governanceError,
  isGatewayReady,
  isOperationPending,
  statusTone,
  type GovernanceView,
} from "./types";
import { PolicyTestPanel } from "./PolicyTestPanel";

interface Props {
  gatewayId: string;
  onNavigate: (view: GovernanceView, gatewayId?: string, policyId?: string) => void;
}

type ConfirmAction =
  | "manage"
  | "unmanage"
  | "import"
  | "retire"
  | "engine"
  | "logOnly"
  | "enforce"
  | "deletePolicy";

interface LoadErrors {
  registry?: string;
  policies?: string;
  decisions?: string;
}

export function GatewayDetailView({ gatewayId, onNavigate }: Props) {
  const { t } = useTranslation();
  const toast = useToast();
  const [gateway, setGateway] = useState<GovernanceGatewayDetail | null>(null);
  const [policies, setPolicies] = useState<GovernancePolicyListResponse | null>(null);
  const [registry, setRegistry] = useState<GovernanceRegistryPreview | null>(null);
  const [evidence, setEvidence] = useState<GovernanceDecisionResponse | null>(null);
  const [fatalError, setFatalError] = useState<string | null>(null);
  const [loadErrors, setLoadErrors] = useState<LoadErrors>({});
  const [refreshing, setRefreshing] = useState(false);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  const [policyToDelete, setPolicyToDelete] = useState<GovernancePolicy | null>(null);
  const [selectedLegacy, setSelectedLegacy] = useState<string[]>([]);
  const [sharedAcknowledged, setSharedAcknowledged] = useState(false);
  const [confirmationName, setConfirmationName] = useState("");
  const [overrideReason, setOverrideReason] = useState("");
  const [authorizationModel, setAuthorizationModel] =
    useState<GovernanceAuthorizationModel>("allowlist");
  const [highRiskAcknowledged, setHighRiskAcknowledged] = useState(false);
  const [operation, setOperation] = useState<GovernanceOperation | null>(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    setFatalError(null);
    const [gatewayResult, policyResult, registryResult, decisionResult] =
      await Promise.allSettled([
        api.getGovernanceGateway(gatewayId),
        api.listGovernancePolicies(gatewayId),
        api.governanceRegistryPreview(gatewayId),
        api.governanceDecisions(gatewayId, "24h"),
      ]);

    if (gatewayResult.status === "fulfilled") {
      setGateway(gatewayResult.value);
    } else {
      setFatalError(governanceError(gatewayResult.reason));
    }
    if (policyResult.status === "fulfilled") setPolicies(policyResult.value);
    if (registryResult.status === "fulfilled") {
      setRegistry(registryResult.value);
      setSelectedLegacy((current) =>
        current.filter((id) =>
          registryResult.value.legacy_records.some((record) => record.record_id === id),
        ),
      );
    }
    if (decisionResult.status === "fulfilled") setEvidence(decisionResult.value);
    setLoadErrors({
      policies:
        policyResult.status === "rejected" ? governanceError(policyResult.reason) : undefined,
      registry:
        registryResult.status === "rejected"
          ? governanceError(registryResult.reason)
          : undefined,
      decisions:
        decisionResult.status === "rejected"
          ? governanceError(decisionResult.reason)
          : undefined,
    });
    setRefreshing(false);
  }, [gatewayId]);

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
        .catch((error) => toast(governanceError(error), "crit"));
    }, 2500);
    return () => window.clearTimeout(timer);
  }, [load, operation, toast]);

  const sharedIds = useMemo(
    () => gateway?.shared_gateways.map((item) => item.id) ?? [],
    [gateway],
  );
  const needsSharedAck = (gateway?.shared_gateways.length ?? 0) > 1;
  const operationBusy = busy !== null || isOperationPending(operation);
  const evidenceCount = evidence?.decisions.length ?? 0;
  const hasOverride = confirmationName === gateway?.name && overrideReason.trim().length > 0;
  const engineReady = gateway?.policy_engine?.status.toUpperCase() === "ACTIVE";
  const iamPass = gateway?.iam_preflight?.status === "pass";
  const commonMutationReady =
    !!gateway && gateway.managed && isGatewayReady(gateway) && !operationBusy;
  const sharedReady = !needsSharedAck || sharedAcknowledged;
  const enforceReady =
    commonMutationReady &&
    engineReady &&
    iamPass &&
    sharedReady &&
    confirmationName === gateway?.name &&
    (evidenceCount > 0 || hasOverride);

  const mutationEnvelope = () => ({
    expected_gateway_updated_at: gateway?.updated_at,
    acknowledged_gateway_ids: needsSharedAck ? sharedIds : [],
    confirmation_name: confirmationName || null,
    override_reason: evidenceCount === 0 ? overrideReason.trim() || null : null,
  });

  const finishMutation = useCallback(
    async (label: string, action: () => Promise<GovernanceOperation | unknown>) => {
      setBusy(label);
      try {
        const result = await action();
        if (result && typeof result === "object" && "operation" in result) {
          setOperation(result as GovernanceOperation);
        } else {
          await load();
        }
        toast(t("governance.messages.requestAccepted"), "good");
      } catch (error) {
        toast(governanceError(error), "crit");
      } finally {
        setBusy(null);
        setConfirmAction(null);
      }
    },
    [load, t, toast],
  );

  const runConfirmedAction = () => {
    if (!gateway || !confirmAction) return;
    if (confirmAction === "manage") {
      void finishMutation("manage", () => api.manageGovernanceGateway(gateway.id));
      return;
    }
    if (confirmAction === "unmanage") {
      void finishMutation("unmanage", () => api.unmanageGovernanceGateway(gateway.id));
      return;
    }
    if (confirmAction === "import") {
      void finishMutation("import", () =>
        api.importGovernanceRegistry(gateway.id, {
          ...mutationEnvelope(),
          record_name: registry?.proposed.name ?? gateway.name,
          apply_update: registry?.outcome === "changed",
        }),
      );
      return;
    }
    if (confirmAction === "retire") {
      void finishMutation("retire", () =>
        api.retireGovernanceLegacyRecords(gateway.id, {
          ...mutationEnvelope(),
          record_ids: selectedLegacy,
        }),
      );
      return;
    }
    if (confirmAction === "deletePolicy") {
      if (!policyToDelete) return;
      const policy = policyToDelete;
      void finishMutation("deletePolicy", () =>
        api.deleteGovernancePolicy(gateway.id, policy.id, {
          ...mutationEnvelope(),
          expected_policy_updated_at: policy.updated_at,
        }),
      );
      return;
    }
    if (confirmAction === "engine") {
      void finishMutation("engine", () =>
        api.attachGovernanceEngine(gateway.id, {
          ...mutationEnvelope(),
          authorization_model: authorizationModel,
          high_risk_acknowledged: highRiskAcknowledged,
        }),
      );
      return;
    }
    const mode: GovernanceGatewayMode =
      confirmAction === "enforce" ? "ENFORCE" : "LOG_ONLY";
    void finishMutation("mode", () =>
      api.setGovernanceGatewayMode(gateway.id, {
        ...mutationEnvelope(),
        mode,
        evidence_range: "24h",
      }),
    );
  };

  if (fatalError || (!gateway && !refreshing)) {
    return (
      <Panel brk>
        <div className="gov-state gov-state-error">
          <TriangleAlert size={20} aria-hidden="true" />
          <strong>{t("governance.states.unavailable")}</strong>
          <span>{fatalError ?? t("governance.states.noData")}</span>
          <Btn onClick={() => onNavigate("gateways")}>
            <ArrowLeft size={14} aria-hidden="true" />
            {t("governance.actions.back")}
          </Btn>
        </div>
      </Panel>
    );
  }

  if (!gateway) {
    return <div className="loading-line">{t("common.loading")}</div>;
  }

  const registryApproved = registry?.exact_record?.status === "APPROVED";
  const confirmBody = t(`governance.confirm.${confirmAction ?? "manage"}`, {
    name: gateway.name,
    engine: gateway.policy_engine?.name ?? t("governance.states.newEngine"),
    count: selectedLegacy.length,
    gateways: gateway.shared_gateways.map((item) => item.name).join(", "),
    policy: policyToDelete?.name ?? "",
  });

  return (
    <>
      <div className="gov-toolbar">
        <Btn onClick={() => onNavigate("gateways")}>
          <ArrowLeft size={14} aria-hidden="true" />
          {t("governance.actions.back")}
        </Btn>
        <div className="gov-toolbar-title">
          <strong>{gateway.name}</strong>
          <span className="mono">{gateway.id}</span>
        </div>
        <Chip tone={statusTone(gateway.status)}>{gateway.status}</Chip>
        <Chip tone={gateway.managed ? "good" : "muted"}>
          {gateway.managed
            ? t("governance.states.managed")
            : t("governance.states.unmanaged")}
        </Chip>
        <Btn disabled={refreshing} onClick={() => void load()}>
          <RefreshCw size={14} aria-hidden="true" />
          {t("governance.actions.refresh")}
        </Btn>
      </div>

      <div className="gov-grid gov-detail-grid">
        <Panel title={t("governance.detail.identity")} brk>
          <div className="gov-kv-list">
            <div className="kv">
              <span className="k">ARN</span>
              <span className="v gov-break">{gateway.arn}</span>
            </div>
            <div className="kv">
              <span className="k">{t("governance.detail.endpoint")}</span>
              <span className="v gov-break">{gateway.url ?? "-"}</span>
            </div>
            <div className="kv">
              <span className="k">{t("governance.detail.authorizer")}</span>
              <span className="v">{gateway.authorizer_type}</span>
            </div>
            <div className="kv">
              <span className="k">{t("governance.detail.role")}</span>
              <span className="v gov-break">{gateway.role_arn ?? "-"}</span>
            </div>
            <div className="kv">
              <span className="k">{t("governance.detail.updated")}</span>
              <span className="v">{gateway.updated_at ?? "-"}</span>
            </div>
          </div>
          {gateway.status_reasons.length > 0 ? (
            <div className="gov-alert gov-alert-error">{gateway.status_reasons.join("; ")}</div>
          ) : null}
          <div className="gov-actions">
            {gateway.managed ? (
              <Btn
                disabled={operationBusy}
                onClick={() => setConfirmAction("unmanage")}
              >
                <Trash2 size={14} aria-hidden="true" />
                {t("governance.actions.unmanage")}
              </Btn>
            ) : (
              <Btn
                primary
                disabled={operationBusy}
                onClick={() => setConfirmAction("manage")}
              >
                <ShieldCheck size={14} aria-hidden="true" />
                {t("governance.actions.manage")}
              </Btn>
            )}
          </div>
        </Panel>

        <Panel
          title={t("governance.detail.registry")}
          sub={t("governance.detail.catalogSeparate")}
          end={
            gateway.attachability.attachable ? (
              <Chip tone="good">{t("governance.states.attachable")}</Chip>
            ) : (
              <Chip tone="warn">{t("governance.states.catalogOnly")}</Chip>
            )
          }
        >
          {loadErrors.registry ? (
            <div className="gov-inline-error">{loadErrors.registry}</div>
          ) : registry ? (
            <>
              <div className="gov-kv-list">
                <div className="kv">
                  <span className="k">{t("governance.detail.previewOutcome")}</span>
                  <span className="v">{registry.outcome}</span>
                </div>
                <div className="kv">
                  <span className="k">{t("governance.detail.record")}</span>
                  <span className="v">
                    {registry.exact_record
                      ? `${registry.exact_record.name} / ${registry.exact_record.status}`
                      : t("governance.states.notCataloged")}
                  </span>
                </div>
                <div className="kv">
                  <span className="k">{t("governance.detail.legacy")}</span>
                  <span className="v">{registry.legacy_records.length}</span>
                </div>
              </div>
              {registry.name_conflict ? (
                <div className="gov-alert gov-alert-error">
                  <TriangleAlert size={15} aria-hidden="true" />
                  {t("governance.detail.registryConflict", {
                    name: registry.name_conflict.name,
                  })}
                </div>
              ) : null}
              <div className="gov-actions">
                <Btn
                  primary
                  disabled={
                    !commonMutationReady ||
                    !!registry.name_conflict ||
                    (registry.outcome === "reused" &&
                      registry.exact_record?.status !== "DRAFT")
                  }
                  onClick={() => setConfirmAction("import")}
                >
                  <FileUp size={14} aria-hidden="true" />
                  {registry.outcome === "changed"
                    ? t("governance.actions.syncRegistry")
                    : t("governance.actions.importRegistry")}
                </Btn>
              </div>
              {registry.legacy_records.length > 0 ? (
                <div className="gov-legacy-list">
                  {registry.legacy_records.map((record) => (
                    <label key={record.record_id} className="gov-check-row">
                      <input
                        type="checkbox"
                        checked={selectedLegacy.includes(record.record_id)}
                        onChange={(event) =>
                          setSelectedLegacy((current) =>
                            event.target.checked
                              ? [...current, record.record_id]
                              : current.filter((id) => id !== record.record_id),
                          )
                        }
                      />
                      <span>{record.name}</span>
                      <Chip tone={statusTone(record.status)}>{record.status}</Chip>
                    </label>
                  ))}
                  <Btn
                    disabled={
                      !commonMutationReady ||
                      !registryApproved ||
                      selectedLegacy.length === 0
                    }
                    onClick={() => setConfirmAction("retire")}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                    {t("governance.actions.retireLegacy")}
                  </Btn>
                </div>
              ) : null}
            </>
          ) : (
            <div className="empty">{t("governance.states.noData")}</div>
          )}
          {!gateway.attachability.attachable ? (
            <div className="gov-alert">
              <ShieldAlert size={15} aria-hidden="true" />
              {gateway.attachability.reason ?? t("governance.states.authUnresolved")}
            </div>
          ) : null}
        </Panel>
      </div>

      <div className="gov-grid gov-detail-grid">
        <Panel
          title={t("governance.detail.engine")}
          end={
            gateway.policy_engine ? (
              <Chip tone={statusTone(gateway.policy_engine.mode)}>
                {gateway.policy_engine.mode ?? "-"}
              </Chip>
            ) : (
              <Chip tone="muted">{t("governance.states.notAttached")}</Chip>
            )
          }
        >
          {gateway.policy_engine ? (
            <div className="gov-kv-list">
              <div className="kv">
                <span className="k">{t("governance.detail.engineName")}</span>
                <span className="v">{gateway.policy_engine.name}</span>
              </div>
              <div className="kv">
                <span className="k">{t("governance.detail.engineStatus")}</span>
                <span className="v">{gateway.policy_engine.status}</span>
              </div>
              <div className="kv">
                <span className="k">{t("governance.detail.gatewayMode")}</span>
                <span className="v">{gateway.policy_engine.mode ?? "-"}</span>
              </div>
              <div className="kv">
                <span className="k">{t("governance.detail.policyCount")}</span>
                <span className="v">{policies?.policies.length ?? "-"}</span>
              </div>
            </div>
          ) : (
            <div className="gov-form-stack">
              <div className="field">
                <label>{t("governance.policyEditor.authorizationModel")}</label>
                <select
                  className="input"
                  value={authorizationModel}
                  onChange={(event) =>
                    setAuthorizationModel(event.target.value as GovernanceAuthorizationModel)
                  }
                >
                  <option value="allowlist">{t("governance.models.allowlist")}</option>
                  <option value="preserve_traffic">
                    {t("governance.models.preserveTraffic")}
                  </option>
                  <option value="custom">{t("governance.models.custom")}</option>
                </select>
              </div>
              {authorizationModel === "preserve_traffic" ? (
                <label className="gov-check-row gov-alert gov-alert-error">
                  <input
                    type="checkbox"
                    checked={highRiskAcknowledged}
                    onChange={(event) => setHighRiskAcknowledged(event.target.checked)}
                  />
                  <span>{t("governance.models.highRiskAck")}</span>
                </label>
              ) : null}
              <Btn
                primary
                disabled={
                  !commonMutationReady ||
                  !sharedReady ||
                  (authorizationModel === "preserve_traffic" && !highRiskAcknowledged)
                }
                onClick={() => setConfirmAction("engine")}
              >
                <Plus size={14} aria-hidden="true" />
                {t("governance.actions.createAttachEngine")}
              </Btn>
            </div>
          )}

          {gateway.shared_engine ? (
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

          {gateway.policy_engine ? (
            <div className="gov-rollout">
              <div className="gov-mode-control">
                <button
                  type="button"
                  className={`selchip${gateway.policy_engine.mode === "LOG_ONLY" ? " on" : ""}`}
                  disabled={
                    !commonMutationReady || gateway.policy_engine.mode === "LOG_ONLY"
                  }
                  onClick={() => setConfirmAction("logOnly")}
                >
                  LOG_ONLY
                </button>
                <button
                  type="button"
                  className={`selchip${gateway.policy_engine.mode === "ENFORCE" ? " on" : ""}`}
                  disabled={!enforceReady || gateway.policy_engine.mode === "ENFORCE"}
                  onClick={() => setConfirmAction("enforce")}
                >
                  ENFORCE
                </button>
              </div>
              <div className="field">
                <label>{t("governance.detail.confirmGatewayName")}</label>
                <input
                  className="input mono"
                  value={confirmationName}
                  onChange={(event) => setConfirmationName(event.target.value)}
                  placeholder={gateway.name}
                />
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
                </div>
              ) : (
                <div className="gov-alert gov-alert-good">
                  <Check size={15} aria-hidden="true" />
                  {t("governance.detail.evidenceReady", { count: evidenceCount })}
                </div>
              )}
              {loadErrors.decisions ? (
                <div className="gov-inline-error">{loadErrors.decisions}</div>
              ) : null}
            </div>
          ) : null}
        </Panel>

        <Panel
          title={t("governance.detail.iam")}
          end={
            gateway.iam_preflight ? (
              <Chip tone={statusTone(gateway.iam_preflight.status)}>
                {gateway.iam_preflight.status.toUpperCase()}
              </Chip>
            ) : (
              <Chip tone="muted">{t("governance.states.notAvailable")}</Chip>
            )
          }
        >
          {gateway.iam_preflight ? (
            <>
              {gateway.iam_preflight.status !== "pass" ? (
                <div className="gov-alert gov-alert-error">
                  <ShieldAlert size={15} aria-hidden="true" />
                  <span>
                    {gateway.iam_preflight.reason}
                    {gateway.iam_preflight.operator_error
                      ? ` / ${gateway.iam_preflight.operator_error}`
                      : ""}
                  </span>
                </div>
              ) : (
                <div className="gov-alert gov-alert-good">
                  <ShieldCheck size={15} aria-hidden="true" />
                  {t("governance.detail.iamPass")}
                </div>
              )}
              <div className="code gov-code-wrap">
                {JSON.stringify(gateway.iam_preflight.remediation, null, 2)}
              </div>
              <Btn
                title={t("governance.actions.copy")}
                onClick={() => {
                  void navigator.clipboard.writeText(
                    JSON.stringify(gateway.iam_preflight?.remediation ?? {}, null, 2),
                  );
                  toast(t("governance.messages.copied"), "good");
                }}
              >
                <ClipboardCopy size={14} aria-hidden="true" />
                {t("governance.actions.copy")}
              </Btn>
            </>
          ) : (
            <div className="empty">{t("governance.detail.iamAfterEngine")}</div>
          )}
        </Panel>
      </div>

      <Panel
        title={t("governance.detail.policies")}
        sub={loadErrors.policies ?? t("governance.detail.policyModesSeparate")}
        pad={false}
        end={
          <Btn
            primary
            disabled={!commonMutationReady || !gateway.policy_engine || !sharedReady}
            onClick={() => onNavigate("policy", gateway.id)}
          >
            <Plus size={14} aria-hidden="true" />
            {t("governance.actions.newPolicy")}
          </Btn>
        }
      >
        <DataTable
          columns={[
            { key: "name", label: t("governance.policyEditor.name") },
            { key: "status", label: t("governance.inventory.status") },
            { key: "mode", label: t("governance.detail.policyMode") },
            { key: "relation", label: t("governance.detail.relation") },
            { key: "action", label: "" },
          ]}
          isEmpty={!loadErrors.policies && (policies?.policies.length ?? 0) === 0}
          empty={t("governance.detail.noPolicies")}
        >
          {policies?.policies.map((policy) => (
            <tr key={policy.id}>
              <td className="pri">
                {policy.name}
                <div className="gov-cell-note mono">{policy.id}</div>
              </td>
              <td>
                <Chip tone={statusTone(policy.status)}>{policy.status}</Chip>
              </td>
              <td>
                <Chip tone={statusTone(policy.enforcement_mode)}>
                  {policy.enforcement_mode}
                </Chip>
              </td>
              <td className="mono">
                {policy.candidate_for
                  ? t("governance.detail.candidateFor", {
                      id: policy.candidate_for,
                    })
                  : policy.candidate_id
                    ? t("governance.detail.hasCandidate", {
                        id: policy.candidate_id,
                      })
                    : "-"}
              </td>
              <td>
                <div className="gov-action-list">
                  <Btn
                    disabled={!gateway.managed || operationBusy}
                    onClick={() => onNavigate("policy", gateway.id, policy.id)}
                  >
                    <Pencil size={14} aria-hidden="true" />
                    {t("governance.actions.review")}
                  </Btn>
                  <Btn
                    disabled={
                      !commonMutationReady ||
                      !sharedReady ||
                      (policy.enforcement_mode === "ACTIVE" &&
                        gateway.policy_engine?.mode === "ENFORCE")
                    }
                    title={
                      policy.enforcement_mode === "ACTIVE" &&
                      gateway.policy_engine?.mode === "ENFORCE"
                        ? t("governance.detail.deleteEnforcedHint")
                        : undefined
                    }
                    onClick={() => {
                      setPolicyToDelete(policy);
                      setConfirmAction("deletePolicy");
                    }}
                  >
                    <Trash2 size={14} aria-hidden="true" />
                    {t("governance.actions.deletePolicy")}
                  </Btn>
                </div>
              </td>
            </tr>
          ))}
        </DataTable>
      </Panel>

      {gateway.policy_test_available ? (
        <PolicyTestPanel actions={gateway.actions} />
      ) : null}

      <Panel title={t("governance.detail.targets")} pad={false}>
        <DataTable
          columns={[
            { key: "target", label: t("governance.inventory.targets") },
            { key: "status", label: t("governance.inventory.status") },
            { key: "listing", label: t("governance.detail.listingMode") },
            { key: "actions", label: t("governance.detail.actions") },
          ]}
          isEmpty={gateway.targets.length === 0}
          empty={t("governance.detail.noTargets")}
        >
          {gateway.targets.map((target) => {
            const actions = gateway.actions.filter((action) => action.target_id === target.id);
            return (
              <tr key={target.id}>
                <td className="pri">
                  {target.name}
                  <div className="gov-cell-note mono">{target.id}</div>
                </td>
                <td>
                  <Chip tone={statusTone(target.status)}>{target.status}</Chip>
                </td>
                <td className="mono">{target.listing_mode ?? "-"}</td>
                <td>
                  <div className="gov-action-list">
                    {actions.map((action) => (
                      <Chip key={action.name} tone={action.verified ? "good" : "warn"}>
                        {action.name} /{" "}
                        {action.verified
                          ? t("governance.states.verified")
                          : t("governance.states.unverified")}
                      </Chip>
                    ))}
                    {actions.length === 0 ? "-" : null}
                  </div>
                </td>
              </tr>
            );
          })}
        </DataTable>
        {gateway.external_tools_list_command ? (
          <div className="gov-command">
            <div className="code gov-code-wrap">{gateway.external_tools_list_command}</div>
            <Btn
              title={t("governance.actions.copy")}
              onClick={() =>
                void navigator.clipboard.writeText(
                  gateway.external_tools_list_command ?? "",
                )
              }
            >
              <ClipboardCopy size={14} aria-hidden="true" />
              {t("governance.actions.copy")}
            </Btn>
          </div>
        ) : null}
      </Panel>

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
        title={t(`governance.confirmTitles.${confirmAction ?? "manage"}`)}
        body={confirmBody}
        confirmLabel={t("governance.actions.confirm")}
        onCancel={() => {
          setConfirmAction(null);
          setPolicyToDelete(null);
        }}
        onConfirm={runConfirmedAction}
      />
    </>
  );
}
