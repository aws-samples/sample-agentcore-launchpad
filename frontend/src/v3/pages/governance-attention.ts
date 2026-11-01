import { type ExternalMcpTarget, type LambdaToolTarget, targetsNeedingAttention } from "../../lib/api/mcpReviews";
import type { Signal } from "../ui";

/** One row of Gateway governance's 需要处理 panel. */
export interface Attention {
  key: string;
  s: Signal;
  title: string;
  detail: string;
  to: string;
  act: string;
}

const TARGETS_URL = "/v3/governance?tab=targets";

/** The tool targets (Lambda and adopted external) whose schema waits for a reviewer,
 *  and the Lambda targets whose publish failed. */
export function targetAttentionOf(
  lambdas: LambdaToolTarget[],
  external: ExternalMcpTarget[],
  t: (k: string, o?: Record<string, unknown>) => string,
): Attention[] {
  const out: Attention[] = [];
  const { failed, review } = targetsNeedingAttention(lambdas, external);
  if (failed.length) {
    out.push({
      key: "targets:failed", s: "act", title: t("v3.governance.att.targetsFailed", { count: failed.length }),
      detail: failed.join("  ·  "), to: TARGETS_URL, act: t("v3.governance.att.inspect"),
    });
  }
  if (review.length) {
    out.push({
      key: "targets:review", s: "wait", title: t("v3.governance.att.targetsReview", { count: review.length }),
      detail: review.join("  ·  "), to: TARGETS_URL, act: t("v3.governance.att.review"),
    });
  }
  return out;
}
