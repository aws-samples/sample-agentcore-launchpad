import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";

import type { NewStrategy } from "../../../v2/pages/tasks/urls";
import { Panel } from "../../ui";
import { Option } from "./common";
import { strategyUrl } from "./model";

/**
 * The first choice of both create forms, as in V2: evaluate once (`TaskWizard`, a batch
 * run) or keep evaluating new sessions (`OnlineEditor`, an online evaluation config).
 * Picking the other opens its form in place (replace) with the same agent preselected
 * and the history state kept, so Back still returns to where the form was opened from.
 * It always lands on `/v3/tasks`: the editor is also mounted at `/v3/online?view=new`,
 * where a bare search-param write could not reach the run wizard.
 */
export function StrategySwitch({ value, agentId }: { value: NewStrategy; agentId?: string | null }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const location = useLocation();
  const choose = (next: NewStrategy) => {
    if (next !== value) navigate(strategyUrl(next, agentId), { replace: true, state: location.state });
  };
  return (
    <Panel title={t("v2.tasks.strategy")}>
      <div className="v3-task-opts c2" role="radiogroup" aria-label={t("v2.tasks.strategy")} data-testid="v3-task-strategy">
        <Option on={value === "history"} title={t("v2.scorecard.tabs.runs")} desc={t("v2.tasks.strategyHistoryDesc")}
          onClick={() => choose("history")} />
        <Option on={value === "continuous"} title={t("v2.scorecard.tabs.online")} desc={t("v2.tasks.strategyContinuousDesc")}
          hint={t("glossary.onlineEvaluation")} onClick={() => choose("continuous")} />
      </div>
    </Panel>
  );
}
