import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  ApiError,
  errorMessage,
  type V2FromSessionsJob,
  type V2FromSessionsOutcome,
  type V2Range,
  type V2SkippedSession,
} from "../../../lib/api";
import { fromSessionsJobView, shouldStopPolling } from "../../../lib/fromSessionsJob";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Field, Modal, Segmented, Select } from "../../ui";

/**
 * "加入数据集" — turn observed sessions (the trajectories behind the selected
 * traces) into dataset items: appended to an existing local dataset, or as a
 * new one. Shared by the trajectory list, the trajectory detail and insights.
 *
 * N-SCN-obs10 round-6: the route is asked for its 202 job shape (`?sync=false`) and the
 * modal polls `GET /api/jobs/{id}` — the span reads of a busy environment took 22 s, then
 * > 30 s, which a request with no progress cannot carry. The result (dataset id / name /
 * item count, added, skipped) comes off the job row.
 */
const JOB_POLL_MS = 1000;
export function AddToDatasetModal({
  open,
  sessionIds,
  range,
  onClose,
  onDone,
}: {
  open: boolean;
  sessionIds: string[];
  range: V2Range;
  onClose: () => void;
  onDone?: (result: V2FromSessionsOutcome) => void;
}) {
  if (!open) return null;
  return <AddToDatasetBody sessionIds={sessionIds} range={range} onClose={onClose} onDone={onDone} />;
}

