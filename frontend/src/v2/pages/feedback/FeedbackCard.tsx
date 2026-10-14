import { DatabaseZap } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { feedbackApi, type FeedbackItem, type V2Range } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad } from "../../hooks";
import { Button, Card, Kpi, Table, Tag } from "../../ui";
import { AddToDatasetModal } from "../data/AddToDataset";

/**
 * Thumbs-down answers of the selected range (V2 Chat thumbs). A thumbs-down is a
 * bad case: its session goes into the existing "add to dataset" flow
 * (`POST /api/eval/datasets/from-sessions`), which reads the trace back and says
 * which sessions it skipped — no dataset-building logic lives here.
 */
export function FeedbackCard({ range }: { range: V2Range }) {
  const { t } = useTranslation();
  const feedback = useLoad(() => feedbackApi.list({ range, verdict: "down", limit: 50 }), `feedback:down:${range}`);
  const [adding, setAdding] = useState(false);
  const data = feedback.data;
  // the server already limits these to the range (and to one from-sessions call)
  const sessions = data?.down_session_ids ?? [];

  return (
    <Card
      title={t("feedbackCard.title")}
      sub={t("feedbackCard.desc")}
      testId="v2-feedback-card"
      end={
        <Button
          disabled={sessions.length === 0}
          title={t("feedbackCard.toDatasetHint")}
          onClick={() => setAdding(true)}
          testId="v2-feedback-to-dataset"
        >
          <DatabaseZap size={14} aria-hidden="true" />
          {t("feedbackCard.toDataset", { count: sessions.length })}
        </Button>
      }
    >
      <div className="v2-kpis">
        <Kpi label={t("feedbackCard.up")} value={data?.totals.up ?? "—"} tone="good" testId="v2-feedback-up" />
        <Kpi
          label={t("feedbackCard.down")}
          value={data?.totals.down ?? "—"}
          tone={data?.totals.down ? "bad" : undefined}
          testId="v2-feedback-down"
        />
      </div>
      <Table<FeedbackItem>
        rows={data?.items ?? []}
        rowKey={(r) => r.id}
        loading={feedback.loading}
        error={feedback.error}
        onRetry={feedback.reload}
        empty={t("feedbackCard.empty")}
        testId="v2-feedback-table"
        columns={[
          { key: "time", title: t("v2.insights.colTime"), className: "nowrap", render: (r) => fmtTime(r.updated_at) },
          { key: "agent", title: t("feedbackCard.colAgent"), render: (r) => r.agent_name },
          {
            key: "q",
            title: t("feedbackCard.colQuestion"),
            render: (r) => (
              <span className="clip" title={r.question}>
                {r.question || "—"}
              </span>
            ),
          },
          {
            key: "a",
            title: t("feedbackCard.colAnswer"),
            render: (r) => (
              <span className="clip" title={r.answer}>
                {r.answer || "—"}
              </span>
            ),
          },
          { key: "who", title: t("feedbackCard.colWho"), render: (r) => r.actor || "—" },
          {
            key: "src",
            title: t("feedbackCard.colSource"),
            // main writes only `console`; a ledger that once ran another console line
            // may hold e.g. `share` / `review` rows — an unknown source shows verbatim
            render: (r) => (
              <Tag tone="outline">{t(`feedbackCard.source.${r.source}`, { defaultValue: r.source || "—" })}</Tag>
            ),
          },
          {
            key: "open",
            title: t("v2.common.actions"),
            className: "right",
            render: (r) => (
              <Link to={`/v2/chat?agent=${encodeURIComponent(r.agent_id)}&session=${encodeURIComponent(r.session_id)}`}>
                {t("feedbackCard.openSession")}
              </Link>
            ),
          },
        ]}
      />
      <AddToDatasetModal open={adding} sessionIds={sessions} range={range} onClose={() => setAdding(false)} />
    </Card>
  );
}
