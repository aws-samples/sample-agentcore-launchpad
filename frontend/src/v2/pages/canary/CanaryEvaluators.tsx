import { useTranslation } from "react-i18next";

import { type EvaluatorRow } from "../../../lib/api";
import { ONLINE_EVAL_MAX } from "../../../lib/experiments";
import { EvaluatorPicker } from "../../EvaluatorPicker";
import { Alert, Field, Select } from "../../ui";
import { type CanaryEvaluatorChoice, evaluatorName } from "./evaluatorChoice";

/** Picker + primary select. Live traffic carries no ground truth, so evaluators that
 *  need it are blocked; a categorical custom judge is refused by the backend at create
 *  (the account listing carries no rating scale to screen it here). */
export function CanaryEvaluatorFields({ choice, disabled }: { choice: CanaryEvaluatorChoice; disabled?: boolean }) {
  const { t } = useTranslation();
  const blocked = (e: EvaluatorRow) =>
    e.requires_ground_truth || e.id.startsWith("Builtin.Trajectory") ? t("v2.online.needsGt") : null;
  return (
    <div className="v2-form" data-testid="v2-canary-evaluators">
      <Alert>{t("v2.canary.eval.hint", { max: ONLINE_EVAL_MAX })}</Alert>
      <EvaluatorPicker
        evaluators={choice.rows}
        loading={choice.loading}
        error={choice.error}
        onRetry={choice.reload}
        selected={choice.selected}
        onChange={(next) => !disabled && choice.setSelected(next)}
        max={ONLINE_EVAL_MAX}
        blockedReason={blocked}
        testIdPrefix="v2-canary-eval"
      />
      {choice.selected.length === 0 && <Alert tone="warn">{t("v2.canary.eval.none")}</Alert>}
      <Field label={t("v2.canary.eval.primary")} hint={t("v2.canary.eval.primaryHint")}>
        <Select
          value={choice.primary}
          onChange={choice.setPrimary}
          disabled={disabled || choice.selected.length === 0}
          testId="v2-canary-primary"
          options={choice.selected.map((id) => ({ value: id, label: evaluatorName(t, choice.rows, id) }))}
        />
      </Field>
    </div>
  );
}
