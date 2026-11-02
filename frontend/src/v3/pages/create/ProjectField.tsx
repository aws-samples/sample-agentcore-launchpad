import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { agentProjectCopyKeys, agentProjectOptionLabel } from "../../../lib/agent-project";
import type { AgentProjectPick } from "../../../lib/useAgentProjectPick";
import { Select } from "../../select";
import { Notice, Skeleton } from "../../ui";

/**
 * The business project a NEW agent is filed under (`useAgentProjectPick`): the
 * create form's and the convert dialog's picker. `touched` turns a missing pick
 * (or an empty eligible list) into an error; `idBase` keeps two on one page apart.
 */
export function ProjectField({
  pick,
  touched,
  idBase = "v3-create-project",
}: {
  pick: AgentProjectPick;
  touched: boolean;
  idBase?: string;
}) {
  const { t } = useTranslation();
  const missing = !pick.projectId;
  const copy = agentProjectCopyKeys(pick.defaultAllowed);
  const open = (
    <Link to="/v3/projects" style={{ textDecoration: "underline" }}>
      {t("v3.create.project.open")}
    </Link>
  );
  return (
    <div className="v3-field" data-testid={`${idBase}-field`}>
      <span id={`${idBase}-label`}>
        {t("v3.create.project.label")} · {t("v3.create.required")}
      </span>
      {pick.error != null ? (
        <Notice s="act">
          <ApiErrorDetail error={pick.error} />{" "}
          <button type="button" className="v3-btn sm" onClick={pick.reload}>
            {t("v3.ux.retry")}
          </button>
        </Notice>
      ) : pick.loading ? (
        <Skeleton rows={1} />
      ) : pick.rows.length === 0 ? (
        <small id={`${idBase}-hint`} className={touched ? "v3-err" : "v3-hint"} role={touched ? "alert" : undefined}>
          {t(copy.none)} {open}
        </small>
      ) : (
        <>
          <Select
            className="v3-select"
            value={pick.projectId}
            aria-labelledby={`${idBase}-label`}
            aria-describedby={`${idBase}-hint`}
            aria-invalid={touched && missing}
            onChange={(e) => pick.pick(e.target.value)}
            data-testid={idBase}
          >
            <option value="" disabled>
              {t(copy.placeholder)}
            </option>
            {pick.rows.map((p) => (
              <option key={p.id} value={p.id}>
                {agentProjectOptionLabel(p, t("v3.create.project.defaultTag"))}
              </option>
            ))}
          </Select>
          <small id={`${idBase}-hint`} className={touched && missing ? "v3-err" : "v3-hint"}>
            {touched && missing ? t("v3.create.project.err") : t(copy.hint)} {missing && open}
          </small>
        </>
      )}
    </div>
  );
}
