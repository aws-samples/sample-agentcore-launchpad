import { useTranslation } from "react-i18next";

import { Btn, Chip } from "../../components";
import type { TaskgenReviewDraft } from "./taskgenReview";

const str = (value: unknown): string => (typeof value === "string" ? value : "");

/**
 * Editable review list for generated tasks. Purely controlled: the parent owns
 * the drafts (so a job switch resets them and a poll or language change does
 * not) and decides when to send them.
 */
export function TaskgenReviewEditor({
  drafts,
  onChange,
  showAttachments,
  readOnly = false,
}: {
  drafts: TaskgenReviewDraft[];
  onChange: (next: TaskgenReviewDraft[]) => void;
  /** the job was given documents — show each row's declared ones */
  showAttachments: boolean;
  /** after a save: the selection is what was written, nothing may change */
  readOnly?: boolean;
}) {
  const { t } = useTranslation();
  const patch = (index: number, change: Partial<TaskgenReviewDraft>) =>
    onChange(drafts.map((draft) => (draft.index === index ? { ...draft, ...change } : draft)));

  return (
    <div data-testid="taskgen-task-table">
      {drafts.map((draft) => {
        const originalId = str(draft.original.id);
        const attachments = Array.isArray(draft.original.attachments)
          ? draft.original.attachments.map(String)
          : [];
        const disabled = readOnly || draft.excluded;
        return (
          <div
            key={draft.index}
            data-testid={`taskgen-review-row-${draft.index}`}
            data-excluded={draft.excluded ? "true" : "false"}
            style={{
              border: "1px solid rgba(255,255,255,.08)",
              borderRadius: 4,
              padding: "8px 10px",
              marginBottom: 6,
              opacity: draft.excluded ? 0.55 : 1,
            }}
          >
            <div style={{ display: "flex", gap: 6, alignItems: "center", marginBottom: 6 }}>
              <span className="mono dim" style={{ fontSize: 10, minWidth: 22 }}>
                #{draft.index + 1}
              </span>
              <input
                className="input mono"
                value={draft.id}
                disabled={disabled}
                aria-label={t("skillLab.tasksets.field.id")}
                data-testid={`taskgen-review-id-${draft.index}`}
                style={{ maxWidth: 200, fontSize: 11 }}
                onChange={(e) => patch(draft.index, { id: e.target.value })}
              />
              <input
                className="input mono"
                value={draft.taskType}
                disabled={disabled}
                aria-label={t("skillLab.tasksets.field.taskType")}
                placeholder={t("skillLab.tasksets.field.taskTypePlaceholder")}
                data-testid={`taskgen-review-type-${draft.index}`}
                style={{ maxWidth: 160, fontSize: 11 }}
                onChange={(e) => patch(draft.index, { taskType: e.target.value })}
              />
              {showAttachments && (
                <span
                  className="mono dim"
                  style={{ fontSize: 10 }}
                  data-testid={`taskgen-task-attachments-${originalId}`}
                >
                  {t("skillLab.taskgen.col.attachments")}:{" "}
                  {attachments.length ? attachments.join(" · ") : "—"}
                </span>
              )}
              {draft.excluded && (
                <Chip tone="muted" icon="✕">
                  {t("skillLab.taskgen.review.excludedTag")}
                </Chip>
              )}
              {!readOnly && (
                <Btn
                  style={{ marginLeft: "auto" }}
                  data-testid={`taskgen-review-exclude-${draft.index}`}
                  onClick={() => patch(draft.index, { excluded: !draft.excluded })}
                >
                  {draft.excluded
                    ? t("skillLab.taskgen.review.restore")
                    : t("skillLab.taskgen.review.exclude")}
                </Btn>
              )}
            </div>
            <div className="field">
              <label>{t("skillLab.tasksets.field.question")}</label>
              <textarea
                className="input"
                rows={2}
                value={draft.question}
                disabled={disabled}
                data-testid={`taskgen-review-question-${draft.index}`}
                style={{ resize: "vertical", fontSize: 11.5 }}
                onChange={(e) => patch(draft.index, { question: e.target.value })}
              />
            </div>
            <div className="field" style={{ marginBottom: 0 }}>
              <label>{t("skillLab.tasksets.field.rubric")}</label>
              <textarea
                className="input"
                rows={2}
                value={draft.rubric}
                disabled={disabled}
                data-testid={`taskgen-review-rubric-${draft.index}`}
                style={{ resize: "vertical", fontSize: 11.5 }}
                onChange={(e) => patch(draft.index, { rubric: e.target.value })}
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}
