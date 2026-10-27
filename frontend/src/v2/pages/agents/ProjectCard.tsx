import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { agentProjectLabel } from "../../../lib/agent-project";
import { errorMessage } from "../../../lib/api";
import type { AgentProjectPick } from "../../../lib/useAgentProjectPick";
import { Alert, Button, Card, Field, Select } from "../../ui";

/**
 * The business project a NEW agent is filed under (services/agent_project): the
 * server-filtered eligible list (`useAgentProjectPick`), so a `default` global
 * project never reaches the create call. The V2 shell has no project switch, so the
 * pick lives in the flow itself; the loading, failed and empty states each say why
 * it cannot go on. `ProjectField` is the bare field (dialogs), `ProjectCard` the
 * wizard's card around it.
 */
export function ProjectField({ pick, error }: { pick: AgentProjectPick; error?: string }) {
  const { t } = useTranslation();
  const open = (
    <Link to="/v3/projects" className="v2-link">
      {t("v3.create.project.open")}
    </Link>
  );
  let body: ReactNode;
  if (pick.error != null) {
    body = (
      <Alert
        tone="error"
        action={
          <Button size="sm" onClick={pick.reload} testId="v2-agent-project-retry">
            {t("v2.common.retry")}
          </Button>
        }
      >
        {errorMessage(pick.error)}
      </Alert>
    );
  } else if (pick.loading) {
    body = <span className="hint">{t("v2.common.loading")}</span>;
  } else if (pick.rows.length === 0) {
    body = (
      <Alert tone={error ? "error" : "warn"}>
        {t("v3.create.project.none")} {open}
      </Alert>
    );
  } else {
    body = (
      <>
        <Field label={t("v3.create.project.label")} required error={error} hint={t("v3.create.project.hint")}>
          <Select
            value={pick.projectId}
            placeholder={t("v3.create.project.placeholder")}
            options={pick.rows.map((p) => ({ value: p.id, label: agentProjectLabel(p) }))}
            onChange={pick.pick}
            testId="v2-agent-project"
          />
        </Field>
        {/* outside the field: its error replaces the hint, the way out stays */}
        {!pick.projectId && open}
      </>
    );
  }
  return (
    <div className="v2-form" data-testid="v2-agent-project-field">
      {body}
    </div>
  );
}

export function ProjectCard({ pick, error }: { pick: AgentProjectPick; error?: string }) {
  const { t } = useTranslation();
  return (
    <Card title={t("v3.create.project.label")} testId="v2-agent-project-card">
      <ProjectField pick={pick} error={error} />
    </Card>
  );
}
