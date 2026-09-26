import { type ReactNode, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type SkillLabJobInfo, type SkillLabJobResults } from "../../../lib/api";
import { elapsed, isLiveJob, LIVE_STATUSES, tasksetLabel } from "../../../lib/skillLab";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, FlowHeader, Spin, Tag } from "../../ui";
import { ArtifactBrowser } from "./ArtifactBrowser";
import { GoneCard, JobStatusTag } from "./common";
import { passRateCache, useJob } from "./state";
import { EvalResults } from "./EvalResults";
import { JobLog } from "./JobLog";

/** The overview eval and train details share (+ type-specific rows). */
export function JobOverview({ job, extra = [] }: { job: SkillLabJobInfo; extra?: { label: string; value: ReactNode }[] }) {
  const { t } = useTranslation();
  const items = [
    { label: "ID", value: <span className="mono">{job.id}</span> },
    {
      label: t("skillLab.eval.field.skillSource"),
      value: job.skill_source ? (
        <span className="v2-row">
          <Tag tone={job.skill_source.kind === "registry" ? "blue" : "gray"}>
            {t(`skillLab.eval.wizard.source.${job.skill_source.kind}`)}
          </Tag>
          <span className="mono">{job.skill_source.version || ""}</span>
        </span>
      ) : (
        "—"
      ),
    },
    { label: t("skillLab.eval.col.taskset"), value: tasksetLabel(job) },
    { label: t("skillLab.backend.field"), value: <span className="mono">{job.params.target_backend ?? "claude_code_exec"}</span> },
    { label: t("skillLab.eval.wizard.field.targetModel"), value: <span className="mono">{job.params.target_model}</span> },
    {
      label: job.type === "train" ? t("skillLab.train.wizard.field.judgeModel") : t("skillLab.eval.wizard.field.judgeModel"),
      value: (
        <span className="mono">
          {job.params.judge_model} · {job.params.judge_mode ?? "auto"}
        </span>
      ),
    },
    {
      label: t("skillLab.eval.field.execution"),
      value: (
        <span className="mono">
          workers {job.params.workers} · timeout {job.params.timeout}s
          {job.params.limit > 0 ? ` · limit ${job.params.limit}` : ""}
        </span>
      ),
    },
    { label: t("skillLab.eval.col.created"), value: fmtTime(job.created_at) },
    { label: t("skillLab.eval.field.elapsed"), value: elapsed(job) },
    { label: t("v2.skillLab.finishedAt"), value: fmtTime(job.finished_at) },
    ...extra,
  ];
  return <Descriptions items={items} />;
}

/** Live progress phrase + queue position + log tail while a job runs. */
export function LiveProgress({ job }: { job: SkillLabJobInfo }) {
  const { t } = useTranslation();
  return (
    <Card
      title={t("skillLab.eval.progress")}
      end={
        <span className="v2-row">
          <Tag tone="blue">{job.progress || job.status}</Tag>
          {job.status === "queued" && job.queue_position > 0 && (
            <span className="v2-muted">{t("skillLab.eval.queuedAt", { n: job.queue_position })}</span>
          )}
        </span>
      }
      testId="v2-skilllab-progress"
    >
      <JobLog jobId={job.id} live />
    </Card>
  );
}

export function EvalDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { job, setJob, missing, error } = useJob(id);
  const [results, setResults] = useState<SkillLabJobResults | null>(null);
  const [resultsPending, setResultsPending] = useState(true);
  const [confirm, setConfirm] = useState<"cancel" | "delete" | null>(null);
  const [busy, setBusy] = useState(false);
  const back = () => setParams({ tab: "eval" });

  // Results appear the moment the CLI writes results.json, so a terminal status
  // is the trigger, and a 404 is a normal answer (cancelled / failed-before-scoring).
  const status = job?.status ?? null;
  useEffect(() => {
    if (status === null || LIVE_STATUSES.includes(status)) {
      setResults(null);
      return;
    }
    let stale = false;
    setResultsPending(true);
    api
      .skillLabJobResults(id)
      .then((data) => {
        if (stale) return;
        setResults(data);
        passRateCache.set(id, data.summary.pass_rate);
      })
      .catch(() => {
        if (!stale) setResults(null);
      })
      .finally(() => {
        if (!stale) setResultsPending(false);
      });
    return () => {
      stale = true;
    };
  }, [id, status]);

  if (missing) return <GoneCard title={t("skillLab.eval.gone.title")} body={t("skillLab.eval.gone.body")} onBack={back} />;
  if (!job) return error ? <Alert tone="error">{error}</Alert> : <Spin />;
  const live = isLiveJob(job);

  const act = async () => {
    setBusy(true);
    try {
      if (confirm === "cancel") {
        setJob(await api.skillLabJobCancel(job.id));
        toast("success", t("skillLab.eval.cancelRequested"));
      } else if (confirm === "delete") {
        await api.skillLabJobDelete(job.id);
        toast("success", t("skillLab.eval.deleted"));
        back();
      }
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setConfirm(null);
    }
  };

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {job.skill_source?.name ?? t("skillLab.eval.unknownSkill")}
            <JobStatusTag job={job} />
          </span>
        }
        onBack={back}
        end={
          live ? (
            <Button kind="danger" disabled={busy} onClick={() => setConfirm("cancel")} testId="v2-eval-cancel">
              {t("v2.skillLab.cancelRun")}
            </Button>
          ) : (
            <Button kind="danger" disabled={busy} onClick={() => setConfirm("delete")} testId="v2-eval-delete">
              {t("v2.common.delete")}
            </Button>
          )
        }
      />
      {job.error && (
        <Alert tone="error">
          <span style={{ whiteSpace: "pre-wrap" }}>{job.error}</span>
        </Alert>
      )}
      <Card title={t("v2.skillLab.overview")}>
        <JobOverview job={job} />
      </Card>
      {live && <LiveProgress job={job} />}
      {!live &&
        (results !== null ? (
          <EvalResults results={results} />
        ) : (
          <Card title={t("v2.skillLab.taskResults")}>
            {resultsPending ? <Spin /> : <Alert>{t("skillLab.eval.noResults")}</Alert>}
          </Card>
        ))}
      <Card title={t("skillLab.eval.artifacts.title")}>
        <ArtifactBrowser jobId={job.id} live={live} />
      </Card>
      {!live && (
        <Card title={t("skillLab.eval.log.title")}>
          <JobLog jobId={job.id} live={false} />
        </Card>
      )}
      <Confirm
        open={confirm !== null}
        title={confirm === "cancel" ? t("skillLab.eval.confirmCancel.title") : t("skillLab.eval.confirmDelete.title")}
        body={
          confirm === "cancel"
            ? t("skillLab.eval.confirmCancel.body")
            : t("skillLab.eval.confirmDelete.body", { name: job.skill_source?.name ?? job.id })
        }
        confirmLabel={confirm === "cancel" ? t("v2.skillLab.cancelRun") : t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void act()}
        onClose={() => setConfirm(null)}
      />
    </>
  );
}
