import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { PageHeader, SubTabs } from "../ui";
import { GATEWAY_SECTIONS, type GatewaySection, type GovernanceTab, isGovernanceTab } from "./governance/common";
import { GatewayDetail } from "./governance/GatewayDetail";
import { GatewayList } from "./governance/GatewayList";
import { RateLimitEditor } from "./governance/RateLimitEditor";
import { ToolsTab } from "./governance/Tools";
import "./governance/governance.css";

/**
 * Gateway — MCP Gateway lifecycle (management tags, targets + synchronization, rate limits,
 * the attached policy engine) and the tool catalog with the built-in Code Interpreter /
 * Browser demos. Sub-pages are `?view=` states: `gateway&gateway=&section=`,
 * `rate-limit&gateway=[&limit=]`. The Policy module is its own page (`/v2/policy`, Global
 * console structure).
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
  if (view === "rate-limit" && gatewayId) {
    const limitId = params.get("limit");
    return <RateLimitEditor key={`${gatewayId}:${limitId ?? "new"}`} gatewayId={gatewayId} limitId={limitId} />;
  }

  const requested = params.get("tab");
  const tab: GovernanceTab = isGovernanceTab(requested) ? requested : "gateways";
  const tabs: GovernanceTab[] = ["gateways", "tools"];
  return (
    <>
      <PageHeader
        title={t("nav.gateway")}
        desc={t(`v2.governance.tabDesc.${tab}`)}
        tabs={
          <SubTabs
            value={tab}
            onChange={(next) => setParams(next === "gateways" ? {} : { tab: next })}
            tabs={tabs.map((value) => ({ value, label: t(`v2.governance.tab.${value}`) }))}
          />
        }
      />
      {tab === "gateways" ? <GatewayList /> : <ToolsTab />}
    </>
  );
}
