import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import type { RuntimeCanaryInfo } from "../../../lib/api";
import { fmtTime } from "../../format";
import { usePaged } from "../../hooks";
import { Button, Card, type Column, FilterSelect, LinkButton, Pager, SearchInput, Table, Tag } from "../../ui";
import { CANARY_TONE, versionsLabel, weightsLabel } from "./common";

const STATUSES: RuntimeCanaryInfo["status"][] = ["running", "completed", "rolled_back", "cleaned"];

export function CanaryList({
  canaries,
  loading,
  error,
  reload,
}: {
  canaries: RuntimeCanaryInfo[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const [status, setStatus] = useState("");
  const [agent, setAgent] = useState("");
  const [q, setQ] = useState("");
  const agents = useMemo(() => [...new Set(canaries.map((c) => c.champion_agent_name))].sort(), [canaries]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return canaries.filter((c) => {
      if (status && c.status !== status) return false;
      if (agent && c.champion_agent_name !== agent) return false;
      return !needle || `${c.name} ${c.id}`.toLowerCase().includes(needle);
    });
  }, [canaries, status, agent, q]);
  const paged = usePaged(rows, 12);
  const open = (c: RuntimeCanaryInfo) => setParams({ mode: "canary", canary: c.id });

  const columns: Column<RuntimeCanaryInfo>[] = [
    {
      key: "name",
      title: t("v2.tasks.colNameId"),
      render: (c) => (
        <>
          <LinkButton onClick={() => open(c)} testId={`v2-canary-${c.id}`}>
            {c.name}
          </LinkButton>
          <span className="sub mono">ID: {c.id}</span>
        </>
      ),
    },
    { key: "agent", title: t("v2.tasks.colAgent"), render: (c) => c.champion_agent_name },
    { key: "versions", title: t("canaryPage.list.versions"), render: (c) => <span className="mono">{versionsLabel(c.artifacts.setup)}</span> },
    { key: "weights", title: t("canaryPage.list.weights"), render: (c) => <span className="mono">{weightsLabel(c.artifacts.setup)}</span> },
    {
      key: "stage",
      title: t("v2.experiments.colStage"),
      render: (c) =>
        c.running_action ? (
          <Tag tone="blue" dot>
            {t("v2.canary.actionRunning", { action: t(`v2.canary.action.${c.running_action}`, { defaultValue: c.running_action }) })}
          </Tag>
        ) : (
          t(`v2.canary.stage.${c.stage}`, { defaultValue: c.stage })
        ),
    },
    { key: "status", title: t("v2.tasks.colStatus"), render: (c) => <Tag tone={CANARY_TONE[c.status]}>{t(`v2.canary.status.${c.status}`)}</Tag> },
    { key: "created", title: t("v2.tasks.colCreated"), className: "nowrap", render: (c) => fmtTime(c.created_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (c) => (
        <div className="v2-actions">
          <LinkButton onClick={() => open(c)}>{t("v2.common.view")}</LinkButton>
        </div>
      ),
    },
  ];

  return (
    <Card>
      <div className="v2-toolbar">
        <Button onClick={reload}>{t("v2.common.refresh")}</Button>
        <Button kind="primary" onClick={() => setParams({ mode: "canary", canary: "new" })} testId="v2-canary-new">
          <Plus size={14} aria-hidden="true" />
          {t("canaryPage.list.new")}
        </Button>
        <FilterSelect
          label={t("v2.tasks.colStatus")}
          value={status}
          allLabel={t("v2.common.all")}
          onChange={setStatus}
          options={STATUSES.map((s) => ({ value: s, label: t(`v2.canary.status.${s}`) }))}
        />
        <FilterSelect
          label={t("v2.tasks.colAgent")}
          value={agent}
          allLabel={t("v2.common.all")}
          onChange={setAgent}
          options={agents.map((a) => ({ value: a, label: a }))}
        />
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.experiments.search")} />
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(c) => c.id}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={canaries.length ? t("v2.experiments.noMatch") : t("canaryPage.list.empty")}
        testId="v2-canary-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
    </Card>
  );
}
