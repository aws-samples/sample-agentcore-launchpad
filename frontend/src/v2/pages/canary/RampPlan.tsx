import { useTranslation } from "react-i18next";

import { Alert } from "../../ui";

/**
 * 放量计划, chosen before setup (both canary kinds): 90/10 is optional (untick → the
 * A/B test opens at 50/50), 50/50 always runs, and 1/99 is decided on the 50/50
 * verdict (complete straight away, or ramp on). The unticked hint differs by kind: a
 * Harness treatment is already production DEFAULT, while a Runtime canary's live
 * platform traffic is still split by the gateway, so 50/50 sends half of it to the
 * never-served candidate.
 */
export function RampPlan({
  runFirst,
  onChange,
  disabled,
  kind = "harness",
}: {
  /** run the 90/10 stage first (the default) */
  runFirst: boolean;
  onChange: (runFirst: boolean) => void;
  disabled?: boolean;
  kind?: "harness" | "runtime";
}) {
  const { t } = useTranslation();
  return (
    <div className="v2-stack" data-testid="v2-canary-ramp-plan">
      <div style={{ fontWeight: 600 }}>{t("canaryPage.plan.title")}</div>
      <label className="v2-check">
        <input
          type="checkbox"
          checked={runFirst}
          disabled={disabled}
          onChange={(e) => onChange(e.target.checked)}
          data-testid="v2-canary-plan-first"
        />
        {t("canaryPage.plan.first")}
      </label>
      {!runFirst &&
        (kind === "runtime" ? (
          // real platform traffic, not just test traffic, reaches the candidate
          <div data-testid="v2-canary-plan-runtime-warning">
            <Alert tone="warn">{t("canaryPage.plan.firstHintRuntime")}</Alert>
          </div>
        ) : (
          <span className="v2-muted" style={{ fontSize: 12.5 }}>{t("canaryPage.plan.firstHint")}</span>
        ))}
      <span className="v2-muted">■ {t("canaryPage.plan.half")}</span>
      <span className="v2-muted">◇ {t("canaryPage.plan.last")}</span>
    </div>
  );
}
