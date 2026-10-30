import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { PageHeader, SubTabs } from "../ui";
import { ConnectionList } from "./connections/ConnectionList";
import { GatewayTargets } from "./connections/GatewayTargets";
import { MyGrants } from "./connections/MyGrants";
import "./connections/connections.css";

type Tab = "connections" | "targets" | "mine";
const TABS: Tab[] = ["connections", "targets", "mine"];

/**
 * 连接 — AgentCore Identity Connections (credential providers in the workspace
 * token vault), the Gateway targets bound to one, and the caller's own as_user
 * grants. Reads are open to members; create/delete need `identity.manage`.
 * Sub-pages are `?view=` states: (none) = Connections, `targets` = Gateway
 * targets, `mine` = My authorizations (the former `/v2/my-connections` page).
 */
export function V2Connections() {
  const { t } = useTranslation();
  const [params, setParams] = useSearchParams();
  const view = params.get("view");
  const tab: Tab = view === "targets" || view === "mine" ? view : "connections";
  return (
    <>
      <PageHeader
        title={t("v2.connections.title")}
        desc={t("v2.connections.desc")}
        tabs={
          <SubTabs
            value={tab}
            onChange={(next) => setParams(next === "connections" ? {} : { view: next })}
            tabs={TABS.map((value) => ({ value, label: t(`v2.connections.tab.${value}`) }))}
          />
        }
      />
      {tab === "targets" ? <GatewayTargets /> : tab === "mine" ? <MyGrants /> : <ConnectionList />}
    </>
  );
}
