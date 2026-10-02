import { useTranslation } from "react-i18next";

/**
 * 放量计划 for a Harness canary, chosen before setup: 90/10 is optional (untick →
 * the A/B test opens at 50/50), 50/50 always runs, and 1/99 is decided on the 50/50
 * verdict (complete straight away, or ramp on). Runtime canaries keep the full ramp
 * and never render this.
 */
export function RampPlan({
  runFirst,
  onChange,
  disabled,
}: {
  /** run the 90/10 stage first (the default) */
  runFirst: boolean;
  onChange: (runFirst: boolean) => void;
  disabled?: boolean;
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
      {!runFirst && <span className="v2-muted" style={{ fontSize: 12.5 }}>{t("canaryPage.plan.firstHint")}</span>}
      <span className="v2-muted">■ {t("canaryPage.plan.half")}</span>
      <span className="v2-muted">◇ {t("canaryPage.plan.last")}</span>
    </div>
  );
}
