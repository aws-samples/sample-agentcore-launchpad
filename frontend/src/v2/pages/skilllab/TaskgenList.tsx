import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type SkillLabJobInfo } from "../../../lib/api";
import { isLiveJob, jobSkillName } from "../../../lib/skillLab";
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
} from "../../ui";
import { JobStatusTag } from "./common";
import { JOB_STATUSES, taskgenTarget, useJobList } from "./state";

export function TaskgenList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { rows: all, loading, error, reload } = useJobList("taskgen");
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [pending, setPending] = useState<SkillLabJobInfo | null>(null);
  const [busy, setBusy] = useState(false);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((job) => {
      if (status && job.status !== status) return false;
      return !needle || `${job.id} ${jobSkillName(job)} ${job.taskset_name}`.toLowerCase().includes(needle);
    });
  }, [all, status, q]);
  const paged = usePaged(rows, 12);
  const open = (job: SkillLabJobInfo) => setParams({ tab: "taskgen", view: "detail", id: job.id });

  const cancel = async () => {
    if (!pending) return;
    setBusy(true);
    try {
      await api.skillLabJobCancel(pending.id);
      toast("success", t("skillLab.eval.cancelRequested"));
      setPending(null);
      void reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<SkillLabJobInfo>[] = [
    {
      key: "skills",
      title: t("v2.skillLab.colSkillJob"),
      render: (job) => (
        <>
          <LinkButton onClick={() => open(job)} testId={`v2-taskgen-${job.id}`}>
            <span className="ellipsis" style={{ maxWidth: 300 }} title={jobSkillName(job)}>
              {jobSkillName(job) || "—"}
            </span>
          </LinkButton>
          <span className="sub mono">ID: {job.id}</span>
        </>
      ),
    },
    {
      key: "target",
      title: t("skillLab.taskgen.col.target"),
      render: (job) => <span className="ellipsis">{taskgenTarget(job, t("skillLab.taskgen.target.new"))}</span>,
    },
    { key: "count", title: t("skillLab.taskgen.field.count"), className: "num", render: (job) => job.params.count ?? "—" },
    {
      key: "model",
      title: t("skillLab.eval.field.models"),
      render: (job) => (
        <>
          <span className="mono ellipsis" style={{ maxWidth: 220 }}>
            {job.params.model ?? "—"}
          </span>
          <span className="sub mono">{job.params.target_backend ?? "claude_code_exec"}</span>
        </>
      ),
    },
    { key: "status", title: t("skillLab.eval.col.status"), render: (job) => <JobStatusTag job={job} /> },
    { key: "created", title: t("skillLab.eval.col.created"), className: "nowrap", render: (job) => fmtTime(job.created_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (job) => (
        <div className="v2-actions">
          <LinkButton onClick={() => open(job)}>{t("v2.common.view")}</LinkButton>
          {isLiveJob(job) && (
            <LinkButton danger disabled={busy} onClick={() => setPending(job)}>
              {t("skillLab.taskgen.job.cancel")}
            </LinkButton>
          )}
        </div>
      ),
    },
  ];

  return (
    <Card>
      <div className="v2-toolbar">
        <Button onClick={() => void reload()}>{t("v2.common.refresh")}</Button>
        <Button kind="primary" onClick={() => setParams({ tab: "taskgen", view: "new" })} testId="v2-taskgen-new">
          <Plus size={14} aria-hidden="true" />
          {t("skillLab.taskgen.wizard.title")}
        </Button>
        <FilterSelect
          label={t("skillLab.eval.col.status")}
          value={status}
          allLabel={t("v2.common.all")}
          onChange={setStatus}
          options={JOB_STATUSES.map((s) => ({ value: s, label: t(`skillLab.eval.status.${s}`) }))}
        />
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.skillLab.searchJobs")} />
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(job) => job.id}
        loading={loading}
        error={error}
        onRetry={() => void reload()}
        empty={all.length ? t("v2.skillLab.noMatch") : t("skillLab.taskgen.empty")}
        testId="v2-taskgen-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      <Confirm
        open={pending !== null}
        title={t("v2.skillLab.confirmCancelGen")}
        body={t("skillLab.eval.confirmCancel.body")}
        confirmLabel={t("skillLab.taskgen.job.cancel")}
        danger
        busy={busy}
        onConfirm={() => void cancel()}
        onClose={() => setPending(null)}
      />
    </Card>
  );
}
