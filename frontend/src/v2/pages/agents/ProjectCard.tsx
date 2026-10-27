import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { agentProjectLabel } from "../../../lib/agent-project";
import type { EligibleAgentProject } from "../../../lib/api";
import type { Loaded } from "../../hooks";
import { Alert, Button, Card, Field, Select } from "../../ui";

/**
 * The business project a NEW agent is filed under (services/agent_project): the
 * server-filtered eligible list, so a `default` global project never reaches the
 * create call. The V2 shell has no project switch, so the pick lives here; the
 * loading, failed and empty states each say why the wizard cannot go on.
 */
export function ProjectCard({
  projects,
  value,
  onPick,
  error,
}: {
  projects: Loaded<{ projects: EligibleAgentProject[] }>;
  value: string;
  onPick: (id: string) => void;
  error?: string;
}) {
  const { t } = useTranslation();
  const rows = projects.data?.projects ?? [];
  const open = (
    <Link to="/v3/projects" className="v2-link">
      {t("v3.create.project.open")}
    </Link>
  );
  let body: ReactNode;
  if (projects.error) {
    body = (
      <Alert
        tone="error"
        action={
          <Button size="sm" onClick={projects.reload} testId="v2-agent-project-retry">
            {t("v2.common.retry")}
          </Button>
        }
      >
        {projects.error}
      </Alert>
    );
  } else if (projects.loading && !projects.data) {
    body = <span className="hint">{t("v2.common.loading")}</span>;
  } else if (rows.length === 0) {
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
            value={value}
            placeholder={t("v3.create.project.placeholder")}
            options={rows.map((p) => ({ value: p.id, label: agentProjectLabel(p) }))}
            onChange={onPick}
            testId="v2-agent-project"
          />
        </Field>
        {/* outside the field: its error replaces the hint, the way out stays */}
        {!value && open}
      </>
    );
  }
  return (
    <Card title={t("v3.create.project.label")} testId="v2-agent-project-card">
      <div className="v2-form">{body}</div>
    </Card>
  );
}
