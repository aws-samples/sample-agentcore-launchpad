import type { CSSProperties } from "react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { Btn, Chip, ConfirmDialog, Pager, Panel, useTablePage, useToast } from "../components";
import type { ChipTone } from "../components";
import type { SkillLabJobInfo, SkillLabJobResults, SkillLabStatus } from "../lib/api";
import { api, ApiError } from "../lib/api";
import { ArtifactBrowser } from "./skillLab/ArtifactBrowser";
import { EvalResults } from "./skillLab/EvalResults";
import { EvalWizard } from "./skillLab/EvalWizard";
import { JobLogPane } from "./skillLab/JobLogPane";

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

const isLive = (job: SkillLabJobInfo) => LIVE_STATUSES.includes(job.status);

const stamp = (value: string | null) => (value ? new Date(value).toLocaleString() : "—");

function elapsed(job: SkillLabJobInfo): string {
  if (!job.started_at) return "—";
  const end = job.finished_at ? new Date(job.finished_at) : new Date();
  const seconds = Math.max(0, (end.getTime() - new Date(job.started_at).getTime()) / 1000);
  return seconds < 90 ? `${seconds.toFixed(0)}s` : `${(seconds / 60).toFixed(1)}m`;
}

const tasksetLabel = (job: SkillLabJobInfo) =>
  job.split ? `${job.taskset_name} · ${job.split}` : job.taskset_name;

/**
 * Evaluation half of the Skill Lab (`?view=eval`): the job list, the submit
 * wizard (`job=new`, optionally deep-linked with `record=`) and the per-job
 * detail with a live log tail while running and judged results once done.
 *
 * The page shell owns the module head, the nav and the provisioning banner —
 * this renders panels only.
 */
