import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import type { MemoryOverview, MemorySibling, MemoryStrategy } from "../../../lib/api";
import { fmtTime } from "../../format";
import { Button, Card, type Column, Descriptions, Kpi, LinkButton, Table, Tag } from "../../ui";
import { shortId, statusTone } from "./common";

/** Memory resource configuration + the long-term strategies that drive extraction. */
export function OverviewTab({ overview, onReload }: { overview: MemoryOverview; onReload: () => void }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const mem = overview.memory;
  if (!mem) return null;

  const strategyColumns: Column<MemoryStrategy>[] = [
    {
      key: "name",
      title: t("v2.memory.colStrategy"),
      render: (s) => (
        <>
          <b>{s.name ?? "—"}</b>
          <span className="sub mono">ID: {s.strategy_id ?? "—"}</span>
        </>
      ),
    },
    { key: "type", title: t("v2.memory.colType"), render: (s) => <Tag tone="outline">{s.type ?? "—"}</Tag> },
    {
      key: "status",
      title: t("v2.memory.colStatus"),
      render: (s) => (
        <Tag tone={statusTone(s.status)} dot>
          {s.status ?? "—"}
        </Tag>
      ),
    },
    {
      key: "ns",
      title: t("v2.memory.colNamespace"),
      render: (s) => (
        <div className="v2-stack" style={{ gap: 2 }}>
          {(s.namespace_templates.length ? s.namespace_templates : s.namespaces).map((ns) => (
            <span key={ns} className="mono">
              {ns}
            </span>
          ))}
          {s.description && <span className="v2-muted">{s.description}</span>}
        </div>
      ),
    },
    { key: "created", title: t("v2.memory.colCreated"), className: "nowrap", render: (s) => fmtTime(s.created_at) },
  ];

  const siblingColumns: Column<MemorySibling>[] = [
    {
      key: "id",
      title: t("v2.memory.colMemoryId"),
      render: (m) => (
        <span className="mono" title={m.arn ?? ""}>
          {m.id ?? "—"}
        </span>
      ),
    },
    {
      key: "status",
      title: t("v2.memory.colStatus"),
      render: (m) => (
        <Tag tone={statusTone(m.status)} dot>
          {m.status ?? "—"}
        </Tag>
      ),
    },
    {
      key: "role",
      title: t("v2.memory.colRole"),
      render: (m) =>
        m.is_platform ? (
          <Tag tone="orange">{t("v2.memory.platformMemory")}</Tag>
        ) : (
          <span className="v2-muted">{t("memoryPage.overview.externalMemory")}</span>
        ),
    },
    { key: "created", title: t("v2.memory.colCreated"), className: "nowrap", render: (m) => fmtTime(m.created_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (m) =>
        m.id ? (
          <div className="v2-actions">
            <LinkButton onClick={() => setParams({ view: "resource", id: m.id ?? "" })}>{t("v2.common.view")}</LinkButton>
          </div>
        ) : null,
    },
  ];

  return (
    <>
      <div className="v2-kpis">
        <Kpi
          label={t("v2.memory.kpi.actors")}
          value={overview.actor_count_truncated ? `${overview.actor_count}+` : overview.actor_count}
          sub={overview.actor_count_truncated ? t("memoryPage.overview.actorsTruncated") : t("v2.memory.kpi.actorsSub")}
        />
        <Kpi label={t("v2.memory.kpi.strategies")} value={overview.strategies.length} sub={t("memoryPage.overview.strategiesFoot")} />
        <Kpi
          label={t("v2.memory.kpi.expiry")}
          value={mem.event_expiry_days != null ? t("memoryPage.overview.daysValue", { count: mem.event_expiry_days }) : "—"}
          sub={t("memoryPage.overview.expiryFoot")}
        />
        <Kpi
          label={t("v2.memory.kpi.status")}
          value={mem.status ?? "—"}
          tone={statusTone(mem.status) === "red" ? "bad" : statusTone(mem.status) === "green" ? "good" : undefined}
          sub={mem.failure_reason ?? t("memoryPage.overview.statusFoot")}
        />
      </div>

      <Card
        title={t("memoryPage.overview.resourceTitle")}
        sub={mem.name ?? undefined}
        end={
          <span className="v2-row">
            <Button size="sm" onClick={onReload}>
              {t("v2.common.refresh")}
            </Button>
            <Button size="sm" onClick={() => setParams({ view: "resource", id: mem.id })}>
              {t("v2.common.detail")}
            </Button>
          </span>
        }
        testId="v2-memory-resource"
      >
        <Descriptions
          items={[
            { label: t("memoryPage.overview.id"), value: <span className="mono">{mem.id}</span> },
            {
              label: t("memoryPage.overview.arn"),
              value: (
                <span className="mono" title={mem.arn ?? ""}>
                  {shortId(mem.arn, 22)}
                </span>
              ),
            },
            { label: t("memoryPage.overview.description"), value: mem.description || "—" },
            {
              label: t("memoryPage.overview.expiry"),
              value: mem.event_expiry_days != null ? t("memoryPage.overview.daysValue", { count: mem.event_expiry_days }) : "—",
            },
            {
              label: t("memoryPage.overview.encryption"),
              value: mem.encryption_key_arn ? (
                <span className="mono" title={mem.encryption_key_arn}>
                  {shortId(mem.encryption_key_arn, 16)}
                </span>
              ) : (
                t("memoryPage.overview.awsManagedKey")
              ),
            },
            {
              label: t("memoryPage.overview.executionRole"),
              value: (
                <span className="mono" title={mem.execution_role_arn ?? ""}>
                  {shortId(mem.execution_role_arn, 18)}
                </span>
              ),
            },
            { label: t("memoryPage.overview.createdAt"), value: fmtTime(mem.created_at) },
            { label: t("memoryPage.overview.updatedAt"), value: fmtTime(mem.updated_at) },
            ...(mem.failure_reason ? [{ label: t("memoryPage.overview.failureReason"), value: mem.failure_reason }] : []),
          ]}
        />
      </Card>

      <Card title={t("memoryPage.overview.strategiesTitle")} sub={t("memoryPage.overview.strategiesSub")}>
        <Table
          columns={strategyColumns}
          rows={overview.strategies}
          rowKey={(s) => s.strategy_id ?? s.name ?? ""}
          empty={t("memoryPage.overview.noStrategies")}
          testId="v2-memory-strategies"
        />
      </Card>

      <Card title={t("memoryPage.overview.siblingsTitle")} sub={t("v2.memory.siblingsSub")}>
        <Table
          columns={siblingColumns}
          rows={overview.other_memories}
          rowKey={(m) => m.id ?? m.arn ?? ""}
          selectedKey={overview.other_memories.find((m) => m.is_platform)?.id ?? null}
          empty={t("memoryPage.overview.noSiblings")}
        />
      </Card>
    </>
  );
}
