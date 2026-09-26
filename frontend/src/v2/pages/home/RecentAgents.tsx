import { ArrowRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { useNavigate } from "react-router-dom";

import { methodLabel } from "../../../components/methodChipMeta";
import type { AgentInfo } from "../../../lib/api";
import { fmtTime } from "../../format";
import { Button, Card, type Column, LinkButton, Table, Tag, type TagTone } from "../../ui";
import { stageSummary } from "./common";

const STATUS_TONE: Record<string, TagTone> = { active: "green", deploying: "blue", failed: "red", draft: "gray" };

/** 最近部署 — the newest agents with their deploy progress (the classic "deploy feed"). */
export function RecentAgents({
  agents,
  loading,
  error,
  onRetry,
}: {
  agents: AgentInfo[] | null;
  loading: boolean;
  error: string | null;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const open = (agent: AgentInfo) => navigate(`/v2/agents?view=detail&id=${encodeURIComponent(agent.id)}`);
  const rows = [...(agents ?? [])].sort((a, b) => (b.created_at ?? "").localeCompare(a.created_at ?? "")).slice(0, 6);
  const columns: Column<AgentInfo>[] = [
    {
      key: "name",
      title: t("v2.home.agents.colAgent"),
      render: (agent) => (
        <>
          <LinkButton onClick={() => open(agent)}>{agent.name}</LinkButton>
          <span className="sub mono">ID: {agent.id.slice(0, 12)}</span>
        </>
      ),
    },
    { key: "method", title: t("v2.home.agents.colMethod"), render: (agent) => <Tag tone="outline">{methodLabel(agent.method)}</Tag> },
    {
      key: "status",
      title: t("v2.home.agents.colStatus"),
      // the pipeline position only says something while a deploy is moving or stuck
      render: (agent) => (
        <>
          <Tag tone={STATUS_TONE[agent.status] ?? "gray"}>{t(`status.${agent.status}`, { defaultValue: agent.status })}</Tag>
          {(agent.status === "deploying" || agent.status === "failed") && <span className="sub mono">{stageSummary(agent)}</span>}
        </>
      ),
    },
    { key: "created", title: t("v2.home.agents.colCreated"), className: "nowrap", render: (agent) => fmtTime(agent.created_at) },
  ];
  return (
    <Card
      title={t("v2.home.agents.title")}
      flush
      testId="v2-home-agents"
      end={
        <Button size="sm" onClick={() => navigate("/v2/agents")}>
          {t("v2.home.agents.all")}
          <ArrowRight size={13} aria-hidden="true" />
        </Button>
      }
    >
      <div style={{ padding: "0 24px 16px" }}>
        <Table
          columns={columns}
          rows={rows}
          rowKey={(agent) => agent.id}
          loading={loading}
          error={agents === null ? error : null}
          onRetry={onRetry}
          empty={
            <>
              {t("v2.home.agents.empty")}{" "}
              <LinkButton onClick={() => navigate("/v2/agents?view=new")}>{t("v2.home.agents.create")}</LinkButton>
            </>
          }
        />
      </div>
    </Card>
  );
}
