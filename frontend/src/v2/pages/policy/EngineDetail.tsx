import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, ApiError, type PolicyEngine } from "../../../lib/api";
import { governanceError } from "../../../lib/governance";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Confirm, FlowHeader, LinkButton, Spin, SubTabs, Tag } from "../../ui";
import { AttachGatewaysTab } from "./AttachGateways";
import { AuditTab } from "./Audit";
import { ENGINE_TABS, type EngineTab, POLICY_PATH } from "./common";
import { DecisionsTab } from "./Decisions";
import { PoliciesTab } from "./PolicyList";
import { PolicySimulator } from "./PolicySimulator";

/**
 * One 策略引擎: its 策略 (list / lifecycle / per-policy 生效 vs 仅记录), the 模拟器 (call_tool
 * against one gateway), the 决策 timeline (observability, per gateway) and 附加到网关.
 */
export function EngineDetail({ engineId, tab }: { engineId: string; tab: EngineTab }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { can } = useAuth();
  const manage = can("policy.manage");
  const [engine, setEngine] = useState<PolicyEngine | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(true);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const load = useCallback(async () => {
    setRefreshing(true);
    setError(null);
    try {
      setEngine(await api.getPolicyEngine(engineId));
    } catch (err) {
      setError(governanceError(err));
    } finally {
      setRefreshing(false);
    }
  }, [engineId]);
  useEffect(() => {
    void load();
  }, [load]);

  // the policies list and the entity catalog are shared by the 策略 / 模拟器 / 决策 tabs
  const list = useLoad(() => api.listEnginePolicies(engineId), `engine-policies:${engineId}`);
  const entities = useLoad(() => api.policyEntities(engineId), `policy-entities:${engineId}`);
  const reloadAll = () => {
    void load();
    list.reload();
  };

  const remove = async (force: boolean) => {
    setBusy(true);
    try {
      await api.deletePolicyEngine(engineId, force);
      toast("success", t("v2.policy.messages.engineDeleted", { name: engine?.name ?? engineId }));
      navigate(POLICY_PATH);
    } catch (err) {
      if (!force && err instanceof ApiError && err.code === "policy_engine.has_policies") {
        // the engine still owns policies: offer the forced delete (retires them)
        setBusy(false);
        setForceOpen(true);
        setDeleteOpen(false);
        return;
      }
      toast("error", governanceError(err));
    } finally {
      setBusy(false);
      setDeleteOpen(false);
    }
  };
  const [forceOpen, setForceOpen] = useState(false);

  const back = () => navigate(POLICY_PATH);
  if (error && !engine) {
    return (
      <>
        <FlowHeader title={engineId} onBack={back} />
        <Alert tone="error" action={<LinkButton onClick={() => void load()} testId="v2-policy-engine-retry">{t("v2.common.retry")}</LinkButton>}>
          {error}
        </Alert>
      </>
    );
  }
  if (!engine) {
    return (
      <>
        <FlowHeader title={engineId} onBack={back} />
        <Spin />
      </>
    );
  }
  const attached = engine.attachments.length;
  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            <span className="mono">{engine.name}</span>
            <Tag tone={attached > 0 ? "green" : "gray"} dot testId="v2-policy-engine-attached">
              {attached > 0 ? t("v2.policy.attachedCount", { count: attached }) : t("v2.policy.notAttached")}
            </Tag>
          </span>
        }
        onBack={back}
        end={
          <>
            <Button disabled={refreshing} onClick={reloadAll} testId="v2-policy-engine-refresh">
              {t("v2.common.refresh")}
            </Button>
            <Button kind="danger" disabled={!manage || busy || attached > 0} title={attached > 0 ? t("v2.policy.deleteBlockedAttached") : undefined} onClick={() => setDeleteOpen(true)} testId="v2-policy-engine-delete">
              {t("v2.common.delete")}
            </Button>
          </>
        }
      />
      {error && <Alert tone="error">{error}</Alert>}
      {engine.has_user_permit === false && (
        <Alert tone="warn">{t("v2.policy.noUserPermit")}</Alert>
      )}
      {engine.description && <div className="v2-muted v2-governance-note">{engine.description}</div>}
      <div className="v2-governance-sections">
        <SubTabs
          value={tab}
          onChange={(next) => setParams(next === "policies" ? { view: "engine", engine: engineId } : { view: "engine", engine: engineId, tab: next })}
          tabs={ENGINE_TABS.map((value) => ({
            value,
            label:
              value === "policies" && list.data
                ? `${t("v2.policy.tab.policies")} (${list.data.policies.length})`
                : value === "gateways"
                  ? `${t("v2.policy.tab.gateways")} (${attached})`
                  : t(`v2.policy.tab.${value}`),
          }))}
        />
        <span className="mono v2-muted">{engine.id}</span>
      </div>
      {tab === "policies" && (
        <PoliciesTab engine={engine} list={list.data} loading={list.loading} error={list.error} reload={reloadAll} />
      )}
      {tab === "simulate" && <PolicySimulator engine={engine} policies={list.data?.policies ?? []} />}
      {tab === "decisions" && <DecisionsTab engine={engine} entities={entities.data} policies={list.data?.policies ?? null} />}
      {tab === "audit" && <AuditTab engine={engine} />}
      {tab === "gateways" && <AttachGatewaysTab engine={engine} onChanged={() => void load()} />}
      <Confirm
        open={deleteOpen}
        title={t("v2.policy.deleteEngineTitle")}
        body={t("v2.policy.deleteEngineBody", { name: engine.name })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove(false)}
        onClose={() => setDeleteOpen(false)}
      />
      <Confirm
        open={forceOpen}
        title={t("v2.policy.deleteEngineTitle")}
        body={t("v2.policy.deleteEngineForceBody", { name: engine.name, count: engine.policy_count })}
        confirmLabel={t("v2.policy.deleteEngineForce")}
        danger
        busy={busy}
        onConfirm={() => void remove(true)}
        onClose={() => setForceOpen(false)}
      />
    </>
  );
}
