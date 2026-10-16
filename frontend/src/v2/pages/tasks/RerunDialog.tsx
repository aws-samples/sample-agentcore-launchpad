import type { TFunction } from "i18next";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useSearchParams } from "react-router-dom";

import { api, errorMessage, type ScorecardAgent, type ScorecardRun } from "../../../lib/api";
import { rememberBudgetNotice } from "../../../lib/budgets";
import type { EvaluationRunInfo } from "../../../lib/evaluation";
import { useV2Toast } from "../../hooks";
import { shortDate } from "../../scorecard";
import { Alert, Button, Descriptions, Field, Modal } from "../../ui";
import { backState } from "./back";
import { benchName, evaluatorList, runSourceText } from "./scorecardText";

/** The backend's task-name limit (`RunRerun.name`). */
const NAME_MAX = 64;

/** The default task name: "<benchmark> · #<current> · <MM-DD>". */
function defaultName(t: TFunction, agent: ScorecardAgent, source: ScorecardRun): string {
  const vars = { bench: benchName(t, source.benchmark), seq: agent.current_seq, date: shortDate(new Date().toISOString()) };
  const key = agent.current_seq != null ? "v2.scorecard.rerun.defaultName" : "v2.scorecard.rerun.defaultNameNoVersion";
  return t(key, vars).slice(0, NAME_MAX);
}

/**
 * 再评一次: evaluate the agent as it is now on the source run's own scope (the same
 * dataset and pinned version, or the same window) with the same evaluators — one
 * `POST /api/eval/runs/{id}/rerun`. The task name is editable; a budget notice on the
 * response is remembered for the new run's detail page, as the wizard does.
 */
export function RerunDialog({
  agent,
  source,
  onClose,
  onStarted,
}: {
  agent: ScorecardAgent;
  source: ScorecardRun;
  onClose: () => void;
  onStarted: (run: EvaluationRunInfo) => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [, setParams] = useSearchParams();
  const location = useLocation();
  const [name, setName] = useState(() => defaultName(t, agent, source));
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const start = async () => {
    const trimmed = name.trim();
    if (!trimmed) {
      setError(t("v2.tasks.errName"));
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const run = await api.rerunEvaluationRun(source.id, { name: trimmed.slice(0, NAME_MAX) });
      // its estimate is more than the creator has left: the agent page and the new
      // run's detail page say so
      rememberBudgetNotice(run.id, run.budget_notice);
      toast("success", t("v2.scorecard.rerun.started", { name: run.name || trimmed }));
      onStarted(run);
    } catch (err) {
      setError(errorMessage(err));
      setBusy(false);
    }
  };

  const seq = agent.current_seq;
  const size = source.benchmark.size;
  const data = [
    runSourceText(t, source),
    source.scope === "cloud" && !source.dataset_version ? t("v2.scorecard.rerun.draft") : null,
    size != null && source.scope !== "window" ? t("v2.scorecard.benchSize", { count: size }) : null,
  ]
    .filter(Boolean)
    .join(" · ");

  return (
    <Modal
      open
      title={t("v2.scorecard.rerun.title")}
      onClose={onClose}
      testId="v2-scorecard-rerun"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button onClick={() => setParams({ view: "new", from: `run:${source.id}` }, { state: backState(location.search) })} testId="v2-scorecard-rerun-wizard">
            {t("v2.scorecard.rerun.wizard")}
          </Button>
          <Button kind="primary" disabled={busy} onClick={() => void start()} testId="v2-scorecard-rerun-confirm">
            {busy ? t("v2.scorecard.rerun.starting") : t("v2.scorecard.rerun.start")}
          </Button>
        </>
      }
    >
      <div className="v2-stack">
        <p className="v2-sc-intro">{t("v2.scorecard.rerun.intro")}</p>
        <Descriptions
          one
          items={[
            {
              label: t("v2.scorecard.rerun.agent"),
              value:
                seq != null
                  ? t("v2.scorecard.rerun.agentValue", { name: agent.name, seq })
                  : `${agent.name} · ${t("v2.scorecard.noVersion")}`,
            },
            { label: t("v2.scorecard.rerun.data"), value: data },
            { label: t("v2.scorecard.rerun.evaluators"), value: evaluatorList(t, source.evaluators) || "—" },
          ]}
        />
        <Field label={t("v2.tasks.colName")} required>
          <input
            className="v2-input"
            value={name}
            maxLength={NAME_MAX}
            onChange={(e) => setName(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !busy) void start();
            }}
            aria-label={t("v2.tasks.colName")}
            data-testid="v2-scorecard-rerun-name"
          />
        </Field>
        <Alert tone="info">
          {seq != null ? t("v2.scorecard.rerun.note", { seq }) : t("v2.scorecard.rerun.noteNoVersion")}
        </Alert>
        {error && <Alert tone="error">{error}</Alert>}
      </div>
    </Modal>
  );
}
