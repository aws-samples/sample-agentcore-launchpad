// One account's spend budget on its detail page: level, personal amount, the limit they
// yield and this month's spend (`PUT /api/budgets/users/{id}`, hub-global).
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, type ConsoleUser, errorMessage } from "../../../lib/api";
import { fmtUsd } from "../../../lib/budgets";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, LinkButton, Spin } from "../../ui";
import { AssignFields } from "../costs/BudgetAssign";
import { type AssignDraft, assignDraftFrom, assignDraftValid, assignmentPayload } from "../costs/budgets";
import { useBasisLabel } from "../usage/labels";
import { UsageBar } from "../usage/UsageParts";

export function BudgetCard({ user, onSaved }: { user: ConsoleUser; onSaved: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const basisLabel = useBasisLabel();
  const [nonce, setNonce] = useState(0);
  const levels = useLoad(() => api.budgetLevels(), `v2-user-budget-levels:${nonce}`);
  const people = useLoad(() => api.budgetPeople(), `v2-user-budget-people:${nonce}`);
  const initial = assignDraftFrom(user);
  const [draft, setDraft] = useState<AssignDraft>(initial);
  const [busy, setBusy] = useState(false);
  const person = people.data?.people.find((p) => p.user_id === user.id) ?? null;
  const changed = draft.levelId !== initial.levelId || draft.override.trim() !== initial.override;

  const save = async () => {
    if (busy || !assignDraftValid(draft)) return;
    setBusy(true);
    try {
      const saved = await api.assignBudget(user.id, assignmentPayload(draft));
      toast("success", t("v2.budgets.assigned", { name: user.username }));
      setDraft(assignDraftFrom(saved));
      setNonce((n) => n + 1);
      onSaved();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Card
      title={t("v2.budgets.userCardTitle")}
      sub={t("v2.budgets.userCardSub")}
      end={
        <Link className="v2-link" to="/v2/costs?view=budgets">
          {t("v2.budgets.openBudgets")}
        </Link>
      }
      testId="v2-users-budget"
    >
      {levels.error ? (
        <Alert tone="error" action={<LinkButton onClick={levels.reload}>{t("v2.common.retry")}</LinkButton>}>
          {levels.error}
        </Alert>
      ) : !levels.data ? (
        <Spin />
      ) : (
        <div className="v2-stack">
          {person && (
            <span className="v2-row" style={{ gap: 12 }} data-testid="v2-users-budget-current">
              <span>
                {t("v2.budgets.currentLine", {
                  used: fmtUsd(person.used_usd),
                  limit: person.unlimited ? t("v2.usage.unlimited") : fmtUsd(person.limit_usd),
                })}
              </span>
              <span className="v2-muted">{basisLabel(person.basis, person.level?.name)}</span>
              {!person.unlimited && <UsageBar totals={person} />}
            </span>
          )}
          <AssignFields
            role={user.role}
            draft={draft}
            onChange={setDraft}
            levels={levels.data.levels}
            testIdPrefix="v2-users-budget"
          />
          <div className="v2-row">
            <Button
              kind="primary"
              disabled={busy || !changed || !assignDraftValid(draft)}
              onClick={() => void save()}
              testId="v2-users-budget-save"
            >
              {t("v2.common.save")}
            </Button>
            {changed && (
              <Button disabled={busy} onClick={() => setDraft(initial)}>
                {t("v2.common.cancel")}
              </Button>
            )}
          </div>
        </div>
      )}
    </Card>
  );
}
