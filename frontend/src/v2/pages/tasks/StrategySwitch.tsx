import { useTranslation } from "react-i18next";
import { useLocation, useSearchParams } from "react-router-dom";

import { Card, OptionCard } from "../../ui";
import { type NewStrategy, newTaskParams } from "./urls";

/**
 * The top of both create forms: evaluate once (`TaskWizard`, a batch run) or keep
 * evaluating new sessions (`OnlineEditor`, an online evaluation config). Picking the
 * other one opens its form in place (replace) with the same agent preselected and the
 * history state kept, so Back still returns to where the form was opened from.
 */
export function StrategySwitch({ value, agentId }: { value: NewStrategy; agentId?: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const location = useLocation();
  const choose = (next: NewStrategy) => {
    if (next !== value) setParams(newTaskParams(next, agentId), { replace: true, state: location.state });
  };
  return (
    <Card title={t("v2.tasks.strategy")} testId="v2-task-strategy">
      <div className="v2-options" style={{ gridTemplateColumns: "repeat(2, minmax(0, 1fr))" }}>
        <OptionCard
          title={t("v2.scorecard.tabs.runs")}
          desc={t("v2.tasks.strategyHistoryDesc")}
          on={value === "history"}
          onClick={() => choose("history")}
          testId="v2-task-strategy-history"
        />
        <OptionCard
          title={t("v2.scorecard.tabs.online")}
          desc={t("v2.tasks.strategyContinuousDesc")}
          hint={t("glossary.onlineEvaluation")}
          on={value === "continuous"}
          onClick={() => choose("continuous")}
          testId="v2-task-strategy-continuous"
        />
      </div>
    </Card>
  );
}
