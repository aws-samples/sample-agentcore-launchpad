import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { AssistantProposal } from "../../../lib/api";
import { CREATION_STEPS, creationProgress, type DeployedAgent } from "../../../lib/assistant";
import { Alert, Card, Tag } from "../../ui";
import { scrollToSection, SECTION_IDS, STEP_TARGETS } from "./common";

/** The four required creation steps + what to do next + the deferrable later steps. */
export function CreationProgressCard({
  latest, deployed, resourcesDirty, editing,
}: {
  latest: AssistantProposal | null;
  deployed: DeployedAgent | null;
  resourcesDirty: boolean;
  editing: boolean;
}) {
  const { t } = useTranslation();
  const p = creationProgress({ latest, deployed, resourcesDirty, editing });
  const tone = p.failed ? "error" : p.ready ? "success" : p.hint === "unsaved" || p.hint === "invalid" ? "warn" : "info";
  return (
    <Card
      title={t("assistantProgress.title")}
      end={<Tag tone={p.ready ? "green" : "blue"}>{t("assistantProgress.count", { done: p.done, total: CREATION_STEPS.length })}</Tag>}
      testId="v2-assistant-progress"
    >
      <div className="v2-stages v2-assistant-steps" data-completed={p.done}>
        {CREATION_STEPS.map((step, index) => {
          const state = p.states[index];
          return (
            <button
              key={step}
              type="button"
              className={`v2-stage ${state}`}
              aria-current={index === p.active ? "step" : undefined}
              onClick={() => scrollToSection(STEP_TARGETS[index])}
              data-testid={`v2-assistant-step-${step}`}
              data-state={state}
            >
              <span className="n">{state === "done" ? "✓" : state === "failed" ? "!" : index + 1}</span>
              <span className="b">
                <span className="t">{t(`assistantProgress.steps.${step}`)}</span>
                <span className="d">{t(`assistantProgress.states.${state}`)}</span>
              </span>
            </button>
          );
        })}
      </div>
      <Alert
        tone={tone}
        action={p.approved ? <Link to="/v2/agents" className="v2-link">{t("assistantProgress.manage")}</Link> : undefined}
      >
        <span data-testid="v2-assistant-progress-hint">{t(`assistantProgress.hints.${p.hint}`)}</span>
      </Alert>
      <div className="v2-assistant-later">
        <span>{t("assistantProgress.later")}</span>
        {latest ? (
          <button type="button" className="v2-link" onClick={() => scrollToSection(SECTION_IDS.evaluation)}>
            {t("assistantProgress.evaluate")}
          </button>
        ) : (
          <Link to="/v2/eval/tasks">{t("assistantProgress.evaluate")}</Link>
        )}
        <Link to="/v2/eval/experiments">{t("assistantProgress.experiment")}</Link>
        <Link to="/v2/observability">{t("assistantProgress.feedback")}</Link>
      </div>
    </Card>
  );
}

const ADLC_STAGES = ["define", "build", "evaluate", "release", "observe", "learn"] as const;

/** The ADLC methodology is explanatory; its arrows do not represent completed jobs. */
export function AdlcCard({ open }: { open: boolean }) {
  const { t } = useTranslation();
  return (
    <section className="v2-card" data-testid="v2-assistant-adlc">
      <details className="v2-card-body v2-assistant-adlc" open={open || undefined}>
        <summary>
          <Tag tone="blue">ADLC</Tag>
          <span style={{ fontWeight: 600 }}>{t("assistantAdlc.title")}</span>
          <span className="v2-muted">{t("assistantAdlc.expand")}</span>
        </summary>
        <div className="v2-assistant-adlc-flow" role="list" aria-label={t("assistantAdlc.description")}>
          {ADLC_STAGES.map((stage, index) => (
            <div key={stage} className="v2-flow-step" role="listitem">
              <span className="n">{index + 1}</span>
              <span className="t">{t(`assistantAdlc.stages.${stage}.title`)}</span>
              <span className="d">{t(`assistantAdlc.stages.${stage}.detail`)}</span>
              <span className="d">{t(`assistantAdlc.stages.${stage}.extra`)}</span>
            </div>
          ))}
        </div>
        <div className="v2-assistant-adlc-loop">↺ {t("assistantAdlc.feedback")}</div>
        <p className="v2-muted" style={{ margin: "12px 0 0", fontSize: 13 }}>{t("assistantAdlc.principle")}</p>
      </details>
    </section>
  );
}