export function SkillLabEval({ status }: { status: SkillLabStatus | null }) {
  const { t } = useTranslation();
  const toast = useToast();
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
  const [results, setResults] = useState<SkillLabJobResults | null>(null);
  const [resultsPending, setResultsPending] = useState(false);
  const [confirmCancel, setConfirmCancel] = useState<SkillLabJobInfo | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<SkillLabJobInfo | null>(null);
  const [busy, setBusy] = useState(false);
  // Pass rates are only known after reading a job's results, which the list
  // deliberately does not do (one file read per row); rates observed while
  // browsing are cached so the column fills in as the user clicks around.
  const [passRates, setPassRates] = useState<Record<string, number>>({});

  const load = useCallback(async () => {
    try {
      setRows(await api.skillLabJobs("eval"));
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
  // `liveRef` lets the interval read the current status without re-arming on
  // every progress-string change.
  const liveRef = useRef(false);
  liveRef.current = detail !== null && isLive(detail);

  useEffect(() => {
    setDetailMissing(false);
    if (!jobParam || jobParam === "new") {
      setDetail(null);
      setResults(null);
      return;
    }
    let stale = false;
    const fetchJob = async () => {
      try {
        const job = await api.skillLabJobGet(jobParam);
        if (stale) return;
        setDetail(job);
        // Keep the list row in step with the detail for free: a job that
        // finishes (or is cancelled) between list polls would otherwise sit at
        // RUNNING in the table for up to 8 s.
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

  // Results appear the moment the CLI writes results.json — which is before the
  // process exits — so a terminal status is the trigger, and a 404 is a normal
  // answer (cancelled or failed-before-scoring jobs never produce one).
  const detailId = detail?.id ?? null;
  const detailStatus = detail?.status ?? null;
  useEffect(() => {
    if (detailId === null || detailStatus === null || LIVE_STATUSES.includes(detailStatus)) {
      setResults(null);
      setResultsPending(false);
      return;
    }
    let stale = false;
    setResultsPending(true);
    api
      .skillLabJobResults(detailId)
      .then((data) => {
        if (stale) return;
        setResults(data);
        setPassRates((prev) => ({ ...prev, [detailId]: data.summary.pass_rate }));
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
  }, [detailId, detailStatus]);

  const select = (id: string | null) => {
    setSearchParams(id ? { view: "eval", job: id } : { view: "eval" });
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

  const detailPanel = detail && (
    <Panel
      brk
      title={detail.skill_source?.name ?? t("skillLab.eval.unknownSkill")}
      sub={`${detail.id} · ${tasksetLabel(detail)}`}
      end={
        <>
          {statusChip(detail)}
          {isLive(detail) && (
            <Btn disabled={busy} data-testid="eval-cancel" onClick={() => setConfirmCancel(detail)}>
              {t("skillLab.eval.cancel")}
            </Btn>
          )}
          {!isLive(detail) && (
            <Btn disabled={busy} data-testid="eval-delete" onClick={() => setConfirmDelete(detail)}>
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
        <span className="k mono">{t("skillLab.eval.field.models")}</span>
        <span className="v mono" style={{ fontSize: 10.5 }}>
          {t("skillLab.eval.field.target")} {detail.params.target_model} ·{" "}
          {t("skillLab.eval.field.judge")} {detail.params.judge_model}
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
            data-testid="eval-progress"
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
          data-testid="eval-job-error"
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

      {!isLive(detail) && (
        <div style={{ marginTop: 14 }}>
          {results !== null ? (
            <EvalResults results={results} />
          ) : (
            <div className="empty" data-testid="eval-no-results">
              {resultsPending ? t("common.loading") : t("skillLab.eval.noResults")}
            </div>
          )}
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
        </div>
      )}
    </Panel>
  );

  // Only a 404 from the job endpoint means "gone". Keying this on the list (or
  // on `detail === null`) would flash the panel every time a selection or a
  // freshly submitted job races the list poll.
  const staleSelection = !creating && detailMissing;

  return (
    <>
      {!creating && (
        <Panel
          brk
          pad={false}
          title={t("skillLab.eval.listTitle")}
          sub={t("skillLab.eval.listSub")}
          end={
            <Btn
              primary
              data-testid="new-eval-job-btn"
              onClick={() => setSearchParams({ view: "eval", job: "new" })}
            >
              + {t("skillLab.eval.new")}
            </Btn>
          }
          style={{ "--i": 0, marginBottom: 14 } as CSSProperties}
        >
          <table data-testid="skill-lab-eval-table">
            <thead>
              <tr>
                <th>{t("skillLab.eval.col.status")}</th>
                <th>{t("skillLab.eval.col.skill")}</th>
                <th>{t("skillLab.eval.col.taskset")}</th>
                <th>{t("skillLab.eval.col.passRate")}</th>
                <th>{t("skillLab.eval.col.created")}</th>
              </tr>
            </thead>
            <tbody>
              {pageRows.map((row) => {
                const rate = passRates[row.id];
                return (
                  <tr
                    key={row.id}
                    data-testid={`eval-job-row-${row.id}`}
                    onClick={() => select(row.id)}
                    style={{
                      cursor: "pointer",
                      background: jobParam === row.id ? "rgba(255,176,0,.045)" : undefined,
                    }}
                  >
                    <td>{statusChip(row)}</td>
                    <td className="pri">{row.skill_source?.name ?? "—"}</td>
                    <td className="mono dim">{tasksetLabel(row)}</td>
                    <td className="mono">
                      {row.status === "succeeded" && rate !== undefined
                        ? `${(rate * 100).toFixed(1)}%`
                        : "—"}
                    </td>
                    <td className="mono dim">{stamp(row.created_at)}</td>
                  </tr>
                );
              })}
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
                    {t("skillLab.eval.empty")}
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
        <EvalWizard
          status={status}
          provisioned={status === null || (status.provisioned && status.venv_ready)}
          presetRecordId={recordParam}
          onCreated={(job) => {
            // Seed both surfaces: the list poll is 8 s out, and the detail fetch
            // would otherwise leave the new job's panel blank for a moment.
            setRows((prev) => [job, ...prev]);
            setDetail(job);
            select(job.id);
          }}
          onCancel={() => select(null)}
        />
      ) : staleSelection ? (
        <Panel brk title={t("skillLab.eval.gone.title")} style={{ "--i": 1 } as CSSProperties}>
          <div className="empty" data-testid="eval-job-gone">
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

      <ConfirmDialog
        open={confirmCancel !== null}
        title={t("skillLab.eval.confirmCancel.title")}
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
        open={confirmDelete !== null}
        title={t("skillLab.eval.confirmDelete.title")}
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
