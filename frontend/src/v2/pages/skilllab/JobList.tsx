import { Plus } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type SkillLabJobInfo } from "../../../lib/api";
import { isLiveJob, RESUMABLE_STATUSES, tasksetLabel } from "../../../lib/skillLab";
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
import { JobStatusTag } from "./common";
import { bestScoreCache, JOB_STATUSES, passRateCache, useJobList } from "./state";

type Action = "cancel" | "delete" | "resume";

/** Evaluation (`eval`) and optimization (`train`) job lists — same shape, one score column apart. */
export function JobList({ type }: { type: "eval" | "train" }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { rows: all, loading, error, reload } = useJobList(type);
  const [status, setStatus] = useState("");
  const [skill, setSkill] = useState("");
  const [q, setQ] = useState("");
  const [pending, setPending] = useState<{ job: SkillLabJobInfo; action: Action } | null>(null);
  const [busy, setBusy] = useState(false);

  const skills = useMemo(
    () => [...new Set(all.map((job) => job.skill_source?.name ?? "").filter(Boolean))].sort(),
    [all],
  );
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((job) => {
      if (status && job.status !== status) return false;
      if (skill && job.skill_source?.name !== skill) return false;
      return !needle || `${job.id} ${job.skill_source?.name ?? ""} ${job.taskset_name}`.toLowerCase().includes(needle);
    });
  }, [all, status, skill, q]);
  const paged = usePaged(rows, 12);
  const open = (job: SkillLabJobInfo) => setParams({ tab: type, view: "detail", id: job.id });

  const act = async () => {
    if (!pending) return;
    const { job, action } = pending;
    setBusy(true);
    try {
      if (action === "cancel") {
        await api.skillLabJobCancel(job.id);
        toast("success", t("skillLab.eval.cancelRequested"));
      } else if (action === "resume") {
        await api.skillLabJobResume(job.id);
        toast("success", t("skillLab.train.resumed"));
      } else {
        await api.skillLabJobDelete(job.id);
        toast("success", t("skillLab.eval.deleted"));
      }
      setPending(null);
      void reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const score = (job: SkillLabJobInfo) => {
    if (job.status !== "succeeded") return "—";
    if (type === "eval") {
      const rate = passRateCache.get(job.id);
      return rate === undefined ? "—" : `${(rate * 100).toFixed(1)}%`;
    }
    const best = bestScoreCache.get(job.id);
    return best === undefined ? "—" : best.toFixed(3);
  };

  const columns: Column<SkillLabJobInfo>[] = [
    {
      key: "skill",
      title: t("v2.skillLab.colSkillJob"),
      render: (job) => (
        <>
          <span className="v2-row" style={{ flexWrap: "nowrap" }}>
            <LinkButton onClick={() => open(job)} testId={`v2-${type}-${job.id}`}>
              <span className="ellipsis" style={{ maxWidth: 240 }}>
                {job.skill_source?.name ?? t("skillLab.eval.unknownSkill")}
              </span>
            </LinkButton>
            {job.skill_source && (
              <Tag tone={job.skill_source.kind === "registry" ? "outline" : "gray"}>
                {job.skill_source.kind === "registry" ? job.skill_source.version || "registry" : t("v2.skillLab.uploaded")}
              </Tag>
            )}
          </span>
          <span className="sub mono">ID: {job.id}</span>
        </>
      ),
    },
    {
      key: "taskset",
      title: t("skillLab.eval.col.taskset"),
      render: (job) => (
        <span className="ellipsis" style={{ maxWidth: 240 }} title={tasksetLabel(job)}>
          {tasksetLabel(job)}
        </span>
      ),
    },
    {
      key: "params",
      title: type === "eval" ? t("skillLab.backend.judgeMode") : t("skillLab.train.field.loop"),
      render: (job) =>
        type === "eval" ? (
          <Tag tone="outline">{job.params.judge_mode ?? "auto"}</Tag>
        ) : (
          <span className="nowrap">
            {t("skillLab.train.field.epochs", { n: job.params.epochs ?? 1 })} · {job.params.gate_metric ?? "hard"}
          </span>
        ),
    },
    {
      key: "score",
      title: type === "eval" ? t("skillLab.eval.col.passRate") : t("skillLab.train.col.best"),
      className: "num",
      render: score,
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
            <LinkButton danger disabled={busy} onClick={() => setPending({ job, action: "cancel" })}>
              {t("v2.skillLab.cancelRun")}
            </LinkButton>
          )}
          {type === "train" && RESUMABLE_STATUSES.includes(job.status) && (
            <LinkButton disabled={busy} onClick={() => setPending({ job, action: "resume" })}>
              {t("skillLab.train.resume")}
            </LinkButton>
          )}
          {!isLiveJob(job) && (
            <LinkButton danger disabled={busy} onClick={() => setPending({ job, action: "delete" })}>
              {t("v2.common.delete")}
            </LinkButton>
          )}
        </div>
      ),
    },
  ];

  const confirmTitle = !pending
    ? ""
    : pending.action === "resume"
      ? t("skillLab.train.confirmResume.title")
      : pending.action === "cancel"
        ? t(type === "eval" ? "skillLab.eval.confirmCancel.title" : "skillLab.train.confirmCancel.title")
        : t(type === "eval" ? "skillLab.eval.confirmDelete.title" : "skillLab.train.confirmDelete.title");
  const confirmBody = !pending
    ? ""
    : pending.action === "resume"
      ? t("skillLab.train.confirmResume.body")
      : pending.action === "cancel"
        ? t("skillLab.eval.confirmCancel.body")
        : t("skillLab.eval.confirmDelete.body", { name: pending.job.skill_source?.name ?? pending.job.id });

  return (
    <Card>
      <div className="v2-toolbar">
        <Button onClick={() => void reload()}>{t("v2.common.refresh")}</Button>
        <Button kind="primary" onClick={() => setParams({ tab: type, view: "new" })} testId={`v2-${type}-new`}>
          <Plus size={14} aria-hidden="true" />
          {t(type === "eval" ? "skillLab.eval.new" : "skillLab.train.new")}
        </Button>
        <FilterSelect
          label={t("skillLab.eval.col.status")}
          value={status}
          allLabel={t("v2.common.all")}
          onChange={setStatus}
          options={JOB_STATUSES.map((s) => ({ value: s, label: t(`skillLab.eval.status.${s}`) }))}
        />
        <FilterSelect
          label={t("skillLab.eval.col.skill")}
          value={skill}
          allLabel={t("v2.common.all")}
          onChange={setSkill}
          options={skills.map((s) => ({ value: s, label: s }))}
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
        empty={all.length ? t("v2.skillLab.noMatch") : t(type === "eval" ? "skillLab.eval.empty" : "skillLab.train.empty")}
        testId={`v2-${type}-table`}
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      <Confirm
        open={pending !== null}
        title={confirmTitle}
        body={confirmBody}
        confirmLabel={
          pending?.action === "resume"
            ? t("skillLab.train.resume")
            : pending?.action === "cancel"
              ? t("v2.skillLab.cancelRun")
              : t("v2.common.delete")
        }
        danger={pending?.action !== "resume"}
        busy={busy}
        onConfirm={() => void act()}
        onClose={() => setPending(null)}
      />
    </Card>
  );
}
