import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { type AgentIdentityDownstream, api } from "../../../lib/api";
import { useAuth } from "../../../auth/auth-context";
import { useLoad } from "../../hooks";
import { Alert, Button, Card, type Column, Descriptions, FlowHeader, Spin, Table, Tag } from "../../ui";
import "../connections/connections.css";
import { InboundSwitchCard } from "./InboundAuthFields";

const WORKLOAD_TONE = { ready: "green", none: "gray", managed: "blue", not_deployed: "gray", missing: "red" } as const;
const CONNECTION_TONE = { ready: "green", missing: "red", unbound: "gray" } as const;

/**
 * 身份 — read-only: who this agent is (its workload identity), how callers reach it
 * (inbound), and every downstream it calls with the acting mode and Connection.
 * Everything is read back from AWS on open (`GET /api/agents/{id}/identity`).
 */
export function AgentIdentity({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const { can } = useAuth();
  const { data, loading, error, reload } = useLoad(() => api.agentIdentity(id), `identity:${id}`);

  const columns: Column<AgentIdentityDownstream>[] = [
    {
      key: "name",
      title: t("v2.agents.identity.colDownstream"),
      render: (row) => (
        <span className="v2-conn-name">
          <span className="mono">{row.name}</span>
          <span className="v2-muted">{t(`v2.agents.identity.type.${row.type}`)} · {row.tool_type}</span>
        </span>
      ),
    },
    {
      key: "via",
      title: t("v2.agents.identity.colVia"),
      render: (row) => t(`v2.agents.identity.via.${row.via}`),
    },
    {
      key: "mode",
      title: t("v2.agents.identity.colMode"),
      render: (row) => <Tag tone="blue">{t(`identity.mode.${row.mode}`, row.mode)}</Tag>,
    },
    {
      key: "connection",
      title: t("v2.agents.identity.colConnection"),
      render: (row) => (
        <span className="v2-identity-downstream">
          <span className="mono">{row.connection ?? "—"}</span>
          <Tag tone={CONNECTION_TONE[row.connection_status]} dot>
            {t(`v2.agents.identity.connection.${row.connection_status}`)}
          </Tag>
        </span>
      ),
    },
    {
      key: "scopes",
      title: t("v2.agents.identity.colScopes"),
      render: (row) => (row.scopes.length ? <span className="mono">{row.scopes.join(" ")}</span> : "—"),
    },
  ];

  const workload = data?.workload_identity;
  return (
    <>
      <FlowHeader
        title={t("v2.agents.identity.title", { name: data?.name ?? id })}
        onBack={() => setParams({ view: "detail", id })}
        end={
          <>
            <Button onClick={reload}>{t("v2.common.refresh")}</Button>
            <Link to="/v2/connections" className="v2-btn">
              {t("v2.agents.identity.manageConnections")}
            </Link>
          </>
        }
      />
      {error && <Alert tone="error">{error}</Alert>}
      {loading && !data && <Spin />}
      {data && workload && (
        <div data-testid="v2-agent-identity-page" className="v2-form">
          <Card title={t("v2.agents.identity.workloadTitle")} sub={t("v2.agents.identity.workloadSub")}>
            <Descriptions
              items={[
                {
                  label: t("v2.agents.identity.status"),
                  value: (
                    <Tag tone={WORKLOAD_TONE[workload.status]} dot>
                      {t(`v2.agents.identity.workload.${workload.status}`)}
                    </Tag>
                  ),
                },
                { label: t("v2.agents.identity.workloadName"), value: <span className="mono">{workload.name ?? "—"}</span> },
                { label: "ARN", value: <span className="mono">{workload.arn ?? "—"}</span> },
                {
                  label: t("v2.agents.identity.returnUrls"),
                  value: workload.allowed_return_urls.length ? (
                    <span className="mono">{workload.allowed_return_urls.join(", ")}</span>
                  ) : (
                    "—"
                  ),
                },
              ]}
            />
          </Card>
          <InboundSwitchCard
            agentId={id}
            mode={data.inbound.mode}
            pinned={data.inbound.pinned}
            capable={data.inbound.capable}
            canSwitch={can("agents.deploy")}
            busy={false}
            jwt={data.inbound.jwt}
            invokeUrl={data.inbound.invoke_url}
            onSwitched={() => setParams({ view: "detail", id })}
          />
          <Card title={t("v2.agents.identity.downstreamTitle")} sub={t("v2.agents.identity.downstreamSub")} flush>
            <Table
              columns={columns}
              rows={data.downstreams}
              rowKey={(row) => `${row.type}:${row.name}`}
              empty={t("v2.agents.identity.downstreamEmpty")}
            />
          </Card>
        </div>
      )}
    </>
  );
}
