import { Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { api, errorMessage, type OnlineEvalConfigRow } from "../../../lib/api";
import { evaluatorLabel } from "../../../lib/evaluators";
import { creatorLabel, fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import {
  agentLabel,
  canToggle,
  configName,
  isEditable,
  isTransient,
  modeOf,
  ownerLabel,
  OWNER_TONE,
  POLL_MS,
  statusTone,
} from "../../online";
import { evaluatorSummary } from "../../tasks";
import {
  type AttentionItem,
  AttentionPanel,
  Button,
  Card,
  type Column,
  Confirm,
  FilterSelect,
  Lamp,
  LinkButton,
  Pager,
  SearchInput,
  Table,
  Tag,
} from "../../ui";
import { backState } from "../tasks/back";
import { newTaskParams } from "../tasks/urls";
import { onlineAttention } from "./attention";

type Pending = { row: OnlineEvalConfigRow; action: "pause" | "resume" | "delete" };

/** 持续评估 (`/v2/eval/tasks?tab=online`): every online evaluation config of the workspace
 *  (agent-owned, experiment / canary-owned, external), scores and insights modes. Opens on the
 *  agent-owned ones; the owner filter shows the rest. The page header and the tabs belong
 *  to `V2Tasks`; this renders the attention panel and the table under them. */
export function OnlineList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const location = useLocation();
  const toast = useV2Toast();
  const { can } = useAuth();
  const mayRun = can("eval.run");
  const { data, loading, error, reload } = useLoad(() => api.v2OnlineConfigs(), "online-configs");
  const [mode, setMode] = useState("");
  const [owner, setOwner] = useState<string>("agent");
  const [exec, setExec] = useState("");
  const [agent, setAgent] = useState("");
  const [q, setQ] = useState("");
  const [pending, setPending] = useState<Pending | null>(null);
  const [busy, setBusy] = useState(false);

  const configs = useMemo(() => data?.configs ?? [], [data]);
  const agents = useMemo(() => [...new Set(configs.map(agentLabel))].sort(), [configs]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return configs.filter((row) => {
      if (mode && modeOf(row) !== mode) return false;
      if (owner && row.owner !== owner) return false;
      if (exec && row.execution_status !== exec) return false;
      if (agent && agentLabel(row) !== agent) return false;
      return !needle || `${configName(row)} ${row.config_id} ${row.description}`.toLowerCase().includes(needle);
    });
  }, [configs, mode, owner, exec, agent, q]);
  const paged = usePaged(rows, 12);

  // CREATING / UPDATING / DELETING settle on their own: follow them
  const transient = configs.some(isTransient);
  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(reload, POLL_MS);
    return () => window.clearInterval(timer);
  }, [transient, reload]);

  // the detail / form's Back returns to this tab (`back.ts`)
  const back = { state: backState(location.search) };
  const open = (row: OnlineEvalConfigRow) => setParams({ view: "detail", kind: "online", id: row.config_id }, back);

  const attention: AttentionItem[] = onlineAttention(configs).map((entry) => {
    const { row } = entry;
    const hint =
      entry.kind === "failed"
        ? (row.failure_reason?.split("\n")[0] ?? "") || row.status
        : entry.kind === "duplicate"
          ? t("v2.online.duplicateWarn")
          : row.status;
    return {
      key: entry.key,
      signal: entry.signal,
      live: entry.kind === "settling",
      title: configName(row),
      hint,
      meta: [row.agent_name || row.matched_agent || row.parent?.agent_name ? agentLabel(row) : null, t(`v2.online.attention.why.${entry.kind}`)]
        .filter(Boolean)
        .join(" · "),
      onOpen: () => open(row),
      openLabel: t("v2.common.view"),
      testId: `v2-online-att-${entry.kind}-${row.config_id}`,
    };
  });

  const act = async () => {
    if (!pending) return;
    const { row, action } = pending;
    setBusy(true);
    try {
      if (action === "delete") await api.v2DeleteOnlineConfig(row.config_id);
      else await api.v2OnlineAction(row.config_id, action);
      toast("success", action === "delete" ? t("v2.online.deleted", { logGroup: row.results_log_group }) : t(`v2.tasks.done.${action}`));
      setPending(null);
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<OnlineEvalConfigRow>[] = [
    {
      key: "name",
      title: t("v2.tasks.colNameId"),
      render: (row) => (
        <>
          <span className="v2-row" style={{ flexWrap: "nowrap" }}>
            <LinkButton onClick={() => open(row)} testId={`v2-online-${row.config_id}`}>
              <span className="ellipsis" style={{ maxWidth: 220 }} title={configName(row)}>
                {configName(row)}
              </span>
            </LinkButton>
            <Tag tone={OWNER_TONE[row.owner]}>{ownerLabel(t, row)}</Tag>
            {row.duplicate_enabled && (
              <Tag tone="orange" title={t("v2.online.duplicateWarn")}>
                {t("v2.online.duplicate")}
              </Tag>
            )}
          </span>
          <span className="sub mono">ID: {row.config_id}</span>
        </>
      ),
    },
    { key: "agent", title: t("v2.tasks.colAgent"), render: (row) => <span className="ellipsis">{agentLabel(row)}</span> },
    {
      key: "judges",
      title: t("v2.online.colJudges"),
      render: (row) => (
        <>
          <Tag tone={modeOf(row) === "insights" ? "blue" : "outline"}>{t(`v2.online.mode.${modeOf(row)}`)}</Tag>{" "}
          {modeOf(row) === "insights"
            ? t("v2.online.insightsCount", { count: row.insights.length })
            : row.detailed
              ? evaluatorSummary(t, row.evaluators, (id) => evaluatorLabel(t, id))
              : "—"}
        </>
      ),
    },
    {
      key: "sampling",
      title: t("v2.tasks.samplingRate"),
      className: "num",
      render: (row) => (row.sampling_percentage != null ? row.sampling_percentage : "—"),
    },
    {
      key: "status",
      title: t("v2.tasks.colStatus"),
      render: (row) => (
        // the resource status only speaks up when it is not the steady ACTIVE
        row.status && row.status !== "ACTIVE" ? (
          <Tag tone={statusTone(row.status)} title={row.failure_reason ?? undefined}>
            {row.status}
          </Tag>
        ) : (
          <Tag tone={row.execution_status === "ENABLED" ? "green" : "gray"} dot>
            {t(`v2.online.exec.${row.execution_status === "ENABLED" ? "on" : "off"}`)}
          </Tag>
        )
      ),
    },
    {
      key: "updated",
      title: t("v2.tasks.colUpdated"),
      className: "nowrap",
      render: (row) => (
        <>
          {fmtTime(row.updated_at ?? row.created_at)}
          <span className="sub" title={t("v2.common.creator")}>{creatorLabel(row)}</span>
        </>
      ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) => {
        const locked = busy || isTransient(row);
        return (
          <div className="v2-actions">
            <LinkButton onClick={() => open(row)}>{t("v2.common.view")}</LinkButton>
            {isEditable(row) && (
              <LinkButton disabled={locked || !mayRun} onClick={() => setParams({ view: "edit", kind: "online", id: row.config_id }, back)}>
                {t("v2.common.edit")}
              </LinkButton>
            )}
            {canToggle(row) && row.execution_status === "ENABLED" && (
              <LinkButton disabled={locked} onClick={() => setPending({ row, action: "pause" })}>
                {t("v2.tasks.pause")}
              </LinkButton>
            )}
            {canToggle(row) && row.execution_status === "DISABLED" && (
              <LinkButton disabled={locked || !mayRun} onClick={() => setPending({ row, action: "resume" })}>
                {t("v2.tasks.resume")}
              </LinkButton>
            )}
            {canToggle(row) && (
              <LinkButton danger disabled={busy || row.status === "DELETING" || !mayRun} onClick={() => setPending({ row, action: "delete" })}>
                {t("v2.common.delete")}
              </LinkButton>
            )}
          </div>
        );
      },
    },
  ];

  return (
    <>
      {data && (
        <AttentionPanel
          title={t("v2.online.attention.title")}
          items={attention}
          dismissScope="online"
          testId="v2-online-attention"
          empty={
            <>
              <Lamp signal={configs.length ? "ok" : "off"} />
              {configs.length ? t("v2.online.attention.empty") : t("v2.online.empty")}
            </>
          }
        />
      )}
      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload}>{t("v2.common.refresh")}</Button>
          <Button
            kind="primary"
            disabled={!mayRun}
            title={mayRun ? undefined : t("v2.tasks.noPermission")}
            onClick={() => setParams(newTaskParams("continuous"), back)}
            testId="v2-online-new"
          >
            <Plus size={14} aria-hidden="true" />
            {t("v2.tasks.new")}
          </Button>
          <FilterSelect
            label={t("v2.online.colMode")}
            value={mode}
            allLabel={t("v2.common.all")}
            onChange={setMode}
            options={(["scores", "insights"] as const).map((m) => ({ value: m, label: t(`v2.online.mode.${m}`) }))}
          />
          <FilterSelect
            label={t("v2.online.colOwner")}
            value={owner}
            allLabel={t("v2.common.all")}
            onChange={setOwner}
            options={(["agent", "experiment", "external"] as const).map((o) => ({ value: o, label: t(`v2.online.owner.${o}`) }))}
          />
          <FilterSelect
            label={t("v2.online.colExec")}
            value={exec}
            allLabel={t("v2.common.all")}
            onChange={setExec}
            options={[
              { value: "ENABLED", label: t("v2.online.exec.on") },
              { value: "DISABLED", label: t("v2.online.exec.off") },
            ]}
          />
          <FilterSelect
            label={t("v2.tasks.colAgent")}
            value={agent}
            allLabel={t("v2.common.all")}
            onChange={setAgent}
            options={agents.map((a) => ({ value: a, label: a }))}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.online.search")} />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(row) => row.config_id}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={configs.length ? t("v2.online.noMatch") : t("v2.online.empty")}
          testId="v2-online-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      <Confirm
        open={pending !== null}
        title={pending ? t(`v2.online.confirm.${pending.action}Title`) : ""}
        body={pending ? t(`v2.online.confirm.${pending.action}Body`, { name: configName(pending.row), logGroup: pending.row.results_log_group }) : null}
        confirmLabel={pending ? t(`v2.online.confirm.${pending.action}Ok`) : ""}
        danger={pending?.action === "delete"}
        busy={busy}
        onConfirm={() => void act()}
        onClose={() => setPending(null)}
      />
    </>
  );
}
