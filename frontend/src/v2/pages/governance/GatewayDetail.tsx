import { RotateCw } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, type GatewayPolicyEngine, type GovernanceGatewayDetail, type GovernanceGatewayTarget } from "../../../lib/api";
import { governanceError, TARGET_SYNC_POLL_INTERVAL_MS, TARGET_SYNC_POLL_MS } from "../../../lib/governance";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  FlowHeader,
  LinkButton,
  Spin,
  SubTabs,
  Table,
  Tag,
} from "../../ui";
import { type GatewaySection, GATEWAY_SECTIONS, targetKind, useCopy } from "./common";
import { GatewayPolicyEngineSection } from "./GatewayPolicyEngine";
import { RateLimitsSection } from "./RateLimits";
import { StatusTag } from "./widgets";

type ConfirmAction = "manage" | "unmanage";

const DANGER: ConfirmAction[] = ["unmanage"];

/** One MCP Gateway: identity, targets + synchronization, rate limits, the attached policy engine. */
export function GatewayDetail({ gatewayId, section }: { gatewayId: string; section: GatewaySection }) {
  const { t, i18n } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const copy = useCopy(t);
  const [gateway, setGateway] = useState<GovernanceGatewayDetail | null>(null);
  const [fatalError, setFatalError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [busy, setBusy] = useState<string | null>(null);
  const [confirmAction, setConfirmAction] = useState<ConfirmAction | null>(null);
  // rate limits own their fetch; REFRESH reaches them through this tick
  const [refreshTick, setRefreshTick] = useState(0);
  // SynchronizeGatewayTargets answers 202 and the target sits in SYNCHRONIZING with no
  // operation row to poll, so the detail itself is re-fetched until every target settles
  const [syncingTarget, setSyncingTarget] = useState<string | null>(null);
  const [syncPollStartedAt, setSyncPollStartedAt] = useState<number | null>(null);
  // the gateway's policy engine attachment (Global `policyEngineConfiguration`) — read with the
  // detail so the header can name it; the section owns the attach / detach controls
  const [policyEngine, setPolicyEngine] = useState<GatewayPolicyEngine | null>(null);

  const load = useCallback(async () => {
    setRefreshing(true);
    setFatalError(null);
    setRefreshTick((tick) => tick + 1);
    try {
      setGateway(await api.getGovernanceGateway(gatewayId));
    } catch (error) {
      setFatalError(governanceError(error));
    } finally {
      setRefreshing(false);
    }
    // fail-soft: an unreadable attachment leaves the header silent, the section shows the error
    api.gatewayPolicyEngine(gatewayId).then(setPolicyEngine, () => setPolicyEngine(null));
  }, [gatewayId]);

  useEffect(() => {
    void load();
  }, [load]);

  const operationBusy = busy !== null;

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

  const finishMutation = async (label: string, action: () => Promise<unknown>) => {
    setBusy(label);
    try {
      await action();
      await load();
      toast("success", t("governance.messages.requestAccepted"));
    } catch (error) {
      toast("error", governanceError(error));
    } finally {
      setBusy(null);
      setConfirmAction(null);
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
    if (confirmAction === "manage") {
      void finishMutation("manage", () => api.manageGovernanceGateway(gateway.id));
      return;
    }
    void finishMutation("unmanage", () => api.unmanageGovernanceGateway(gateway.id));
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

  /* ---------------- overview ---------------- */
  const overview = (
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
            {policyEngine?.attached && (
              <Tag tone="blue" title={policyEngine.engine_id ?? undefined} testId="v2-governance-policy-engine-tag">
                {t("v2.policy.gateway.headerTag", { engine: policyEngine.engine_name ?? policyEngine.engine_id ?? "", mode: t(`v2.policy.attachMode.${(policyEngine.mode ?? "").toLowerCase()}`, { defaultValue: policyEngine.mode ?? "" }) })}
              </Tag>
            )}
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

      {section === "overview" && overview}
      {section === "targets" && targetsSection}
      {section === "rate-limits" && <RateLimitsSection gateway={gateway} operationBusy={operationBusy} refreshTick={refreshTick} />}
      {section === "policy-engine" && (
        <GatewayPolicyEngineSection gateway={gateway} current={policyEngine} refreshTick={refreshTick} onChanged={setPolicyEngine} />
      )}

      <Confirm
        open={confirmAction !== null}
        title={t(`v2.governance.confirmTitle.${confirmAction ?? "manage"}`)}
        body={t(`governance.confirm.${confirmAction ?? "manage"}`, { name: gateway.name })}
        confirmLabel={t("v2.common.confirm")}
        danger={confirmAction !== null && DANGER.includes(confirmAction)}
        busy={busy !== null}
        onConfirm={runConfirmedAction}
        onClose={() => setConfirmAction(null)}
      />
    </>
  );
}
