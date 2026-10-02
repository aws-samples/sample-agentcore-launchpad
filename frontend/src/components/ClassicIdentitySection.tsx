import type { Dispatch, SetStateAction } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { type AgentForm, type AuthToolRow, authToolIssues, type ConnectionRef, emptyAuthToolRow } from "../lib/agent-spec";
import {
  authCatalogLoading,
  authRowErrors,
  authSectionErrors,
  connectionOptions,
  pickConnection,
} from "../lib/identity-ui";
import { Btn } from "./Btn";

/**
 * The classic configure step's Identity section — the same rows, validation and
 * copy as the v2 wizard's `IdentityCard`, in the classic markup. A stored row
 * whose Connection is gone stays, flagged, and blocks the deploy.
 */
export default function ClassicIdentitySection({
  form,
  setRows,
  connections,
  catalogError,
  onRetry,
}: {
  form: AgentForm;
  setRows: Dispatch<SetStateAction<AuthToolRow[]>>;
  connections: ConnectionRef[] | null;
  catalogError: boolean;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const byoc = form.method === "byoc";
  const issues = authToolIssues(form, connections);
  const patch = (i: number, p: Partial<AuthToolRow>) =>
    setRows((prev) => prev.map((r, j) => (j === i ? { ...r, ...p } : r)));
  return (
    <div className="field" data-testid="classic-identity">
      <label>{t("identity.tools.title")}</label>
      <div className="note" style={{ marginBottom: 8 }}>
        <span className="i">[i]</span>
        <span>
          {t(byoc ? "identity.tools.subByoc" : "identity.tools.sub")}{" "}
          <Link to="/v2/connections">{t("identity.tools.manage")}</Link>
        </span>
      </div>
      {authSectionErrors(t, issues, catalogError).map((msg) => (
        <div key={msg} className="mono" role="alert" style={{ color: "var(--crit)", fontSize: 12, marginBottom: 6 }}>
          {msg}{" "}
          {catalogError && (
            <Btn className="small" onClick={onRetry}>
              {t("identity.tools.retry")}
            </Btn>
          )}
        </div>
      ))}
      {authCatalogLoading(issues, catalogError) && (
        <div className="mono" style={{ color: "var(--ink-3)", fontSize: 12, marginBottom: 6 }}>
          {t("identity.tools.err.catalogPending")}
        </div>
      )}
      {form.authTools.map((row, i) => (
        <div key={i} style={{ marginBottom: 10 }}>
          <div style={{ display: "flex", gap: 8, marginBottom: 6 }}>
            <select
              className="input"
              style={{ flex: "0 0 110px" }}
              value={row.type}
              aria-label={t("identity.tools.type")}
              onChange={(e) => patch(i, { type: e.target.value as AuthToolRow["type"] })}
            >
              <option value="rest">{t("identity.tools.typeRest")}</option>
              <option value="mcp">{t("identity.tools.typeMcp")}</option>
            </select>
            <input
              className="input mono"
              style={{ flex: 1 }}
              value={row.name}
              placeholder="crm"
              aria-label={t("identity.tools.name")}
              onChange={(e) => patch(i, { name: e.target.value })}
            />
            <select
              className="input mono"
              style={{ flex: 1 }}
              value={row.connection ? `${row.kind}:${row.connection}` : ""}
              aria-label={t("identity.tools.connection")}
              onChange={(e) => patch(i, pickConnection(e.target.value))}
            >
              {connectionOptions(t, connections, row, row.type === "rest" && !byoc).map((o) => (
                <option key={o.value} value={o.value}>
                  {o.label}
                </option>
              ))}
            </select>
            <Btn onClick={() => setRows((prev) => prev.filter((_, j) => j !== i))}>✕</Btn>
          </div>
          <div style={{ display: "flex", gap: 8 }}>
            <input
              className="input mono"
              style={{ flex: 2 }}
              value={row.url}
              placeholder="https://api.example.com/v1/items"
              aria-label={t("identity.tools.url")}
              onChange={(e) => patch(i, { url: e.target.value })}
            />
            {row.connection && row.kind === "oauth2" && (
              <input
                className="input mono"
                style={{ flex: 1 }}
                value={row.scopes}
                placeholder={t("identity.tools.scopesPlaceholder")}
                aria-label={t("identity.tools.scopes")}
                onChange={(e) => patch(i, { scopes: e.target.value })}
              />
            )}
            {row.connection && row.kind === "api_key" && (
              <>
                <select
                  className="input"
                  style={{ flex: "0 0 100px" }}
                  value={row.keyIn}
                  aria-label={t("identity.tools.keyIn")}
                  onChange={(e) => patch(i, { keyIn: e.target.value as AuthToolRow["keyIn"] })}
                >
                  <option value="header">{t("identity.tools.keyInHeader")}</option>
                  <option value="query">{t("identity.tools.keyInQuery")}</option>
                </select>
                <input
                  className="input mono"
                  style={{ flex: 1 }}
                  value={row.keyName}
                  aria-label={t("identity.tools.keyName")}
                  onChange={(e) => patch(i, { keyName: e.target.value })}
                />
              </>
            )}
            {row.connection && (
              <span className="selchip on" title={t("identity.tools.modeHint")}>
                {t(`identity.mode.${row.mode}`, row.mode)}
              </span>
            )}
          </div>
          {authRowErrors(t, row, issues.rows[i]).map((msg) => (
            <div key={msg} className="mono" role="alert" style={{ color: "var(--crit)", fontSize: 12, marginTop: 4 }}>
              {msg}
            </div>
          ))}
        </div>
      ))}
      <div style={{ display: "flex", gap: 8 }}>
        <Btn className="small" onClick={() => setRows((prev) => [...prev, emptyAuthToolRow("rest")])}>
          + {t("identity.tools.addRest")}
        </Btn>
        <Btn className="small" onClick={() => setRows((prev) => [...prev, emptyAuthToolRow("mcp")])}>
          + {t("identity.tools.addMcp")}
        </Btn>
      </div>
    </div>
  );
}
