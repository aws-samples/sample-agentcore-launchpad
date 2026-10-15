// The level + personal-amount fields of one account's budget, with the limit they
// yield — shared by the Budgets tab's assign dialog and the user detail page's card.
import { useTranslation } from "react-i18next";

import type { BudgetLevelInfo, ConsoleRole } from "../../../lib/api";
import { fmtUsd } from "../../../lib/budgets";
import { Field, LinkButton, Select } from "../../ui";
import { useBasisLabel } from "../usage/labels";
import { type AssignDraft, amountFromInput, previewLimit } from "./budgets";

export function AssignFields({
  role,
  draft,
  onChange,
  levels,
  testIdPrefix = "v2-budget-assign",
}: {
  role: ConsoleRole;
  draft: AssignDraft;
  onChange: (draft: AssignDraft) => void;
  levels: BudgetLevelInfo[];
  testIdPrefix?: string;
}) {
  const { t } = useTranslation();
  const basisLabel = useBasisLabel();
  const overrideBad = !!draft.override.trim() && amountFromInput(draft.override) === null;
  const preview = previewLimit(role, draft, levels);
  const fallback = levels.find((l) => l.is_default) ?? null;

  return (
    <div className="v2-form">
      <Field
        label={t("v2.budgets.level")}
        hint={
          role === "admin"
            ? t("v2.budgets.levelHintAdmin")
            : fallback
              ? t("v2.budgets.levelHintDefault", { level: fallback.name })
              : t("v2.budgets.levelHintNone")
        }
      >
        <Select
          value={draft.levelId}
          placeholder={t("v2.budgets.noLevel")}
          options={levels.map((l) => ({
            value: l.id,
            label: `${l.name} · ${fmtUsd(l.monthly_budget_usd)}${l.is_default ? ` · ${t("v2.budgets.default")}` : ""}`,
          }))}
          onChange={(levelId) => onChange({ ...draft, levelId })}
          ariaLabel={t("v2.budgets.level")}
          testId={`${testIdPrefix}-level`}
        />
      </Field>
      <Field
        label={t("v2.budgets.override")}
        hint={t("v2.budgets.overrideHint")}
        error={overrideBad ? t("v2.budgets.amountInvalid") : null}
      >
        <span className="v2-row" style={{ gap: 8 }}>
          <input
            className="v2-input"
            inputMode="decimal"
            placeholder={t("v2.budgets.overridePlaceholder")}
            value={draft.override}
            onChange={(e) => onChange({ ...draft, override: e.target.value })}
            aria-label={t("v2.budgets.override")}
            data-testid={`${testIdPrefix}-override`}
          />
          {draft.override && (
            <LinkButton onClick={() => onChange({ ...draft, override: "" })} testId={`${testIdPrefix}-override-clear`}>
              {t("v2.budgets.clear")}
            </LinkButton>
          )}
        </span>
      </Field>
      <Field label={t("v2.budgets.effective")}>
        <span data-testid={`${testIdPrefix}-preview`}>
          <b>{preview.limit === null ? t("v2.usage.unlimited") : fmtUsd(preview.limit)}</b>
          <span className="v2-muted"> · {basisLabel(preview.basis, preview.levelName)}</span>
        </span>
      </Field>
    </div>
  );
}
