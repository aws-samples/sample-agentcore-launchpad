import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";
import type { AssistantConversationDetail, AssistantProposal } from "../../lib/api";
import type { DeployedAgent } from "../AssistantNextSteps";

const STEPS = ["goals", "resources", "review", "deploy"] as const;
const TARGETS = ["assistant-discussion", "assistant-resources", "assistant-proposal", "assistant-proposal"];

export function CreationProgress({
  conversation, latest, deployed, resourcesDirty, editing,
}: {
  conversation: AssistantConversationDetail | null;
  latest: AssistantProposal | null;
  deployed: DeployedAgent | null;
  resourcesDirty: boolean;
  editing: boolean;
}) {
  const { t } = useTranslation();
  const approved = Boolean(deployed);
  const failed = deployed?.jobStatus === "failed" || deployed?.agentStatus === "failed";
  const ready = deployed?.jobStatus === "succeeded" && deployed?.agentStatus === "active";
  const running = deployed?.jobStatus === "queued" || deployed?.jobStatus === "running"
    || deployed?.agentStatus === "deploying";
  const valid = latest?.status === "draft" && latest.validation_errors.length === 0;
  // A proposal establishes a requirements baseline. Empty or ongoing discussion
  // alone is never evidence that the goals or resources are ready.
  const completed = approved
    ? [true, true, true, Boolean(ready)]
    : [
      Boolean(latest && typeof latest.content.system_prompt === "string" && latest.content.system_prompt.trim()),
      Boolean(valid && !resourcesDirty && !editing),
      false,
      false,
    ];
  const done = completed.filter(Boolean).length;
  const active = completed.findIndex((value) => !value);
  const hint = failed ? "failed"
    : ready ? "ready"
      : approved ? (running ? "deploying" : "unavailable")
        : resourcesDirty ? "unsaved"
          : editing ? "editing"
            : latest?.status === "invalid" ? "invalid"
              : latest?.status === "rejected" ? "rejected"
                : valid ? "review" : latest ? "resources" : "goals";

  return (
    <nav className="assist-progress" aria-label={t("assistantProgress.title")}
      data-testid="creation-progress" data-completed={done}>
      <div className="assist-progress-head">
        <strong>{t("assistantProgress.title")}</strong>
        <span>{t("assistantProgress.count", { done, total: STEPS.length })}</span>
      </div>
      <div className="assist-progress-meter" role="progressbar" aria-valuemin={0}
        aria-valuemax={STEPS.length} aria-valuenow={done}
        aria-label={t("assistantProgress.title")}
        aria-valuetext={t("assistantProgress.count", { done, total: STEPS.length })}>
        {STEPS.map((step, i) => <span key={step} className={completed[i] ? "done" : ""} />)}
      </div>
      <ol className="assist-progress-steps">
        {STEPS.map((step, index) => {
          const state = failed && index === 3 ? "failed"
            : completed[index] ? "done" : index === active ? "current" : "pending";
          return (
            <li key={step} data-state={state}>
              <a href={`#${TARGETS[index]}`} aria-current={index === active ? "step" : undefined}>
                <span className="assist-progress-number" aria-hidden="true">
                  {state === "done" ? "✓" : state === "failed" ? "!" : index + 1}
                </span>
                <span>
                  <strong>{t(`assistantProgress.steps.${step}`)}</strong>
                  <small>{t(`assistantProgress.states.${state}`)}</small>
                </span>
              </a>
            </li>
          );
        })}
      </ol>
      <div className={`assist-progress-next${failed ? " failed" : ""}`} role="status">
        {t(`assistantProgress.hints.${hint}`)}
        {approved && <Link to="/agents">{t("assistantProgress.manage")}</Link>}
      </div>
      <div className="assist-progress-later">
        <span>{t("assistantProgress.later")}</span>
        {conversation && latest
          ? <a href="#assistant-evaluation">{t("assistantProgress.evaluate")}</a>
          : <Link to="/evaluation">{t("assistantProgress.evaluate")}</Link>}
        <Link to="/evaluation?view=experiment">{t("assistantProgress.experiment")}</Link>
        <Link to="/observability">{t("assistantProgress.feedback")}</Link>
      </div>
    </nav>
  );
}
