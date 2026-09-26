import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { DiffPanes } from "../../../components/DiffPanes";
import {
  api,
  errorMessage,
  type RegistryRecordOut,
  type SkillLabPublishResult,
  type SkillLabSkillDiff,
  type SkillLabTrainStep,
  type SkillLabTrainSummary,
} from "../../../lib/api";
import {
  extraText,
  isLiveJob,
  JOB_POLL_MS,
  LIVE_STATUSES,
  RESUMABLE_STATUSES,
  saveBlob,
  stepVerdict,
} from "../../../lib/skillLab";
import { useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  Confirm,
  Descriptions,
  Drawer,
  FlowHeader,
  Kpi,
  LinkButton,
  Modal,
  Spin,
  Table,
  Tag,
  type TagTone,
} from "../../ui";
import { ArtifactBrowser } from "./ArtifactBrowser";
import { bestScoreCache, useJob } from "./state";
import { GoneCard, JobStatusTag } from "./common";
import { JobOverview, LiveProgress } from "./EvalDetail";
import { JobLog } from "./JobLog";
import { TrainCurve } from "./TrainCurve";

const score = (value: number | null | undefined) => (typeof value === "number" ? value.toFixed(3) : "—");
const wallTime = (value: number | null | undefined) => (typeof value === "number" ? `${(value / 60).toFixed(1)}m` : "—");
const stepWall = (value: number | null) =>
  typeof value !== "number" ? "—" : value < 90 ? `${value.toFixed(0)}s` : `${(value / 60).toFixed(1)}m`;

const VERDICT_TONE: Record<string, TagTone> = { accept: "green", reject: "red", skip: "gray", other: "orange" };

type Pending = "cancel" | "resume" | "delete";

/** Per-step optimizer timeline: one row per gate decision; reasons open in a drawer. */
function Timeline({ steps, bestStep }: { steps: SkillLabTrainStep[]; bestStep: number | null }) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<SkillLabTrainStep | null>(null);
  const extras = (step: SkillLabTrainStep) =>
    (
      [
        ["gateReasons", extraText(step.gate_reasons)],
        ["excludedFailures", extraText(step.excluded_failures)],
      ] as [string, string | null][]
    ).filter((entry): entry is [string, string] => entry[1] !== null);
  return (
    <>
      <Table
        testId="v2-train-timeline"
        rows={steps}
        rowKey={(step) => String(step.step ?? steps.indexOf(step))}
        empty={t("skillLab.train.curve.waiting")}
        columns={[
          {
            key: "step",
            title: t("skillLab.train.col.step"),
            render: (step: SkillLabTrainStep) => (
              <span className="v2-row" style={{ flexWrap: "nowrap" }}>
                {t("skillLab.train.stepLabel", { n: step.step ?? steps.indexOf(step) + 1 })}
                {step.epoch !== null && <span className="v2-muted">{t("skillLab.train.epochLabel", { n: step.epoch })}</span>}
                {step.step !== null && step.step === bestStep && <Tag tone="green">★ {t("skillLab.train.bestTag")}</Tag>}
              </span>
            ),
          },
          {
            key: "gate",
            title: t("skillLab.train.col.gate"),
            render: (step: SkillLabTrainStep) => {
              const v = stepVerdict(step.action);
              return (
                <Tag tone={VERDICT_TONE[v]} title={step.action ?? undefined}>
                  {t(`skillLab.train.gate.${v}`)}
                </Tag>
              );
            },
          },
          { key: "hard", title: t("skillLab.train.col.hard"), className: "num", render: (step: SkillLabTrainStep) => score(step.selection_hard) },
          { key: "soft", title: t("skillLab.train.col.soft"), className: "num", render: (step: SkillLabTrainStep) => score(step.selection_soft) },
          { key: "len", title: t("skillLab.train.col.skillLen"), className: "num", render: (step: SkillLabTrainStep) => step.skill_len ?? "—" },
          { key: "wall", title: t("skillLab.train.col.wall"), className: "num", render: (step: SkillLabTrainStep) => stepWall(step.wall_time_s) },
          {
            key: "ops",
            title: t("v2.common.actions"),
            className: "right",
            render: (step: SkillLabTrainStep) =>
              extras(step).length > 0 ? (
                <LinkButton onClick={() => setOpen(step)}>{t("v2.common.detail")}</LinkButton>
              ) : (
                <span className="v2-muted">—</span>
              ),
          },
        ]}
      />
      <Drawer
        open={open !== null}
        title={open ? t("skillLab.train.stepLabel", { n: open.step ?? steps.indexOf(open) + 1 }) : ""}
        onClose={() => setOpen(null)}
        testId="v2-train-step-drawer"
      >
        {open && (
          <div className="v2-stack">
            <Descriptions
              one
              items={[
                { label: t("skillLab.train.col.gate"), value: <span className="mono">{open.action ?? "—"}</span> },
                { label: t("skillLab.train.col.hard"), value: score(open.selection_hard) },
                { label: t("skillLab.train.col.soft"), value: score(open.selection_soft) },
              ]}
            />
            {extras(open).map(([key, value]) => (
              <div key={key}>
                <h3 className="v2-sub-title">{t(`skillLab.train.detail.${key}`)}</h3>
                <pre className="v2-pre">{value}</pre>
              </div>
            ))}
          </div>
        )}
      </Drawer>
    </>
  );
}

