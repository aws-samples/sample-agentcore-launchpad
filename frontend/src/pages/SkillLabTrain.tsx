import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import {
  Btn,
  Chip,
  ConfirmDialog,
  DiffPanes,
  Pager,
  Panel,
  StatTile,
  useTablePage,
  useToast,
} from "../components";
import type { ChipTone } from "../components";
import type {
  SkillLabJobInfo,
  SkillLabPublishResult,
  SkillLabSkillDiff,
  SkillLabStatus,
  SkillLabTrainSummary,
} from "../lib/api";
import { api, ApiError } from "../lib/api";
import type { RegistryRecord } from "./Registry";
import { ArtifactBrowser } from "./skillLab/ArtifactBrowser";
import { JobLogPane } from "./skillLab/JobLogPane";
import { TrainCurve } from "./skillLab/TrainCurve";
import { TrainTimeline } from "./skillLab/TrainTimeline";
import { TrainWizard } from "./skillLab/TrainWizard";

const LIST_POLL_MS = 8000;
const JOB_POLL_MS = 2500;

const STATUS_CHIP: Record<string, { tone: ChipTone; icon: string }> = {
  queued: { tone: "muted", icon: "◍" },
  running: { tone: "warn", icon: "◐" },
  succeeded: { tone: "good", icon: "●" },
  failed: { tone: "crit", icon: "✕" },
  cancelled: { tone: "muted", icon: "✕" },
  interrupted: { tone: "warn", icon: "!" },
};

const LIVE_STATUSES = ["queued", "running"];
const RESUMABLE_STATUSES = ["interrupted", "failed"];

const isLive = (job: SkillLabJobInfo) => LIVE_STATUSES.includes(job.status);

const stamp = (value: string | null) => (value ? new Date(value).toLocaleString() : "—");

const score = (value: number | null | undefined) =>
  typeof value === "number" ? value.toFixed(3) : "—";

function elapsed(job: SkillLabJobInfo): string {
  if (!job.started_at) return "—";
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const seconds = Math.max(0, (end.getTime() - new Date(job.started_at).getTime()) / 1000);
  return seconds < 90 ? `${seconds.toFixed(0)}s` : `${(seconds / 60).toFixed(1)}m`;
}

const wallTime = (value: number | null | undefined) =>
  typeof value === "number" ? `${(value / 60).toFixed(1)}m` : "—";

/**
 * Optimization half of the Skill Lab (`?view=train`): the training job list, the
 * submit wizard (`job=new`, deep-linkable with `record=`) and the per-job detail
 * — live step timeline and score curve while the trainer runs, then the SEED →
 * BEST diff and the publish-back-to-Registry action.
 */
