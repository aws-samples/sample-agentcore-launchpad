import { Plus, Sparkles } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type SkillLabTasksetInfo } from "../../../lib/api";
import { countsLabel } from "../../../lib/skillLabTasksets";
import { fmtTime } from "../../format";
import { usePaged, useV2Toast } from "../../hooks";
import {
  Button,
  Card,
  type Column,
  Confirm,
  FilterSelect,
  LinkButton,
  Pager,
  SearchInput,
  Table,
  Tag,
} from "../../ui";
import { useTasksets } from "./state";

export function TasksetList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { data, loading, error, reload } = useTasksets();
  const [mode, setMode] = useState("");
  const [q, setQ] = useState("");
  const [pending, setPending] = useState<SkillLabTasksetInfo | null>(null);
  const [busy, setBusy] = useState(false);

  const all = useMemo(() => data ?? [], [data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((row) => {
      if (mode && row.mode !== mode) return false;
      return !needle || `${row.name} ${row.id} ${row.description}`.toLowerCase().includes(needle);
    });
  }, [all, mode, q]);
  const paged = usePaged(rows, 12);

  const open = (row: SkillLabTasksetInfo) => setParams({ tab: "tasksets", view: "detail", id: row.id });

  const remove = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      await api.skillLabTasksetDelete(pending.id);
      toast("success", t("skillLab.tasksets.deleted"));
      setPending(null);
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<SkillLabTasksetInfo>[] = [
    {
      key: "name",
      title: t("v2.skillLab.colNameId"),
      render: (row) => (
        <>
          <span className="v2-row" style={{ flexWrap: "nowrap" }}>
            <LinkButton onClick={() => open(row)} testId={`v2-taskset-${row.id}`}>
              <span className="ellipsis" style={{ maxWidth: 360 }} title={row.description || row.name}>
                {row.name}
              </span>
            </LinkButton>
            {row.sample && <Tag tone="blue">{t("skillLab.tasksets.sampleChip")}</Tag>}
          </span>
          <span className="sub mono">ID: {row.id}</span>
        </>
      ),
    },
    {
      key: "mode",
      title: t("skillLab.tasksets.col.mode"),
      render: (row) => (
        <Tag tone={row.mode === "split" ? "blue" : "outline"}>{t(`skillLab.tasksets.mode.${row.mode}`)}</Tag>
      ),
    },
    { key: "counts", title: t("skillLab.tasksets.col.counts"), className: "nowrap mono", render: (row) => countsLabel(row.counts) },
    { key: "updated", title: t("skillLab.tasksets.col.updated"), className: "nowrap", render: (row) => fmtTime(row.updated_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) => (
        <div className="v2-actions">
          <LinkButton onClick={() => open(row)}>{t("v2.common.view")}</LinkButton>
          {!row.sample && (
            <>
              <LinkButton onClick={() => setParams({ tab: "tasksets", view: "edit", id: row.id })}>
                {t("v2.common.edit")}
              </LinkButton>
              <LinkButton danger disabled={busy} onClick={() => setPending(row)}>
                {t("v2.common.delete")}
              </LinkButton>
            </>
          )}
        </div>
      ),
    },
  ];

  return (
    <Card>
      <div className="v2-toolbar">
        <Button onClick={reload}>{t("v2.common.refresh")}</Button>
        <Button kind="primary" onClick={() => setParams({ tab: "tasksets", view: "new" })} testId="v2-taskset-new">
          <Plus size={14} aria-hidden="true" />
          {t("skillLab.tasksets.new")}
        </Button>
        <Button kind="soft" onClick={() => setParams({ tab: "tasksets", view: "gen-new" })} testId="v2-taskset-ai">
          <Sparkles size={14} aria-hidden="true" />
          {t("skillLab.taskgen.open")}
        </Button>
        <FilterSelect
          label={t("skillLab.tasksets.col.mode")}
          value={mode}
          allLabel={t("v2.common.all")}
          onChange={setMode}
          options={(["single", "split"] as const).map((m) => ({ value: m, label: t(`skillLab.tasksets.mode.${m}`) }))}
        />
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.skillLab.searchTasksets")} />
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(row) => row.id}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={all.length ? t("v2.skillLab.noMatch") : t("skillLab.tasksets.empty")}
        testId="v2-taskset-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      <Confirm
        open={pending !== null}
        title={t("skillLab.tasksets.confirmDelete.title")}
        body={t("skillLab.tasksets.confirmDelete.body", { name: pending?.name ?? "" })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => setPending(null)}
      />
    </Card>
  );
}
