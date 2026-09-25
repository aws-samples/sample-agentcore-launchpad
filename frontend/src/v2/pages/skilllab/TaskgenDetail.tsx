import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  errorMessage,
  type SkillLabJobInfo,
  type SkillLabTaskgenResults,
  type SkillLabTasksetInfo,
} from "../../../lib/api";
import { elapsed, isLiveJob, jobSkillName } from "../../../lib/skillLab";
import {
  describeSaveError,
  reviewBlocker,
  reviewSelection,
  type TaskgenReviewDraft,
  toReviewDrafts,
} from "../../../lib/skillLabTaskgen";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, Field, FlowHeader, LinkButton, Spin, Tag } from "../../ui";
import { GoneCard, JobStatusTag } from "./common";
import { JobLog } from "./JobLog";
import { taskgenTarget, useJob } from "./state";

/** Editable review of generated rows: the four author fields + exclude. Purely controlled. */
function ReviewEditor({
  drafts,
  onChange,
  showAttachments,
  readOnly,
}: {
  drafts: TaskgenReviewDraft[];
  onChange: (next: TaskgenReviewDraft[]) => void;
  showAttachments: boolean;
  readOnly: boolean;
}) {
  const { t } = useTranslation();
  const patch = (index: number, change: Partial<TaskgenReviewDraft>) =>
    onChange(drafts.map((draft) => (draft.index === index ? { ...draft, ...change } : draft)));
  return (
    <div className="v2-stack v2-skilllab-review" data-testid="v2-taskgen-review">
      {drafts.map((draft) => {
        const attachments = Array.isArray(draft.original.attachments) ? draft.original.attachments.map(String) : [];
        const disabled = readOnly || draft.excluded;
        return (
          <div
            key={draft.index}
            className={draft.excluded ? "v2-skilllab-task excluded" : "v2-skilllab-task"}
            data-testid={`v2-taskgen-row-${draft.index}`}
            data-excluded={draft.excluded ? "true" : "false"}
          >
            <div className="v2-skilllab-task-head">
              <span className="v2-muted mono">#{draft.index + 1}</span>
              <input
                className="v2-input mono"
                value={draft.id}
                disabled={disabled}
                aria-label={t("skillLab.tasksets.field.id")}
                style={{ maxWidth: 220 }}
                onChange={(e) => patch(draft.index, { id: e.target.value })}
              />
              <input
                className="v2-input mono"
                value={draft.taskType}
                disabled={disabled}
                aria-label={t("skillLab.tasksets.field.taskType")}
                placeholder={t("skillLab.tasksets.field.taskTypePlaceholder")}
                style={{ maxWidth: 200 }}
                onChange={(e) => patch(draft.index, { taskType: e.target.value })}
              />
              {showAttachments && (
                <span className="v2-muted">
                  {t("skillLab.taskgen.col.attachments")}: <span className="mono">{attachments.length ? attachments.join(" · ") : "—"}</span>
                </span>
              )}
              {draft.excluded && <Tag tone="gray">{t("skillLab.taskgen.review.excludedTag")}</Tag>}
              {!readOnly && (
                <span style={{ marginLeft: "auto" }}>
                  <LinkButton
                    danger={!draft.excluded}
                    onClick={() => patch(draft.index, { excluded: !draft.excluded })}
                    testId={`v2-taskgen-exclude-${draft.index}`}
                  >
                    {draft.excluded ? t("skillLab.taskgen.review.restore") : t("skillLab.taskgen.review.exclude")}
                  </LinkButton>
                </span>
              )}
            </div>
            <div className="v2-form cols-2">
              <Field label={t("skillLab.tasksets.field.question")}>
                <textarea
                  className="v2-textarea"
                  rows={3}
                  value={draft.question}
                  disabled={disabled}
                  onChange={(e) => patch(draft.index, { question: e.target.value })}
                />
              </Field>
              <Field label={t("skillLab.tasksets.field.rubric")}>
                <textarea
                  className="v2-textarea"
                  rows={3}
                  value={draft.rubric}
                  disabled={disabled}
                  onChange={(e) => patch(draft.index, { rubric: e.target.value })}
                />
              </Field>
            </div>
          </div>
        );
      })}
    </div>
  );
}

