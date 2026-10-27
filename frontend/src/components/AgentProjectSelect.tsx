import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { agentProjectLabel } from "../lib/agent-project";
import { errorMessage } from "../lib/api";
import type { AgentProjectPick } from "../lib/useAgentProjectPick";
import { Btn } from "./Btn";

/**
 * The classic console's picker for the business project a NEW agent is filed under
 * (`useAgentProjectPick`): the wizard, the import page, the Studio publish dialog,
 * the architect's approve and convert confirmations. A failed, loading or empty
 * eligible list says why the action cannot go on and links the projects page.
 */
export function AgentProjectSelect({
  pick,
  id = "agent-project",
  touched = false,
}: {
  pick: AgentProjectPick;
  id?: string;
  /** an attempt was made: a missing pick reads as an error */
  touched?: boolean;
}) {
  const { t } = useTranslation();
  const open = (
    <Link to="/v3/projects" style={{ textDecoration: "underline" }}>
      {t("v3.create.project.open")}
    </Link>
  );
  const note = (tone: "crit" | "amber", text: string, extra?: ReactNode) => (
    <div className="note" style={{ borderColor: `var(--${tone})` }} role={tone === "crit" ? "alert" : undefined}>
      <span className="i" style={{ color: `var(--${tone})` }}>
        [!]
      </span>
      <span>
        {text} {extra}
      </span>
    </div>
  );
  return (
    <div className="field" data-testid={`${id}-field`}>
      <label htmlFor={id}>{t("v3.create.project.label")}</label>
      {pick.error != null ? (
        note(
          "crit",
          errorMessage(pick.error),
          <Btn onClick={pick.reload}>{t("v2.common.retry")}</Btn>,
        )
      ) : pick.loading ? (
        <div className="dim">{t("v2.common.loading")}</div>
      ) : pick.rows.length === 0 ? (
        note(touched ? "crit" : "amber", t("v3.create.project.none"), open)
      ) : (
        <>
          <select
            id={id}
            className="input"
            value={pick.projectId}
            onChange={(e) => pick.pick(e.target.value)}
            aria-invalid={touched && !pick.projectId}
            data-testid={id}
          >
            <option value="" disabled>
              {t("v3.create.project.placeholder")}
            </option>
            {pick.rows.map((p) => (
              <option key={p.id} value={p.id}>
                {agentProjectLabel(p)}
              </option>
            ))}
          </select>
          <div className="dim" style={{ marginTop: 6, fontSize: 12, color: touched && !pick.projectId ? "var(--crit)" : undefined }}>
            {touched && !pick.projectId ? t("v3.create.project.err") : t("v3.create.project.hint")}{" "}
            {!pick.projectId && open}
          </div>
        </>
      )}
    </div>
  );
}
