import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import type { ExperimentInfo } from "../../../lib/experiments";
import { verdictLabel } from "../../../lib/experiments";
import { fmtTime } from "../../format";
import { usePaged } from "../../hooks";
import { Alert, Button, Card, type Column, FilterSelect, LinkButton, Pager, SearchInput, Table, Tag } from "../../ui";
import { EXPERIMENT_TONE, stageLabel } from "./common";

const STATUSES = ["running", "ready", "promoted", "failed", "cleaned"];

export function ExperimentList({
  experiments,
  loading,
  error,
  reload,
}: {
  experiments: ExperimentInfo[];
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const [status, setStatus] = useState("");
  const [agent, setAgent] = useState("");
  const [q, setQ] = useState("");
  const hasRunning = experiments.some((e) => e.status === "running");
  const agents = useMemo(() => [...new Set(experiments.map((e) => e.agent_name))].sort(), [experiments]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return experiments.filter((e) => {
      if (status && e.status !== status) return false;
      if (agent && e.agent_name !== agent) return false;
      return !needle || `${e.name} ${e.id}`.toLowerCase().includes(needle);
    });
  }, [experiments, status, agent, q]);
  const paged = usePaged(rows, 12);
  const open = (e: ExperimentInfo) => setParams({ view: "detail", id: e.id });

  const columns: Column<ExperimentInfo>[] = [
    {
      key: "name",
      title: t("v2.tasks.colNameId"),
      render: (e) => (
        <>
          <LinkButton onClick={() => open(e)} testId={`v2-exp-${e.id}`}>
            {e.name}
          </LinkButton>
          <span className="sub mono">ID: {e.id}</span>
        </>
      ),
    },
    { key: "agent", title: t("v2.tasks.colAgent"), render: (e) => e.agent_name },
    {
      key: "stage",
      title: t("v2.experiments.colStage"),
      render: (e) => (e.running_action ? <Tag tone="blue" dot>{stageLabel(t, e)}</Tag> : stageLabel(t, e)),
    },
    {
      key: "verdict",
      title: t("v2.experiments.colVerdict"),
      render: (e) => {
        const v = e.artifacts.verdict;
        if (!v) return <span className="v2-muted">—</span>;
        const weak = v.significant === false || v.verdict.includes("insufficient");
        return <Tag tone={weak ? "orange" : "green"}>{verdictLabel(t, v)}</Tag>;
      },
    },
    {
      key: "status",
      title: t("v2.tasks.colStatus"),
      render: (e) => <Tag tone={EXPERIMENT_TONE[e.status] ?? "gray"}>{t(`v2.experiments.status.${e.status}`, { defaultValue: e.status })}</Tag>,
    },
    { key: "created", title: t("v2.tasks.colCreated"), className: "nowrap", render: (e) => fmtTime(e.created_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (e) => (
        <div className="v2-actions">
          <LinkButton onClick={() => open(e)}>{t("v2.common.view")}</LinkButton>
        </div>
      ),
    },
  ];

  return (
    <Card>
      {hasRunning && <Alert>{t("evalPage.experiment.runningGuard")}</Alert>}
      <div className="v2-toolbar">
        <Button onClick={reload}>{t("v2.common.refresh")}</Button>
        <Button
          kind="primary"
          disabled={hasRunning}
          title={hasRunning ? t("evalPage.experiment.runningGuard") : undefined}
          onClick={() => setParams({ view: "new" })}
          testId="v2-exp-new"
        >
          <Plus size={14} aria-hidden="true" />
          {t("expPage.start")}
        </Button>
        <FilterSelect
          label={t("v2.tasks.colStatus")}
          value={status}
          allLabel={t("v2.common.all")}
          onChange={setStatus}
          options={STATUSES.map((s) => ({ value: s, label: t(`v2.experiments.status.${s}`) }))}
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
        rowKey={(e) => e.id}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={experiments.length ? t("v2.experiments.noMatch") : t("v2.experiments.empty")}
        testId="v2-exp-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
    </Card>
  );
}
