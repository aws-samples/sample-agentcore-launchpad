import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import {
  changeMark,
  type McpHint,
  type McpReviewDetail,
  type McpReviewList,
  toolReviewApi,
} from "../../../lib/api/mcpReviews";
import { fmtAgo, fmtTime } from "../../format";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Spin, Tag } from "../../ui";
import { GATE_TONE } from "./common";
import { JsonBlock } from "./widgets";

/**
 * The tool review of one Lambda or adopted external target (projects §15.12, §15.15),
 * the V2 twin of V3's `McpReviewSection`: the open (else the latest) review's tool list
 * against the last approved one, with hints, and approve / reject. Deciding needs
 * `mcp.approve`, and the creator can do neither (the server answers 409, shown as a toast).
 */
export function ToolReview({ toolId, onChanged }: { toolId: string; onChanged: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const reviews = useMemo(() => toolReviewApi(toolId), [toolId]);
  const [nonce, setNonce] = useState(0);
  const [busy, setBusy] = useState(false);
  const [note, setNote] = useState("");
  const list = useLoad(() => reviews.list(), `v2-tool-reviews:${toolId}:${nonce}`);
  const shownId = list.data?.open_review_id ?? list.data?.reviews[0]?.id ?? null;
  const detail = useLoad<McpReviewDetail | null>(
    () => (shownId ? reviews.get(shownId) : Promise.resolve(null)),
    `v2-tool-review:${toolId}:${shownId ?? "-"}:${nonce}`,
  );

  const decide = async (approve: boolean) => {
    const openId = list.data?.open_review_id;
    if (!openId) return;
    setBusy(true);
    try {
      await (approve ? reviews.approve : reviews.reject)(openId, note.trim());
      toast("success", t(approve ? "mcpReview.approved" : "mcpReview.rejected"));
      setNote("");
      setNonce((n) => n + 1);
      onChanged();
    } catch (err) {
      toast("error", `${t("mcpReview.decideFailed")}: ${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  };

  if (list.error) return <Alert tone="error">{list.error}</Alert>;
  if (!list.data) return <Spin />;
  return (
    <ToolReviewView
      list={list.data}
      review={detail.data}
      reviewError={detail.error}
      busy={busy}
      note={note}
      onNote={setNote}
      onDecide={decide}
    />
  );
}

/** The review's markup from loaded data (split out so it renders without fetching). */
export function ToolReviewView({
  list,
  review,
  reviewError,
  busy,
  note,
  onNote,
  onDecide,
}: {
  list: McpReviewList;
  review: McpReviewDetail | null;
  reviewError: string | null;
  busy: boolean;
  note: string;
  onNote: (value: string) => void;
  onDecide: (approve: boolean) => void;
}) {
  const { t } = useTranslation();
  const state = list.state;
  const openId = list.open_review_id;
  const hintsByTool = new Map<string, McpHint[]>();
  for (const h of review?.hints ?? []) hintsByTool.set(h.tool, [...(hintsByTool.get(h.tool) ?? []), h]);
  const before = new Map((review?.base_tools ?? []).map((tool) => [tool.name, tool]));

  return (
    <div className="v2-tool-review" data-testid="v2-tool-review">
      <div className="v2-row">
        <strong>{t("mcpReview.title")}</strong>
        <Tag tone={GATE_TONE[state]} dot>{t(`mcpReview.state.${state}`)}</Tag>
      </div>
      {state === "reviewing" && <Alert tone="warn">{t("mcpReview.withheld")}</Alert>}
      {state === "rejected" && <Alert tone="error">{t("mcpReview.rejectedNotice")}</Alert>}
      {state === "none" && <Alert>{t("mcpReview.none")}</Alert>}
      {review && !openId && review.publish_status === "publishing" && <Alert tone="warn">{t("mcpReview.publishing")}</Alert>}
      {review && !openId && review.publish_status === "failed" && (
        <Alert tone="error">{t("mcpReview.publishFailed")} {review.publish_error}</Alert>
      )}
      {reviewError && <Alert tone="error">{reviewError}</Alert>}
      {review && (
        <>
          <div className="v2-muted">
            {t("mcpReview.meta", {
              version: review.version ?? "—",
              source: t(`mcpReview.source.${review.source}`),
              by: review.requested_by || "—",
              ago: fmtAgo(review.created_at),
            })}
            {review.decided_by && ` · ${t("mcpReview.decidedBy", { by: review.decided_by, status: t(`mcpReview.state.${review.status}`) })}`}
          </div>
          <div className="v2-muted">
            {t("mcpReview.diff", {
              added: review.diff.added?.length ?? 0,
              removed: review.diff.removed?.length ?? 0,
              described: review.diff.description_changed?.length ?? 0,
              schema: review.diff.schema_changed?.length ?? 0,
              hints: review.hints.length,
            })}
          </div>
          <div className="v2-table-wrap">
            <table className="v2-table dense">
              <thead>
                <tr>
                  <th style={{ width: 24 }} />
                  <th>{t("mcpReview.tool")}</th>
                  <th>{t("mcpReview.description")}</th>
                  <th>{t("mcpReview.hints")}</th>
                </tr>
              </thead>
              <tbody>
                {review.tools.map((tool) => {
                  const old = before.get(tool.name);
                  return (
                    <tr key={tool.name}>
                      <td className="mono">{changeMark(review.diff, tool.name)}</td>
                      <td className="mono">{tool.name}</td>
                      <td style={{ whiteSpace: "pre-wrap" }}>
                        {tool.description || "—"}
                        {old && review.diff.description_changed?.includes(tool.name) && (
                          <div className="v2-muted" style={{ textDecoration: "line-through" }}>{old.description || "—"}</div>
                        )}
                        {review.diff.schema_changed?.includes(tool.name) && <div className="v2-muted">{t("mcpReview.schemaChanged")}</div>}
                        <details>
                          <summary className="v2-muted">{t("mcpReview.schema")}</summary>
                          <JsonBlock value={tool.input_schema} />
                        </details>
                      </td>
                      <td>
                        {(hintsByTool.get(tool.name) ?? []).map((h, i) => (
                          <div key={i}>
                            <Tag tone="orange" title={h.detail || undefined}>{t(`mcpReview.hint.${h.kind}`)}</Tag>
                          </div>
                        ))}
                      </td>
                    </tr>
                  );
                })}
                {(review.diff.removed ?? []).map((name) => (
                  <tr key={`removed-${name}`}>
                    <td className="mono">−</td>
                    <td className="mono" style={{ textDecoration: "line-through" }}>{name}</td>
                    <td className="v2-muted">{t("mcpReview.removed")}</td>
                    <td />
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          {openId === review.id && (
            <div className="v2-tool-review-decide">
              <label className="v2-muted" htmlFor={`v2-tool-review-note-${review.id}`}>{t("mcpReview.note")}</label>
              <textarea
                id={`v2-tool-review-note-${review.id}`}
                className="v2-textarea"
                rows={2}
                maxLength={2000}
                value={note}
                onChange={(e) => onNote(e.target.value)}
              />
              <div className="v2-row">
                <Button kind="danger" size="sm" disabled={busy} onClick={() => onDecide(false)} testId="v2-tool-review-reject">
                  {t("mcpReview.reject")}
                </Button>
                <Button kind="primary" size="sm" disabled={busy} onClick={() => onDecide(true)} testId="v2-tool-review-approve">
                  {t("mcpReview.approve")}
                </Button>
                <span className="v2-muted">{t("mcpReview.sod")}</span>
              </div>
            </div>
          )}
        </>
      )}
      {list.reviews.length > 1 && (
        <details>
          <summary className="v2-muted">{t("mcpReview.history", { count: list.reviews.length })}</summary>
          <div className="v2-table-wrap">
            <table className="v2-table dense">
              <tbody>
                {list.reviews.map((r) => (
                  <tr key={r.id}>
                    <td><Tag tone={GATE_TONE[r.status]}>{t(`mcpReview.state.${r.status}`)}</Tag></td>
                    <td>{t(`mcpReview.source.${r.source}`)}</td>
                    <td className="mono">{r.tool_count}</td>
                    <td>{r.requested_by || "—"}</td>
                    <td>{r.decided_by ?? "—"}</td>
                    <td className="v2-muted">{fmtTime(r.created_at)}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </details>
      )}
    </div>
  );
}
