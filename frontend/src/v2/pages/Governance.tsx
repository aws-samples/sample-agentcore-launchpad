import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { PageHeader, SubTabs } from "../ui";
import { GATEWAY_SECTIONS, type GatewaySection } from "./governance/common";
import { GatewayDetail } from "./governance/GatewayDetail";
import { GatewayList } from "./governance/GatewayList";
import { PolicyEditor } from "./governance/PolicyEditor";
import { RateLimitEditor } from "./governance/RateLimitEditor";
import { ToolsTab } from "./governance/Tools";
import "./governance/governance.css";

type Tab = "gateways" | "tools";
const TABS: Tab[] = ["gateways", "tools"];

/**
 * 治理 — MCP Gateway governance (management tags, Registry publication, Policy
 * Engine + Cedar policies, rate limits, decisions, audit) and the tool catalog
 * with the built-in Code Interpreter / Browser demos. Sub-pages are `?view=`
 * states: `gateway&gateway=&section=`, `policy&gateway=[&policy=]`,
 * `rate-limit&gateway=[&limit=]`.
 */
export function V2Governance() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const view = params.get("view");
  const gatewayId = params.get("gateway");

  if (view === "gateway" && gatewayId) {
    const raw = params.get("section") ?? "";
    const section: GatewaySection = (GATEWAY_SECTIONS as string[]).includes(raw) ? (raw as GatewaySection) : "overview";
    return <GatewayDetail key={gatewayId} gatewayId={gatewayId} section={section} />;
  }
  if (view === "policy" && gatewayId) {
    const policyId = params.get("policy");
    return <PolicyEditor key={`${gatewayId}:${policyId ?? "new"}`} gatewayId={gatewayId} policyId={policyId} />;
  }
  if (view === "rate-limit" && gatewayId) {
    const limitId = params.get("limit");
    return <RateLimitEditor key={`${gatewayId}:${limitId ?? "new"}`} gatewayId={gatewayId} limitId={limitId} />;
  }

  const tab: Tab = params.get("tab") === "tools" ? "tools" : "gateways";
  return (
    <>
      <PageHeader
        title={t("nav.governance")}
        desc={t(`v2.governance.tabDesc.${tab}`)}
        tabs={
          <SubTabs
            value={tab}
            onChange={(next) => setParams(next === "gateways" ? {} : { tab: next })}
            tabs={TABS.map((value) => ({ value, label: t(`v2.governance.tab.${value}`) }))}
          />
        }
      />
      {tab === "gateways" ? <GatewayList /> : <ToolsTab />}
    </>
  );
}