export function TrainDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const { job, setJob, missing, error } = useJob(id);
  const [summary, setSummary] = useState<SkillLabTrainSummary | null>(null);
  const [diff, setDiff] = useState<SkillLabSkillDiff | null>(null);
  const [record, setRecord] = useState<RegistryRecordOut | null>(null);
  const [pending, setPending] = useState<Pending | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  // checked by default only for a record the Registry reports as APPROVED right now
  const [reapprove, setReapprove] = useState(false);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [published, setPublished] = useState<SkillLabPublishResult | null>(null);
  const [busy, setBusy] = useState(false);
  const back = () => setParams({ tab: "train" });

  const status = job?.status ?? null;
  const live = status !== null && LIVE_STATUSES.includes(status);

  // history.json grows per step, so the timeline polls while the job is live and
  // fetches once more when it settles; a 404 is normal until the first step lands.
  useEffect(() => {
    if (status === null) return;
    let stale = false;
    const fetchSummary = async () => {
      try {
        const data = await api.skillLabJobTrainSummary(id);
        if (stale) return;
        setSummary(data);
        if (typeof data.best_score === "number") bestScoreCache.set(id, data.best_score);
      } catch {
        /* not written yet, or transient — keep what is on screen */
      }
    };
    void fetchSummary();
    if (!live) {
      return () => {
        stale = true;
      };
    }
    const timer = window.setInterval(() => void fetchSummary(), JOB_POLL_MS);
    return () => {
      stale = true;
      window.clearInterval(timer);
    };
  }, [id, status, live]);

  // The diff needs both skill files, which only exist once the run is over.
  useEffect(() => {
    if (status === null || live) return;
    let stale = false;
    api
      .skillLabJobDiff(id)
      .then((data) => {
        if (!stale) setDiff(data);
      })
      .catch(() => {
        /* no best_skill.md — the run never completed a step */
      });
    return () => {
      stale = true;
    };
  }, [id, status, live]);

  // The record's live status decides whether re-approving is meaningful, and its
  // current version is what the bump applies to.
  const publishRecordId =
    job?.status === "succeeded" && job.skill_source?.kind === "registry" ? (job.skill_source.record_id ?? null) : null;
  useEffect(() => {
    if (publishRecordId === null) return;
    let stale = false;
    api
      .v2SkillLabRecord(publishRecordId)
      .then((body) => {
        if (stale) return;
        setRecord(body);
        setReapprove(body.status === "APPROVED");
      })
      .catch(() => {
        /* the panel degrades to the source metadata the job recorded */
      });
    return () => {
      stale = true;
    };
  }, [publishRecordId]);

  if (missing) return <GoneCard title={t("skillLab.eval.gone.title")} body={t("skillLab.eval.gone.body")} onBack={back} />;
  if (!job) return error ? <Alert tone="error">{error}</Alert> : <Spin />;

  const uploadSourced = job.skill_source?.kind === "upload";
  const publishable = job.status === "succeeded" && !uploadSourced && diff?.changed === true;

  const act = async () => {
    setBusy(true);
    try {
      if (pending === "cancel") {
        setJob(await api.skillLabJobCancel(job.id));
        toast("success", t("skillLab.eval.cancelRequested"));
      } else if (pending === "resume") {
        setJob(await api.skillLabJobResume(job.id));
        toast("success", t("skillLab.train.resumed"));
      } else if (pending === "delete") {
        await api.skillLabJobDelete(job.id);
        toast("success", t("skillLab.eval.deleted"));
        back();
      }
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
      setPending(null);
    }
  };

  const publish = async () => {
    setBusy(true);
    setPublishError(null);
    try {
      const result = await api.skillLabJobPublish(job.id, reapprove);
      toast(
        "success",
        t("skillLab.train.publish.done", {
          name: result.name ?? job.skill_source?.name ?? "",
          version: result.new_version,
          status: result.status_after,
        }),
      );
      setPublished(result);
      setPublishOpen(false);
      // the record just changed version and (unless re-approved) status
      api
        .v2SkillLabRecord(result.record_id)
        .then(setRecord)
        .catch(() => undefined);
    } catch (err) {
      setPublishError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const downloadBest = async () => {
    try {
      saveBlob(await api.skillLabJobArtifactRaw(job.id, "best_skill.md"), "best_skill.md");
    } catch (err) {
      toast("error", errorMessage(err));
    }
  };

  const loopItems = [
    {
      label: t("skillLab.train.field.loop"),
      value: (
        <span data-testid="v2-train-loop">
          {t("skillLab.train.field.epochs", { n: job.params.epochs ?? 1 })} · {t("skillLab.train.field.lr", { n: job.params.learning_rate ?? 4 })} ·{" "}
          {t("skillLab.train.field.gate", { metric: job.params.gate_metric ?? "hard" })}
        </span>
      ),
    },
    {
      label: t("skillLab.train.wizard.field.trainableFiles"),
      value: job.params.trainable_files?.length ? <span className="mono">{["SKILL.md", ...job.params.trainable_files].join(" · ")}</span> : "SKILL.md",
    },
  ];

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
          <>
            {isLiveJob(job) && (
              <Button kind="danger" disabled={busy} onClick={() => setPending("cancel")} testId="v2-train-cancel">
                {t("v2.skillLab.cancelRun")}
              </Button>
            )}
            {RESUMABLE_STATUSES.includes(job.status) && (
              <Button kind="primary" disabled={busy} onClick={() => setPending("resume")} testId="v2-train-resume">
                {t("skillLab.train.resume")}
              </Button>
            )}
            {!isLiveJob(job) && (
              <Button kind="danger" disabled={busy} onClick={() => setPending("delete")} testId="v2-train-delete">
                {t("v2.common.delete")}
              </Button>
            )}
          </>
        }
      />
      {job.error && (
        <Alert tone="error">
          <span style={{ whiteSpace: "pre-wrap" }}>{job.error}</span>
        </Alert>
      )}
      <Card title={t("v2.skillLab.overview")}>
        <JobOverview job={job} extra={loopItems} />
      </Card>
      {live && <LiveProgress job={job} />}

      {/* Timeline and curve render from whatever history.json holds, running or
          not — a killed run still shows the steps it did finish. */}
      {summary === null ? (
        <Card title={t("v2.skillLab.trainProgress")}>
          <Alert>{t("skillLab.train.curve.waiting")}</Alert>
        </Card>
      ) : (
        <>
          <div className="v2-kpis" data-testid="v2-train-kpis">
            <Kpi
              label={t("skillLab.train.stat.bestScore")}
              value={score(summary.best_score)}
              sub={
                summary.best_step !== null
                  ? t("skillLab.train.stat.bestFoot", { step: summary.best_step })
                  : t("skillLab.train.stat.bestFootNone")
              }
              tone={
                summary.best_score !== null && summary.baseline_selection_hard !== null && summary.best_score > summary.baseline_selection_hard
                  ? "good"
                  : undefined
              }
            />
            <Kpi label={t("skillLab.train.stat.baseline")} value={score(summary.baseline_selection_hard)} sub={t("skillLab.train.stat.baselineFoot")} />
            <Kpi
              label={t("skillLab.train.stat.steps")}
              value={String(summary.totals.steps ?? summary.steps.length)}
              sub={t("skillLab.train.stat.stepsFoot", {
                accepts: summary.totals.accepts ?? 0,
                rejects: summary.totals.rejects ?? 0,
                skips: summary.totals.skips ?? 0,
              })}
            />
            <Kpi label={t("skillLab.train.stat.wall")} value={wallTime(summary.totals.wall_time_s)} sub={t("skillLab.train.stat.wallFoot")} />
          </div>
          {(summary.test_scores.baseline !== null || summary.test_scores.final !== null) && (
            <Alert tone="info">
              {t("skillLab.train.testScores", { baseline: score(summary.test_scores.baseline), final: score(summary.test_scores.final) })}
            </Alert>
          )}
          <Card title={t("v2.skillLab.curve")}>
            <TrainCurve summary={summary} />
          </Card>
          <Card title={t("v2.skillLab.timeline")}>
            <Timeline steps={summary.steps} bestStep={summary.best_step} />
          </Card>
        </>
      )}

      {diff !== null && (
        <Card
          title={t("skillLab.train.diff.title")}
          end={<Tag tone={diff.changed ? "green" : "gray"}>{t(diff.changed ? "skillLab.train.diff.changed" : "skillLab.train.diff.unchanged")}</Tag>}
          testId="v2-train-diff"
        >
          <div className="v2-skilllab-diff">
            <DiffPanes before={diff.seed} after={diff.best} beforeLabel={t("skillLab.train.diff.seed")} afterLabel={t("skillLab.train.diff.best")} />
          </div>
        </Card>
      )}

      {job.status === "succeeded" && diff !== null && (
        <Card title={t("skillLab.train.publish.title")} testId="v2-train-publish">
          {uploadSourced ? (
            <Alert
              action={
                <Button size="sm" onClick={() => void downloadBest()} testId="v2-train-download-best">
                  {t("skillLab.train.publish.download")}
                </Button>
              }
            >
              {t("skillLab.train.publish.uploadSource")}
            </Alert>
          ) : (
            <>
              <Descriptions
                items={[
                  {
                    label: t("skillLab.train.publish.record"),
                    value: (
                      <span className="v2-row">
                        {record?.name ?? job.skill_source?.name ?? "—"}
                        <span className="mono v2-muted">{record?.version ?? job.skill_source?.version ?? ""}</span>
                        {record !== null && <Tag tone={record.status === "APPROVED" ? "green" : "gray"}>{record.status}</Tag>}
                      </span>
                    ),
                  },
                  { label: "Record ID", value: <span className="mono">{job.skill_source?.record_id ?? "—"}</span> },
                ]}
              />
              {publishError !== null && <Alert tone="error">{publishError}</Alert>}
              {published !== null && (
                <Alert
                  tone="success"
                  action={
                    <Button size="sm" onClick={() => navigate("/v2/registry")} testId="v2-train-open-registry">
                      {t("skillLab.train.publish.openRegistry")}
                    </Button>
                  }
                >
                  {t("skillLab.train.publish.done", {
                    name: published.name ?? "",
                    version: published.new_version,
                    status: published.status_after,
                  })}
                </Alert>
              )}
              <div className="v2-row" style={{ marginTop: 12 }}>
                <Button
                  kind="primary"
                  disabled={busy || !publishable}
                  onClick={() => {
                    setPublishError(null);
                    setPublishOpen(true);
                  }}
                  testId="v2-train-publish-btn"
                >
                  {t("skillLab.train.publish.action")}
                </Button>
                {!publishable && <span className="v2-muted">{t("skillLab.train.publish.noChange")}</span>}
              </div>
            </>
          )}
        </Card>
      )}

      <Card title={t("skillLab.eval.artifacts.title")}>
        <ArtifactBrowser jobId={job.id} live={live} />
      </Card>
      {!live && (
        <Card title={t("skillLab.eval.log.title")}>
          <JobLog jobId={job.id} live={false} />
        </Card>
      )}

      {/* publishing carries a choice (re-approve) the shared Confirm has no room for */}
      <Modal
        open={publishOpen}
        title={t("skillLab.train.publish.confirmTitle")}
        onClose={() => setPublishOpen(false)}
        testId="v2-train-publish-dialog"
        footer={
          <>
            <Button onClick={() => setPublishOpen(false)}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={busy} onClick={() => void publish()} testId="v2-train-publish-confirm">
              {t("skillLab.train.publish.action")}
            </Button>
          </>
        }
      >
        <div className="v2-stack">
          <p style={{ margin: 0 }}>
            {t("skillLab.train.publish.confirmBody", {
              name: record?.name ?? job.skill_source?.name ?? "",
              version: record?.version ?? job.skill_source?.version ?? "—",
            })}
          </p>
          <Alert tone="warn">{t("skillLab.train.publish.confirmDraft")}</Alert>
          <label className="v2-check">
            <input type="checkbox" checked={reapprove} onChange={(e) => setReapprove(e.target.checked)} data-testid="v2-train-reapprove" />
            {t("skillLab.train.publish.reapprove")}
          </label>
          {record !== null && record.status !== "APPROVED" && reapprove && (
            <span className="v2-muted">{t("skillLab.train.publish.reapproveNoop", { status: record.status })}</span>
          )}
          {publishError !== null && <Alert tone="error">{publishError}</Alert>}
        </div>
      </Modal>

      <Confirm
        open={pending !== null}
        title={
          pending === "resume"
            ? t("skillLab.train.confirmResume.title")
            : pending === "cancel"
              ? t("skillLab.train.confirmCancel.title")
              : t("skillLab.train.confirmDelete.title")
        }
        body={
          pending === "resume"
            ? t("skillLab.train.confirmResume.body")
            : pending === "cancel"
              ? t("skillLab.eval.confirmCancel.body")
              : t("skillLab.eval.confirmDelete.body", { name: job.skill_source?.name ?? job.id })
        }
        confirmLabel={
          pending === "resume" ? t("skillLab.train.resume") : pending === "cancel" ? t("v2.skillLab.cancelRun") : t("v2.common.delete")
        }
        danger={pending !== "resume"}
        busy={busy}
        onConfirm={() => void act()}
        onClose={() => setPending(null)}
      />
    </>
  );
}
