import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { PageHeader, SubTabs } from "../ui";
import { ConnectionList } from "./connections/ConnectionList";
import { GatewayTargets } from "./connections/GatewayTargets";
import "./connections/connections.css";

type Tab = "connections" | "targets";

/**
 * 连接 — AgentCore Identity Connections (credential providers in the workspace
 * token vault) and the Gateway targets bound to one. Reads are open to members;
 * create/delete need `identity.manage`. Sub-pages are `?view=` states:
 * (none) = Connections, `targets` = Gateway targets.
 */
export function V2Connections() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const tab: Tab = params.get("view") === "targets" ? "targets" : "connections";
  return (
    <>
      <PageHeader
        title={t("v2.connections.title")}
        desc={t("v2.connections.desc")}
        tabs={
          <SubTabs
            value={tab}
            onChange={(next) => setParams(next === "targets" ? { view: "targets" } : {})}
            tabs={(["connections", "targets"] as Tab[]).map((value) => ({ value, label: t(`v2.connections.tab.${value}`) }))}
          />
        }
      />
      {tab === "targets" ? <GatewayTargets /> : <ConnectionList />}
    </>
  );
}
