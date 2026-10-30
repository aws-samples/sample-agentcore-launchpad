import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useSearchParams } from "react-router-dom";

import { api } from "../../lib/api";
import { governanceError } from "../../lib/governance";
import { Alert, FlowHeader, LinkButton, Spin } from "../ui";
import { EngineDetail } from "./policy/EngineDetail";
import { EngineList } from "./policy/EngineList";
import { PolicyEditor } from "./policy/PolicyEditor";
import { engineHref, isEngineTab, POLICY_PATH, policyEditorHref } from "./policy/common";
import "./governance/governance.css";

/**
 * 策略 — the Policy module with the Global AgentCore Policy structure: 策略引擎 (many per
 * workspace) own 策略; an engine attaches to one or more gateways with a mode; the gateway's
 * interceptor decides the tool calls that pass through it. Sub-pages are `?view=` states:
 * `engine&engine=<id>[&tab=policies|simulate|decisions|gateways]`, `policy&engine=<id>
 * [&policy=<pid>]`; `?policy=<pid>[&tab=decisions]` resolves a bare policy id (chat deny card,
 * decision rows) to its engine.
 */
export function V2Policy() {
  const [params] = useSearchParams();
  const view = params.get("view");
  const engineId = params.get("engine");
  const bare = params.get("policy");

  if (view === "engine" && engineId) {
    const raw = params.get("tab");
    return <EngineDetail key={engineId} engineId={engineId} tab={isEngineTab(raw) ? raw : "policies"} />;
  }
  if (view === "policy" && engineId) {
    const policyId = params.get("policy");
    return <PolicyEditor key={`${engineId}:${policyId ?? "new"}`} engineId={engineId} policyId={policyId} target={policyId ? null : params.get("target")} />;
  }
  if (!view && bare) {
    return <PolicyResolver policyId={bare} tab={params.get("tab")} />;
  }
  return <EngineList />;
}

/**
 * A link that knows a policy id but not its engine (the deny card carries `policy_id` only):
 * find the engine that owns it across the workspace's engines, then land on its editor — or
 * on the engine's 决策 tab when asked. Unknown id ⇒ the engine list with a note.
 */
function PolicyResolver({ policyId, tab }: { policyId: string; tab: string | null }) {
  const { t } = useTranslation();
  const [target, setTarget] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [missing, setMissing] = useState(false);
  useEffect(() => {
    let live = true;
    (async () => {
      try {
        const { engines } = await api.listPolicyEngines();
        for (const engine of engines) {
          const { policies } = await api.listEnginePolicies(engine.id);
          if (policies.some((p) => p.id === policyId)) {
            if (live) setTarget(tab === "decisions" ? engineHref(engine.id, "decisions") : policyEditorHref(engine.id, policyId));
            return;
          }
        }
        if (live) setMissing(true);
      } catch (err) {
        if (live) setError(governanceError(err));
      }
    })();
    return () => {
      live = false;
    };
  }, [policyId, tab]);
  if (target) return <Navigate to={target} replace />;
  if (error || missing) {
    return (
      <>
        <FlowHeader title={t("nav.policy")} onBack={() => setTarget(POLICY_PATH)} />
        <Alert tone={error ? "error" : "warn"} action={<LinkButton onClick={() => setTarget(POLICY_PATH)} testId="v2-policy-resolver-open">{t("v2.policy.openEngines")}</LinkButton>}>
          <span data-testid="v2-policy-resolver-missing">{error ?? t("v2.policy.policyNotFound", { id: policyId })}</span>
        </Alert>
      </>
    );
  }
  return <Spin />;
}