function AddToDatasetBody({
  sessionIds,
  range,
  onClose,
  onDone,
}: {
  sessionIds: string[];
  range: V2Range;
  onClose: () => void;
  onDone?: (result: V2FromSessionsOutcome) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const datasets = useLoad(() => api.v2Datasets(), "datasets");
  const receivable = (datasets.data?.datasets ?? []).filter((d) => d.kind !== "simulated");
  const [mode, setMode] = useState<"existing" | "new">("new");
  const [datasetId, setDatasetId] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [firstTurnOnly, setFirstTurnOnly] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<V2FromSessionsOutcome | null>(null);
  const [refused, setRefused] = useState<V2SkippedSession[]>([]);
  const [job, setJob] = useState<V2FromSessionsJob | null>(null);
  const [jobId, setJobId] = useState<string | null>(null);
  const settled = useRef(false);

  const finish = (outcome: V2FromSessionsOutcome) => {
    setResult(outcome);
    toast("success", t("v2.addToDataset.done", { count: outcome.added, name: outcome.dataset.name }));
    onDone?.(outcome);
  };
  const fail = (message: string, skipped: V2SkippedSession[] | undefined) => {
    setError(message);
    // nothing usable: the backend says why per session
    setRefused((skipped ?? []).filter((s) => s.session_id));
  };

  // poll the job until it settles; the last answer carries the result or the error envelope
  useEffect(() => {
    if (!jobId) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let failures = 0; // consecutive failed reads; a good read resets it
    const tick = async () => {
      try {
        const current = await api.v2FromSessionsJob(jobId);
        if (cancelled) return;
        failures = 0;
        setJob(current);
        const view = fromSessionsJobView(current);
        if (!view.done) {
          timer = setTimeout(() => void tick(), JOB_POLL_MS);
          return;
        }
        if (settled.current) return;
        settled.current = true;
        setBusy(false);
        if (view.phase === "succeeded" && view.result) finish(view.result);
        else {
          const detail = current.payload.error?.detail as { skipped?: V2SkippedSession[] } | undefined;
          fail(view.error ?? t("v2.addToDataset.jobFailed"), detail?.skipped);
        }
      } catch (err) {
        if (cancelled) return;
        // one missed poll is not a failed job — keep polling, but not forever: a gone job
        // row stops at once, anything else after JOB_POLL_MAX_CONSECUTIVE_ERRORS in a row
        failures += 1;
        if (shouldStopPolling(failures, err)) {
          settled.current = true;
          setBusy(false);
          fail(errorMessage(err), undefined);
          return;
        }
        timer = setTimeout(() => void tick(), JOB_POLL_MS);
      }
    };
    void tick();
    return () => {
      cancelled = true;
      if (timer) clearTimeout(timer);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- the poller is keyed on the job id only
  }, [jobId]);

  const submit = async () => {
    if (mode === "existing" && !datasetId) return setError(t("v2.addToDataset.errPick"));
    if (mode === "new" && !name.trim()) return setError(t("v2.addToDataset.errName"));
    setBusy(true);
    setError(null);
    setRefused([]);
    try {
      const accepted = await api.v2DatasetFromSessionsJob({
        session_ids: sessionIds,
        range,
        first_turn_only: firstTurnOnly,
        ...(mode === "existing" ? { dataset_id: datasetId } : { name: name.trim(), description }),
      });
      settled.current = false;
      setJob(null);
      setJobId(accepted.job_id);
    } catch (err) {
      // refused before any job was filed (simulated target, unknown dataset, both targets…)
      setBusy(false);
      const detail = err instanceof ApiError ? (err.detail as { skipped?: V2SkippedSession[] } | null) : null;
      fail(errorMessage(err), detail?.skipped);
    }
  };

  const view = fromSessionsJobView(job);
  const polling = busy && jobId !== null;

  const skippedBySession = (result?.skipped ?? []).filter((s) => s.session_id);
  const duplicates = (result?.skipped ?? []).filter((s) => !s.session_id).length;

  return (
    <Modal
      open
      title={t("v2.addToDataset.title")}
      onClose={onClose}
      testId="v2-add-to-dataset"
      footer={
        result ? (
          <Button kind="primary" onClick={onClose}>
            {t("v2.common.done")}
          </Button>
        ) : (
          <>
            <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={busy} onClick={() => void submit()} testId="v2-add-to-dataset-ok">
              {t("v2.common.confirm")}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <div className="v2-stack">
          <Alert tone="success">
            {t("v2.addToDataset.result", { count: result.added, name: result.dataset.name, total: result.dataset.item_count })}
          </Alert>
          {duplicates > 0 && <Alert tone="warn">{t("v2.addToDataset.duplicates", { count: duplicates })}</Alert>}
          {skippedBySession.length > 0 && (
            <Alert tone="warn">
              {t("v2.addToDataset.skipped", { count: skippedBySession.length })}
              <ul style={{ margin: "4px 0 0 16px" }}>
                {skippedBySession.slice(0, 6).map((s) => (
                  <li key={s.session_id}>
                    <span className="mono">{s.session_id}</span> · {t(`v2.skipReason.${s.reason}`, { defaultValue: s.reason })}
                  </li>
                ))}
              </ul>
            </Alert>
          )}
        </div>
      ) : (
        <div className="v2-form">
          <Alert>{t("v2.addToDataset.hint", { count: sessionIds.length })}</Alert>
          {polling && (
            <div className="v2-stack" style={{ gap: 4 }} data-testid="v2-add-to-dataset-progress" role="status">
              <span className="v2-muted">
                {view.phase === "queued"
                  ? t("v2.addToDataset.queued")
                  : view.phase === "writing"
                    ? t("v2.addToDataset.writing")
                    : t("v2.addToDataset.reading", { read: view.read, total: view.total || sessionIds.length })}
              </span>
              <progress max={100} value={view.percent} style={{ width: "100%" }} />
            </div>
          )}
          {error && (
            <Alert tone="error">
              {error}
              {refused.length > 0 && (
                <ul style={{ margin: "4px 0 0 16px" }}>
                  {refused.slice(0, 6).map((s) => (
                    <li key={s.session_id}>
                      <span className="mono">{s.session_id}</span> · {t(`v2.skipReason.${s.reason}`, { defaultValue: s.reason })}
                    </li>
                  ))}
                </ul>
              )}
            </Alert>
          )}
          <Field label={t("v2.addToDataset.target")}>
            <Segmented
              value={mode}
              onChange={setMode}
              options={[
                { value: "new", label: t("v2.addToDataset.new") },
                { value: "existing", label: t("v2.addToDataset.existing") },
              ]}
            />
          </Field>
          {mode === "existing" ? (
            <Field label={t("v2.addToDataset.dataset")} required>
              <Select
                value={datasetId}
                placeholder={t("v2.common.choose")}
                options={receivable.map((d) => ({ value: d.id, label: `${d.name} · ${t("v2.datasets.items", { count: d.item_count })}` }))}
                onChange={setDatasetId}
              />
            </Field>
          ) : (
            <>
              <Field label={t("v2.datasets.colName")} required>
                <input className="v2-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} data-testid="v2-add-to-dataset-name" />
              </Field>
              <Field label={t("v2.datasets.description")}>
                <input className="v2-input" value={description} maxLength={1000} onChange={(e) => setDescription(e.target.value)} />
              </Field>
            </>
          )}
          <label className="v2-check">
            <input type="checkbox" checked={firstTurnOnly} onChange={(e) => setFirstTurnOnly(e.target.checked)} />
            {t("v2.addToDataset.firstTurnOnly")}
          </label>
        </div>
      )}
    </Modal>
  );
}
