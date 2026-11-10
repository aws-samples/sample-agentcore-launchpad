import { useTranslation } from "react-i18next";

import { type GateSource, gateLabel } from "../lib/evalPlanGate";

/**
 * The gate of an evaluation-plan evaluator or recommendation, rendered the same way in
 * both consoles (WB-006): a `blocking` entry reads "blocking · threshold N" (zh "阻断 ·
 * 阈值 N") and is never shown as "informational"; an informational entry with a display
 * threshold reads "informational · ≥ N". Helpers live in `lib/evalPlanGate.ts`.
 */
export function EvalGateBadge({
  blocking,
  threshold,
  metric,
  source,
  testId,
}: {
  blocking: boolean | null | undefined;
  threshold: number | null | undefined;
  metric?: string | null;
  source?: GateSource | null;
  testId?: string;
}) {
  const { t } = useTranslation();
  const isBlocking = blocking === true;
  const th = typeof threshold === "number" && Number.isFinite(threshold) ? threshold : null;
  return (
    <span
      className="mono"
      data-testid={testId ?? "eval-gate"}
      data-blocking={isBlocking ? "true" : "false"}
      style={isBlocking ? { fontWeight: 600 } : undefined}
    >
      {gateLabel(t, isBlocking, th)}
      {metric ? ` · ${t("assistantEval.gateMetric", { m: metric })}` : ""}
      {source ? (
        <span className="dim" style={{ fontWeight: 400, marginLeft: 6, fontSize: "0.9em" }} data-testid={`${testId ?? "eval-gate"}-source`}>
          {t(`assistantEval.gateSource.${source}`)}
        </span>
      ) : null}
    </span>
  );
}