export function TaskgenDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { job: detail, setJob, missing, error: jobError } = useJob(id);
  const [results, setResults] = useState<SkillLabTaskgenResults | null>(null);
  const [drafts, setDrafts] = useState<TaskgenReviewDraft[] | null>(null);
  const [importName, setImportName] = useState("");
  // the raw failure, localized at render time so a language switch re-labels it
  const [actionError, setActionError] = useState<unknown>(null);
  const [saving, setSaving] = useState(false);
  // Synchronous double-click guard; `alive` drops an outcome that resolves
  // after the operator left this job.
  const savingRef = useRef(false);
  const alive = useRef(true);
  const [confirmCancel, setConfirmCancel] = useState(false);
  const back = () => setParams({ tab: "taskgen" });

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  // Results appear once the job succeeded; drafts are derived exactly once per
  // fetched result set, so the job poll and a language change keep typed edits.
  const succeeded = detail?.status === "succeeded";
  useEffect(() => {
    if (!succeeded) return;
    let stale = false;
    api
      .skillLabTaskgenResults(id)
      .then((r) => {
        if (stale) return;
        setResults(r);
        setDrafts(toReviewDrafts(r.tasks));
      })
      .catch(() => undefined);
    return () => {
      stale = true;
    };
  }, [id, succeeded]);

  if (missing) return <GoneCard title={t("skillLab.eval.gone.title")} body={t("skillLab.eval.gone.body")} onBack={back} />;
  if (!detail) return jobError ? <Alert tone="error">{jobError}</Alert> : <Spin />;

  const live = isLiveJob(detail);
  const imported = detail.params.imported_taskset_id;
  const expanded = detail.params.expanded === true;
  const isExpansion = detail.taskset_id !== "";
  // After a save the review is a read-only view of the generator's ORIGINAL
  // output — not of the selection that was written; that lives in the task set.
  const saved = Boolean(imported || expanded);
  const savedTasksetId = imported ?? detail.taskset_id;
  const selection = drafts === null ? null : reviewSelection(drafts);
  const keptCount = selection?.kept.length ?? 0;
  const excludedCount = drafts === null ? 0 : drafts.length - keptCount;
  const blocker = drafts === null ? null : reviewBlocker(drafts, t);

  // What the run was given vs what its tasks actually asked for.
  const attachedNames =
    detail.params.attachment_names ??
    (Array.isArray(results?.summary?.attachments) ? (results.summary.attachments as unknown[]).map(String) : []);
  const declared = new Set(
    (results?.tasks ?? []).flatMap((task) => (Array.isArray(task.attachments) ? task.attachments.map(String) : [])),
  );
  const unusedAttachments = attachedNames.filter((name) => !declared.has(name));

  const runSave = async (
    request: (job: SkillLabJobInfo, current: TaskgenReviewDraft[]) => Promise<{ job: SkillLabJobInfo; taskset: SkillLabTasksetInfo }>,
  ) => {
    if (drafts === null || savingRef.current) return;
    savingRef.current = true;
    setSaving(true);
    setActionError(null);
    try {
      const outcome = await request(detail, drafts);
      if (!alive.current) return; // operator moved on; the server write stands
      setJob(outcome.job);
      toast(
        "success",
        isExpansion ? t("skillLab.taskgen.review.applied", { name: outcome.taskset.name }) : t("skillLab.taskgen.review.imported"),
      );
      setParams({ tab: "tasksets", view: "detail", id: outcome.taskset.id });
    } catch (err) {
      if (alive.current) setActionError(err);
    } finally {
      savingRef.current = false;
      if (alive.current) setSaving(false);
    }
  };

  const importAsNew = () =>
    runSave((job, current) => api.skillLabTaskgenImport(job.id, importName.trim(), reviewSelection(current).tasks));
  const applyExpansion = () =>
    runSave((job, current) => api.skillLabTaskgenApply(job.id, reviewSelection(current).tasks));

  const cancel = async () => {
    try {
      setJob(await api.skillLabJobCancel(detail.id));
      toast("success", t("skillLab.eval.cancelRequested"));
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setConfirmCancel(false);
    }
  };

  const openSaved = () => setParams({ tab: "tasksets", view: "detail", id: savedTasksetId });

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {t("skillLab.taskgen.job.title", { id: detail.id })}
            <JobStatusTag job={detail} />
          </span>
        }
        onBack={back}
        end={
          live && (
            <Button kind="danger" onClick={() => setConfirmCancel(true)} testId="v2-taskgen-cancel">
              {t("skillLab.taskgen.job.cancel")}
            </Button>
          )
        }
      />
      {detail.error && <Alert tone="error">{detail.error}</Alert>}
      <Card title={t("v2.skillLab.overview")} sub={isExpansion ? t("skillLab.taskgen.job.expandSub", { name: detail.taskset_name, split: detail.split }) : t("skillLab.taskgen.job.newSub")}>
        <Descriptions
          items={[
            { label: t("skillLab.taskgen.col.skills"), value: jobSkillName(detail) || "—" },
            { label: t("skillLab.taskgen.col.target"), value: taskgenTarget(detail, t("skillLab.taskgen.target.new")) },
            { label: t("skillLab.backend.field"), value: <span className="mono">{detail.params.target_backend ?? "claude_code_exec"}</span> },
            { label: t("skillLab.eval.wizard.field.targetModel"), value: <span className="mono">{detail.params.model ?? "—"}</span> },
            { label: t("skillLab.taskgen.field.count"), value: detail.params.count ?? "—" },
            { label: t("skillLab.taskgen.field.timeout"), value: `${detail.params.timeout}s` },
            { label: t("skillLab.taskgen.field.attachments"), value: attachedNames.length ? <span className="mono">{attachedNames.join(" · ")}</span> : "—" },
            { label: t("v2.skillLab.progress"), value: detail.progress || detail.status },
            { label: t("skillLab.eval.col.created"), value: fmtTime(detail.created_at) },
            { label: t("skillLab.eval.field.elapsed"), value: elapsed(detail) },
            ...(detail.params.guidance
              ? [{ label: t("skillLab.taskgen.field.guidance"), value: <span className="v2-skilllab-clamp">{detail.params.guidance}</span> }]
              : []),
          ]}
        />
      </Card>

      {results !== null && drafts !== null && (
        <Card
          title={saved ? t("skillLab.taskgen.review.originalTitle", { n: results.count }) : t("skillLab.taskgen.review.title", { n: results.count })}
          sub={saved ? t("skillLab.taskgen.review.originalNote") : t("skillLab.taskgen.review.editHint")}
          testId="v2-taskgen-results"
        >
          {unusedAttachments.length > 0 && (
            <Alert>{t("skillLab.taskgen.review.unusedAttachments", { names: unusedAttachments.join(", ") })}</Alert>
          )}
          {saved && (
            <Alert
              tone="success"
              action={
                <LinkButton onClick={openSaved} testId="v2-taskgen-open-saved">
                  {expanded ? t("skillLab.taskgen.review.openSaved", { name: detail.taskset_name }) : savedTasksetId}
                </LinkButton>
              }
            >
              {expanded ? t("skillLab.taskgen.review.applied", { name: detail.taskset_name }) : t("skillLab.taskgen.review.imported")}
            </Alert>
          )}
          <ReviewEditor drafts={drafts} onChange={setDrafts} showAttachments={attachedNames.length > 0} readOnly={saved || saving} />
          {!saved && (
            <div className="v2-skilllab-savebar">
              <span className="v2-muted" data-testid="v2-taskgen-summary">
                {t("skillLab.taskgen.review.summary", { kept: keptCount, total: drafts.length })}
                {excludedCount > 0 && ` · ${t("skillLab.taskgen.review.excludedCount", { n: excludedCount })}`}
              </span>
              <Button size="sm" disabled={!selection?.dirty || saving} onClick={() => setDrafts(toReviewDrafts(results.tasks))}>
                {t("skillLab.taskgen.review.reset")}
              </Button>
              <div className="end">
                {isExpansion ? (
                  <Button
                    kind="primary"
                    disabled={blocker !== null || saving}
                    title={blocker ?? undefined}
                    onClick={() => void applyExpansion()}
                    testId="v2-taskgen-apply"
                  >
                    {saving ? t("skillLab.taskgen.review.saving") : t("skillLab.taskgen.review.apply", { split: detail.split })}
                  </Button>
                ) : (
                  <>
                    <input
                      className="v2-input"
                      style={{ width: 280 }}
                      value={importName}
                      disabled={saving}
                      placeholder={t("skillLab.taskgen.review.name")}
                      aria-label={t("skillLab.taskgen.review.name")}
                      onChange={(e) => setImportName(e.target.value)}
                      data-testid="v2-taskgen-import-name"
                    />
                    <Button
                      kind="primary"
                      disabled={!importName.trim() || blocker !== null || saving}
                      title={blocker ?? (!importName.trim() ? t("skillLab.taskgen.review.nameRequired") : undefined)}
                      onClick={() => void importAsNew()}
                      testId="v2-taskgen-import"
                    >
                      {saving ? t("skillLab.taskgen.review.saving") : t("skillLab.taskgen.review.import")}
                    </Button>
                  </>
                )}
              </div>
            </div>
          )}
          {!saved && blocker !== null && <div className="v2-skilllab-err">{blocker}</div>}
          {actionError !== null && (
            <Alert tone="error">
              <span style={{ whiteSpace: "pre-wrap" }}>{describeSaveError(actionError, t)}</span>
            </Alert>
          )}
        </Card>
      )}

      <Card title={t("skillLab.eval.log.title")}>
        <JobLog jobId={detail.id} live={live} testId="v2-taskgen-log" />
      </Card>
      <Confirm
        open={confirmCancel}
        title={t("v2.skillLab.confirmCancelGen")}
        body={t("skillLab.eval.confirmCancel.body")}
        confirmLabel={t("skillLab.taskgen.job.cancel")}
        danger
        onConfirm={() => void cancel()}
        onClose={() => setConfirmCancel(false)}
      />
    </>
  );
}
