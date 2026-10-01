import { useTranslation } from "react-i18next";

import { api } from "../../../lib/api";
import type { EvaluationRunInfo, RunRecommendation } from "../../../lib/evaluation";
import { fmtTime } from "../../format";
import { useLoad } from "../../hooks";
import { LinkButton, Tag } from "../../ui";
import { shortId } from "./common";

const STATUS_TONE: Record<string, "green" | "red" | "blue" | "gray"> = {
  COMPLETED: "green",
  FAILED: "red",
  PENDING: "blue",
  IN_PROGRESS: "blue",
};

/**
 * Recommendations generated from the OTHER completed runs of this agent + dataset.
 * Step 3 seeds from one source run at a time; without this list an accepted
 * recommendation vanishes from the next steps as soon as a newer run completes,
 * and nothing shows where the current Harness version came from.
 */
export function RecommendationHistory({
  runs,
  onOpen,
}: {
  /** completed runs other than the current source, newest first */
  runs: EvaluationRunInfo[];
  /** switch step 3's source run to this one */
  onOpen: (runId: string) => void;
}) {
  const { t } = useTranslation();
  const ids = runs.map((r) => r.id);
  const load = useLoad(
    async () => {
      const lists = await Promise.all(ids.map((id) => api.runRecommendations(id)));
      return lists.flatMap((l) => l.recommendations);
    },
    `rec-history:${ids.join(",")}`,
  );
  const recs: RunRecommendation[] = load.data ?? [];
  if (!recs.length) return null;

  return (
    <div className="v2-assistant-sub-rows" data-testid="v2-assistant-next-rec-history" style={{ marginTop: 8 }}>
      <div className="v2-muted" style={{ fontSize: 12.5 }}>{t("assistantNext.recommend.historyTitle", { n: recs.length })}</div>
      {recs.map((rec) => (
        <div key={rec.id} className="v2-assistant-sub-row" data-testid="v2-assistant-next-rec-history-row">
          <span className="mono">{t("assistantNext.run.runLine", { id: shortId(rec.run_id) })}</span>
          <span>{rec.kind === "system_prompt" ? t("expPage.recTypePrompt") : t("expPage.recTypeTools")}</span>
          <Tag tone={STATUS_TONE[rec.status] ?? "gray"}>{rec.status}</Tag>
          {rec.accepted && (
            <Tag tone="green" dot>
              {t("assistantNext.recommend.historyAccepted", { v: rec.accepted.previous_version ?? "—" })}
            </Tag>
          )}
          {rec.created_at && <span className="v2-muted">{fmtTime(rec.created_at)}</span>}
          <LinkButton onClick={() => onOpen(rec.run_id)}>{t("assistantNext.recommend.historyOpen")} ›</LinkButton>
        </div>
      ))}
    </div>
  );
}