export function SkillLabTrain({ status }: { status: SkillLabStatus | null }) {
  const { t } = useTranslation();
  const toast = useToast();
  const navigate = useNavigate();
  const [searchParams, setSearchParams] = useSearchParams();
  const jobParam = searchParams.get("job");
  const recordParam = searchParams.get("record");
  const creating = jobParam === "new";

  const [rows, setRows] = useState<SkillLabJobInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState<string | null>(null);
  const [detail, setDetail] = useState<SkillLabJobInfo | null>(null);
  // Set only when the backend answered "no such job" — distinguishing a dead
  // deep link from a detail fetch that simply has not landed yet.
  const [detailMissing, setDetailMissing] = useState(false);
  const [summary, setSummary] = useState<SkillLabTrainSummary | null>(null);
  const [diff, setDiff] = useState<SkillLabSkillDiff | null>(null);
  const [record, setRecord] = useState<RegistryRecord | null>(null);
  const [confirmCancel, setConfirmCancel] = useState<SkillLabJobInfo | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<SkillLabJobInfo | null>(null);
  const [confirmResume, setConfirmResume] = useState<SkillLabJobInfo | null>(null);
  const [publishOpen, setPublishOpen] = useState(false);
  // Checked only for a record the Registry currently reports as APPROVED.
  const [reapprove, setReapprove] = useState(false);
  const [publishError, setPublishError] = useState<string | null>(null);
  const [published, setPublished] = useState<SkillLabPublishResult | null>(null);
  const [busy, setBusy] = useState(false);
  // Best scores come from a job's own summary file, which the list deliberately
  // does not read (one file per row); scores seen while browsing are cached so
  // the column fills in as the user clicks around.
  const [bestScores, setBestScores] = useState<Record<string, number>>({});

  const load = useCallback(async () => {
    try {
      setRows(await api.skillLabJobs("train"));
      setListError(null);
    } catch (err) {
      setListError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    void load();
    const timer = setInterval(() => void load(), LIST_POLL_MS);
    return () => clearInterval(timer);
  }, [load]);

  // Selection: fetch once immediately, then poll only while the job is live.
  const liveRef = useRef(false);
  liveRef.current = detail !== null && isLive(detail);

  useEffect(() => {
    setDetailMissing(false);
    if (!jobParam || jobParam === "new") {
      setDetail(null);
      return;
    }
    let stale = false;
    const fetchJob = async () => {
      try {
        const job = await api.skillLabJobGet(jobParam);
        if (stale) return;
        setDetail(job);
        // Keep the list row in step with the detail for free: a job that
        // finishes between list polls would otherwise sit at RUNNING for 8 s.
        setRows((prev) => prev.map((row) => (row.id === job.id ? job : row)));
      } catch (err) {
        if (stale) return;
        if (err instanceof ApiError && err.code === "skill_lab.job_not_found") {
          setDetail(null);
          setDetailMissing(true);
        } else {
          toast(
            t("common.actionFailed", {
              msg: err instanceof ApiError ? err.message : String(err),
            }),
          );
        }
      }
    };
    void fetchJob();
    const timer = setInterval(() => {
      if (liveRef.current) void fetchJob();
    }, JOB_POLL_MS);
    return () => {
      stale = true;
      clearInterval(timer);
    };
  }, [jobParam, t, toast]);

  // Drop the previous job's artifacts on selection change. Kept apart from the
  // fetch below so a transient poll error never blanks a rendered timeline.
  useEffect(() => {
    setSummary(null);
    setDiff(null);
    setRecord(null);
    setPublishError(null);
    setPublished(null);
    setPublishOpen(false);
    // Re-derived from the newly selected job's record below; checked by default
    // only for a record that is APPROVED right now.
    setReapprove(false);
  }, [jobParam]);

  // The timeline is readable mid-run (history.json grows per step), so this
  // polls while the job is live and fetches once more when it settles. A 404 is
  // the normal answer until the first step lands.
  const detailId = detail?.id ?? null;
  const detailStatus = detail?.status ?? null;
  const detailLive = detailStatus !== null && LIVE_STATUSES.includes(detailStatus);
  useEffect(() => {
    if (detailId === null) return;
    let stale = false;
    const fetchSummary = async () => {
      try {
        const data = await api.skillLabJobTrainSummary(detailId);
        if (!stale) setSummary(data);
      } catch {
        /* not written yet, or a transient failure — keep what is on screen */
      }
    };
    void fetchSummary();
    if (!detailLive) {
      return () => {
        stale = true;
      };
    }
    const timer = setInterval(() => void fetchSummary(), JOB_POLL_MS);
    return () => {
      stale = true;
      clearInterval(timer);
    };
  }, [detailId, detailLive]);

  // The diff needs both skill files, which only exist once the run is over.
  useEffect(() => {
    if (detailId === null || detailStatus === null || LIVE_STATUSES.includes(detailStatus)) return;
    let stale = false;
    api
      .skillLabJobDiff(detailId)
      .then((data) => {
        if (!stale) setDiff(data);
      })
      .catch(() => {
        /* no best_skill.md — the run never completed a step */
      });
    return () => {
      stale = true;
    };
  }, [detailId, detailStatus]);

  useEffect(() => {
    if (summary !== null && detailId !== null && typeof summary.best_score === "number") {
      const best = summary.best_score;
      setBestScores((prev) => (prev[detailId] === best ? prev : { ...prev, [detailId]: best }));
    }
  }, [summary, detailId]);

  // The record's live status decides whether re-approving after publish is even
  // meaningful, and its current version is what the bump applies to.
  // Re-read per selection, not per record id: two runs can optimize the same
  // record, and the reset above blanks `record`, so keying on the id alone would
  // leave the panel without live status when the user walks between such runs.
  const publishRecordId =
    detail?.status === "succeeded" && detail.skill_source?.kind === "registry"
      ? (detail.skill_source.record_id ?? null)
      : null;
  useEffect(() => {
    if (publishRecordId === null || detailId === null) return;
    let stale = false;
    fetch(`/api/registry/records/${encodeURIComponent(publishRecordId)}`)
      .then(async (res) => {
        if (!res.ok || stale) return;
        const body = (await res.json()) as RegistryRecord;
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
  }, [publishRecordId, detailId]);

  const select = (id: string | null) => {
    setSearchParams(id ? { view: "train", job: id } : { view: "train" });
  };

  const selectedIndex = rows.findIndex((row) => row.id === jobParam);
  const { rows: pageRows, pagerProps } = useTablePage(rows, selectedIndex);

  const cancel = async (job: SkillLabJobInfo) => {
    setBusy(true);
    try {
      setDetail(await api.skillLabJobCancel(job.id));
      toast(t("skillLab.eval.cancelRequested"));
      await load();
    } catch (err) {
      toast(t("common.actionFailed", { msg: err instanceof ApiError ? err.message : String(err) }));
    } finally {
      setBusy(false);
    }
  };

  const remove = async (job: SkillLabJobInfo) => {
    setBusy(true);
    try {
      await api.skillLabJobDelete(job.id);
      toast(t("skillLab.eval.deleted"));
      if (jobParam === job.id) select(null);
      await load();
    } catch (err) {
      toast(t("common.actionFailed", { msg: err instanceof ApiError ? err.message : String(err) }));
    } finally {
      setBusy(false);
    }
  };

  const resume = async (job: SkillLabJobInfo) => {
    setBusy(true);
    try {
      setDetail(await api.skillLabJobResume(job.id));
      toast(t("skillLab.train.resumed"));
      await load();
    } catch (err) {
      toast(t("common.actionFailed", { msg: err instanceof ApiError ? err.message : String(err) }));
    } finally {
      setBusy(false);
    }
  };

  const publish = async (job: SkillLabJobInfo) => {
    setBusy(true);
    setPublishError(null);
    try {
      const result = await api.skillLabJobPublish(job.id, reapprove);
      toast(
        t("skillLab.train.publish.done", {
          name: result.name ?? job.skill_source?.name ?? "",
          version: result.new_version,
          status: result.status_after,
        }),
      );
      setPublished(result);
      setPublishOpen(false);
      // The record just changed version and (unless re-approved) status.
      fetch(`/api/registry/records/${encodeURIComponent(result.record_id)}`)
        .then(async (res) => {
          if (res.ok) setRecord((await res.json()) as RegistryRecord);
        })
        .catch(() => {
          /* the toast already carried the outcome */
        });
    } catch (err) {
      setPublishError(err instanceof ApiError ? err.message : String(err));
    } finally {
      setBusy(false);
    }
  };

  const downloadBest = async (job: SkillLabJobInfo) => {
    try {
      const blob = await api.skillLabJobArtifactRaw(job.id, "best_skill.md");
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      link.download = "best_skill.md";
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (err) {
      toast(t("common.actionFailed", { msg: err instanceof ApiError ? err.message : String(err) }));
    }
  };

  const statusChip = (job: SkillLabJobInfo) => {
    const chip = STATUS_CHIP[job.status] ?? STATUS_CHIP.queued;
    const queued = job.status === "queued" && job.queue_position > 0;
    return (
      <Chip tone={chip.tone} icon={chip.icon}>
        {t(`skillLab.eval.status.${job.status}`)}
        {queued ? ` #${job.queue_position}` : ""}
      </Chip>
    );
  };

  /* ── detail ─────────────────────────────────────────────────────────────── */

  const uploadSourced = detail?.skill_source?.kind === "upload";
  const publishable =
    detail !== null && detail.status === "succeeded" && !uploadSourced && diff?.changed === true;

  const detailPanel = detail && (
    <Panel
      brk
      title={detail.skill_source?.name ?? t("skillLab.eval.unknownSkill")}
      sub={`${detail.id} · ${detail.taskset_name}`}
      end={
        <>
          {statusChip(detail)}
          {isLive(detail) && (
            <Btn
              disabled={busy}
              data-testid="train-cancel"
              onClick={() => setConfirmCancel(detail)}
            >
              {t("skillLab.eval.cancel")}
            </Btn>
          )}
          {RESUMABLE_STATUSES.includes(detail.status) && (
            <Btn
              primary
              disabled={busy}
              data-testid="train-resume"
              onClick={() => setConfirmResume(detail)}
            >
              {t("skillLab.train.resume")}
            </Btn>
          )}
          {!isLive(detail) && (
            <Btn
              disabled={busy}
              data-testid="train-delete"
              onClick={() => setConfirmDelete(detail)}
            >
              {t("skillLab.eval.delete")}
            </Btn>
          )}
        </>
      }
      style={{ "--i": 1 } as CSSProperties}
    >
      <div className="kv">
        <span className="k mono">{t("skillLab.eval.field.skillSource")}</span>
        <span className="v">
          {detail.skill_source ? (
            <>
              <Chip tone={detail.skill_source.kind === "registry" ? "aqua" : "muted"}>
                {t(`skillLab.eval.wizard.source.${detail.skill_source.kind}`)}
              </Chip>{" "}
              <span className="mono dim">{detail.skill_source.version || ""}</span>
            </>
          ) : (
            "—"
          )}
        </span>
      </div>
      <div className="kv">
        <span className="k mono">{t("skillLab.train.field.loop")}</span>
        <span className="v mono" style={{ fontSize: 10.5 }} data-testid="train-loop-params">
          {t("skillLab.train.field.epochs", { n: detail.params.epochs ?? 1 })} ·{" "}
          {t("skillLab.train.field.lr", { n: detail.params.learning_rate ?? 4 })} ·{" "}
          {t("skillLab.train.field.gate", { metric: detail.params.gate_metric ?? "hard" })}
        </span>
      </div>
      <div className="kv">
        <span className="k mono">{t("skillLab.eval.field.models")}</span>
        <span className="v mono" style={{ fontSize: 10.5 }}>
          {t("skillLab.eval.field.target")} {detail.params.target_model} ·{" "}
          {t("skillLab.train.field.optimizer")} {detail.params.judge_model}
        </span>
      </div>
      <div className="kv">
        <span className="k mono">{t("skillLab.eval.field.execution")}</span>
        <span className="v mono" style={{ fontSize: 10.5 }}>
          workers {detail.params.workers} · timeout {detail.params.timeout}s
          {detail.params.limit > 0 ? ` · limit ${detail.params.limit}` : ""}
        </span>
      </div>
      <div className="kv">
        <span className="k mono">{t("skillLab.eval.col.created")}</span>
        <span className="v mono">{stamp(detail.created_at)}</span>
      </div>
      <div className="kv">
        <span className="k mono">{t("skillLab.eval.field.elapsed")}</span>
        <span className="v mono">{elapsed(detail)}</span>
      </div>

      {isLive(detail) && (
        <div style={{ marginTop: 12 }}>
          <div
            style={{ display: "flex", gap: 8, alignItems: "center", marginBottom: 6 }}
            data-testid="train-progress"
          >
            <span className="mono" style={{ fontSize: 11, letterSpacing: ".08em" }}>
              {t("skillLab.eval.progress")}
            </span>
            <Chip tone="amber">{detail.progress}</Chip>
            {detail.status === "queued" && detail.queue_position > 0 && (
              <span className="mono dim" style={{ fontSize: 10.5 }}>
                {t("skillLab.eval.queuedAt", { n: detail.queue_position })}
              </span>
            )}
          </div>
          <JobLogPane jobId={detail.id} live />
        </div>
      )}

      {detail.error && (
        <div
          className="note"
          data-testid="train-job-error"
          style={{ borderColor: "var(--crit)", marginTop: 12 }}
        >
          <span className="i" style={{ color: "var(--crit)" }}>
            [✕]
          </span>
          <span className="mono" style={{ fontSize: 10.5, whiteSpace: "pre-wrap" }}>
            {detail.error}
          </span>
        </div>
      )}

      {/* Timeline and curve render from whatever history.json holds, running or
          not — a killed run still shows the steps it did finish. */}
      <div style={{ marginTop: 14 }} data-testid="train-run-view">
        {summary === null ? (
          <div className="empty" data-testid="train-no-steps">
            {t("skillLab.train.curve.waiting")}
          </div>
        ) : (
          <>
            <div className="tiles" data-testid="train-summary-tiles">
              <StatTile
                label={t("skillLab.train.stat.bestScore")}
                value={score(summary.best_score)}
                foot={
                  summary.best_step !== null
                    ? t("skillLab.train.stat.bestFoot", { step: summary.best_step })
                    : t("skillLab.train.stat.bestFootNone")
                }
                style={{ "--i": 0 } as CSSProperties}
              />
              <StatTile
                label={t("skillLab.train.stat.baseline")}
                value={score(summary.baseline_selection_hard)}
                foot={t("skillLab.train.stat.baselineFoot")}
                style={{ "--i": 1 } as CSSProperties}
              />
              <StatTile
                label={t("skillLab.train.stat.steps")}
                value={String(summary.totals.steps ?? summary.steps.length)}
                foot={t("skillLab.train.stat.stepsFoot", {
                  accepts: summary.totals.accepts ?? 0,
                  rejects: summary.totals.rejects ?? 0,
                  skips: summary.totals.skips ?? 0,
                })}
                style={{ "--i": 2 } as CSSProperties}
              />
              <StatTile
                label={t("skillLab.train.stat.wall")}
                value={wallTime(summary.totals.wall_time_s)}
                foot={t("skillLab.train.stat.wallFoot")}
                style={{ "--i": 3 } as CSSProperties}
              />
            </div>

            {(summary.test_scores.baseline !== null || summary.test_scores.final !== null) && (
              <div className="note" style={{ marginBottom: 10 }} data-testid="train-test-scores">
                <span className="i">[=]</span>
                <span>
                  {t("skillLab.train.testScores", {
                    baseline: score(summary.test_scores.baseline),
                    final: score(summary.test_scores.final),
                  })}
                </span>
              </div>
            )}

            <TrainCurve summary={summary} />
            <div style={{ marginTop: 12 }}>
              <TrainTimeline steps={summary.steps} bestStep={summary.best_step} />
            </div>
          </>
        )}
      </div>

      {diff !== null && (
        <div style={{ marginTop: 14 }} data-testid="train-diff">
          <div
            style={{
              display: "flex",
              gap: 8,
              alignItems: "center",
              marginBottom: 6,
            }}
          >
            <span className="mono" style={{ fontSize: 11, letterSpacing: ".08em" }}>
              {t("skillLab.train.diff.title")}
            </span>
            <Chip tone={diff.changed ? "good" : "muted"}>
              {t(diff.changed ? "skillLab.train.diff.changed" : "skillLab.train.diff.unchanged")}
            </Chip>
          </div>
          <DiffPanes
            before={diff.seed}
            after={diff.best}
            beforeLabel={t("skillLab.train.diff.seed")}
            afterLabel={t("skillLab.train.diff.best")}
          />
          {diff.changed && (
            <pre
              className="code"
              data-testid="train-diff-unified"
              style={{
                marginTop: 8,
                maxHeight: 260,
                overflow: "auto",
                whiteSpace: "pre-wrap",
                overflowWrap: "anywhere",
                fontSize: 10.5,
              }}
            >
              {diff.diff}
            </pre>
          )}
        </div>
      )}

      {detail.status === "succeeded" && diff !== null && (
        <div style={{ marginTop: 14 }} data-testid="train-publish-panel">
          <div className="mono" style={{ fontSize: 11, letterSpacing: ".08em", marginBottom: 6 }}>
            {t("skillLab.train.publish.title")}
          </div>
          {uploadSourced ? (
            <div className="note">
              <span className="i">[i]</span>
              <span style={{ flex: 1 }}>{t("skillLab.train.publish.uploadSource")}</span>
              <Btn data-testid="train-download-best" onClick={() => void downloadBest(detail)}>
                {t("skillLab.train.publish.download")}
              </Btn>
            </div>
          ) : (
            <>
              <div className="kv">
                <span className="k mono">{t("skillLab.train.publish.record")}</span>
                <span className="v">
                  {record?.name ?? detail.skill_source?.name ?? "—"}{" "}
                  <span className="mono dim">
                    {record?.version ?? detail.skill_source?.version ?? ""}
                  </span>{" "}
                  {record !== null && <Chip tone="muted">{record.status}</Chip>}
                </span>
              </div>
              {publishError !== null && (
                <div
                  className="note"
                  data-testid="train-publish-error"
                  style={{ borderColor: "var(--crit)", marginTop: 8 }}
                >
                  <span className="i" style={{ color: "var(--crit)" }}>
                    [✕]
                  </span>
                  <span className="mono" style={{ fontSize: 10.5 }}>
                    {publishError}
                  </span>
                </div>
              )}
              {published !== null && (
                <div
                  className="note"
                  data-testid="train-published"
                  style={{ borderColor: "var(--good)", marginTop: 8 }}
                >
                  <span className="i" style={{ color: "var(--good)" }}>
                    [✓]
                  </span>
                  <span style={{ flex: 1 }}>
                    {t("skillLab.train.publish.done", {
                      name: published.name ?? "",
                      version: published.new_version,
                      status: published.status_after,
                    })}
                  </span>
                  <Btn data-testid="train-open-registry" onClick={() => navigate("/registry")}>
                    {t("skillLab.train.publish.openRegistry")}
                  </Btn>
                </div>
              )}
              <div style={{ display: "flex", gap: 8, marginTop: 8, alignItems: "center" }}>
                <Btn
                  primary
                  disabled={busy || !publishable}
                  data-testid="train-publish"
                  onClick={() => {
                    setPublishError(null);
                    setPublishOpen(true);
                  }}
                >
                  {t("skillLab.train.publish.action")}
                </Btn>
                {!publishable && (
                  <span className="mono dim" style={{ fontSize: 10.5 }}>
                    {t("skillLab.train.publish.noChange")}
                  </span>
                )}
              </div>
            </>
          )}
        </div>
      )}

      {!isLive(detail) && (
        <>
          <div style={{ marginTop: 14 }}>
            <div className="mono" style={{ fontSize: 11, letterSpacing: ".08em", marginBottom: 6 }}>
              {t("skillLab.eval.artifacts.title")}
            </div>
            <ArtifactBrowser jobId={detail.id} />
          </div>
          <div style={{ marginTop: 14 }}>
            <div className="mono" style={{ fontSize: 11, letterSpacing: ".08em", marginBottom: 6 }}>
              {t("skillLab.eval.log.title")}
            </div>
            <JobLogPane jobId={detail.id} live={false} />
          </div>
        </>
      )}
    </Panel>
  );

  const staleSelection = !creating && detailMissing;

  return (
    <>
      {!creating && (
        <Panel
          brk
          pad={false}
          title={t("skillLab.train.listTitle")}
          sub={t("skillLab.train.listSub")}
          end={
            <Btn
              primary
              data-testid="new-train-job-btn"
              onClick={() => setSearchParams({ view: "train", job: "new" })}
            >
              + {t("skillLab.train.new")}
            </Btn>
          }
          style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
        >
          <table data-testid="skill-lab-train-table">
            <thead>
              <tr>
                <th>{t("skillLab.eval.col.status")}</th>
                <th>{t("skillLab.eval.col.skill")}</th>
                <th>{t("skillLab.eval.col.taskset")}</th>
                <th>{t("skillLab.train.col.best")}</th>
                <th>{t("skillLab.eval.col.created")}</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((row) => (
                <tr
                  key={row.id}
                  data-testid={`train-job-row-${row.id}`}
                  onClick={() => select(row.id)}
                  style={{
                    cursor: "pointer",
                    background: jobParam === row.id ? "rgba(255,176,0,.045)" : undefined,
                  }}
                >
                  <td>{statusChip(row)}</td>
                  <td className="pri">{row.skill_source?.name ?? "—"}</td>
                  <td className="mono dim">{row.taskset_name}</td>
                  <td className="mono">
                    {row.status === "succeeded" && bestScores[row.id] !== undefined
                      ? bestScores[row.id].toFixed(3)
                      : "—"}
                  </td>
                  <td className="mono dim">{stamp(row.created_at)}</td>
                </tr>
              ))}
              {loading && (
                <tr>
                  <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                    {t("common.loading")}
                  </td>
                </tr>
              )}
              {!loading && rows.length === 0 && listError === null && (
                <tr>
                  <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                    {t("skillLab.train.empty")}
                  </td>
                </tr>
              )}
              {listError !== null && (
                <tr>
                  <td colSpan={5} className="dim mono" style={{ textAlign: "center" }}>
                    {listError}
                  </td>
                </tr>
              )}
            </tbody>
          </table>
          <Pager {...pagerProps} always />
        </Panel>
      )}

      {creating ? (
        <TrainWizard
          status={status}
          provisioned={status === null || (status.provisioned && status.venv_ready)}
          presetRecordId={recordParam}
          onCreated={(job) => {
            setRows((prev) => [job, ...prev]);
            setDetail(job);
            select(job.id);
          }}
          onCancel={() => select(null)}
        />
      ) : staleSelection ? (
        <Panel brk title={t("skillLab.eval.gone.title")} style={{ "--i": 1 } as CSSProperties}>
          <div className="empty" data-testid="train-job-gone">
            {t("skillLab.eval.gone.body")}
          </div>
        </Panel>
      ) : (
        detailPanel ??
        (jobParam !== null ? (
          <Panel brk style={{ "--i": 1 } as CSSProperties}>
            <div className="empty">{t("common.loading")}</div>
          </Panel>
        ) : null)
      )}

      {/* Hand-rolled rather than ConfirmDialog: publishing carries a choice
          (re-approve) the shared dialog has no room for. */}
      {publishOpen && detail !== null && (
        <div className="confirm-backdrop" onClick={() => setPublishOpen(false)}>
          <div
            className="confirm-box"
            role="alertdialog"
            aria-modal="true"
            aria-label={t("skillLab.train.publish.confirmTitle")}
            data-testid="train-publish-dialog"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="confirm-title">▲ {t("skillLab.train.publish.confirmTitle")}</div>
            <p className="confirm-body">
              {t("skillLab.train.publish.confirmBody", {
                name: record?.name ?? detail.skill_source?.name ?? "",
                version: record?.version ?? detail.skill_source?.version ?? "—",
              })}
            </p>
            <p className="confirm-body">{t("skillLab.train.publish.confirmDraft")}</p>
            <label
              className="mono"
              style={{ display: "flex", gap: 8, alignItems: "center", fontSize: 11 }}
            >
              <input
                type="checkbox"
                checked={reapprove}
                data-testid="train-publish-reapprove"
                onChange={(e) => setReapprove(e.target.checked)}
              />
              <span>{t("skillLab.train.publish.reapprove")}</span>
            </label>
            {record !== null && record.status !== "APPROVED" && reapprove && (
              <p className="confirm-body dim" data-testid="train-publish-reapprove-note">
                {t("skillLab.train.publish.reapproveNoop", { status: record.status })}
              </p>
            )}
            <div className="confirm-actions">
              <Btn onClick={() => setPublishOpen(false)}>{t("common.cancel")}</Btn>
              <Btn
                primary
                disabled={busy}
                data-testid="train-publish-confirm"
                onClick={() => void publish(detail)}
              >
                {t("skillLab.train.publish.action")}
              </Btn>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmCancel !== null}
        title={t("skillLab.train.confirmCancel.title")}
        body={t("skillLab.eval.confirmCancel.body")}
        confirmLabel={t("skillLab.eval.cancel")}
        onConfirm={() => {
          const job = confirmCancel;
          setConfirmCancel(null);
          if (job) void cancel(job);
        }}
        onCancel={() => setConfirmCancel(null)}
      />
      <ConfirmDialog
        open={confirmResume !== null}
        title={t("skillLab.train.confirmResume.title")}
        body={t("skillLab.train.confirmResume.body")}
        confirmLabel={t("skillLab.train.resume")}
        onConfirm={() => {
          const job = confirmResume;
          setConfirmResume(null);
          if (job) void resume(job);
        }}
        onCancel={() => setConfirmResume(null)}
      />
      <ConfirmDialog
        open={confirmDelete !== null}
        title={t("skillLab.train.confirmDelete.title")}
        body={t("skillLab.eval.confirmDelete.body", {
          name: confirmDelete?.skill_source?.name ?? confirmDelete?.id ?? "",
        })}
        confirmLabel={t("skillLab.eval.delete")}
        onConfirm={() => {
          const job = confirmDelete;
          setConfirmDelete(null);
          if (job) void remove(job);
        }}
        onCancel={() => setConfirmDelete(null)}
      />
    </>
  );
}
