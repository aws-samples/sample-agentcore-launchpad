import { Plus, Trash2 } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  type AgentForm,
  type AuthToolRow,
  authToolIssues,
  type ConnectionRef,
  emptyAuthToolRow,
} from "../../../lib/agent-spec";
import {
  authCatalogLoading,
  authRowErrors,
  authSectionErrors,
  connectionOptions,
  pickConnection,
} from "../../../lib/identity-ui";
import { Alert, Button, Card, LinkButton, Select, Tag } from "../../ui";
import type { SectionProps } from "./wizardKit";

/**
 * The wizard's Identity (身份) card: rest / mcp tools and the Connection each one
 * calls through (`ToolRef.auth`, acting mode `as_agent` or — OAuth2 only —
 * `as_user`, the invoking user's 3LO consent). A stored row whose
 * Connection is gone stays on screen, flagged, and blocks the submit — the auth
 * block is never dropped silently (see `authToolIssues`).
 */
export function IdentityCard({
  form,
  set,
  touched,
  connections,
  catalogError,
  onRetry,
}: Pick<SectionProps, "form" | "set"> & {
  touched: boolean;
  connections: ConnectionRef[] | null;
  catalogError: boolean;
  onRetry: () => void;
}) {
  const { t } = useTranslation();
  const byoc = form.method === "byoc";
  const issues = authToolIssues(form, connections);
  const patch = (i: number, p: Partial<AuthToolRow>) =>
    set((prev: AgentForm) => ({ authTools: prev.authTools.map((r, j) => (j === i ? { ...r, ...p } : r)) }));
  const add = (type: AuthToolRow["type"]) =>
    set((prev: AgentForm) => ({ authTools: [...prev.authTools, emptyAuthToolRow(type)] }));
  // a stored row that no longer validates shows its problem at once, not after "continue"
  const show = (i: number) => touched || Boolean(issues.rows[i]?.connection && form.authTools[i].connection);
  const sectionErrors = authSectionErrors(t, issues, catalogError);

  return (
    <Card
      title={t("identity.tools.title")}
      sub={t(byoc ? "identity.tools.subByoc" : "identity.tools.sub")}
      testId="v2-agent-identity"
      end={
        <Link to="/v2/connections" className="v2-link">
          {t("identity.tools.manage")}
        </Link>
      }
    >
      {sectionErrors.map((msg) => (
        <Alert key={msg} tone="error" action={catalogError ? <LinkButton onClick={onRetry}>{t("identity.tools.retry")}</LinkButton> : undefined}>
          {msg}
        </Alert>
      ))}
      {authCatalogLoading(issues, catalogError) && <p className="v2-muted">{t("identity.tools.err.catalogPending")}</p>}
      {form.authTools.length === 0 ? (
        <p className="v2-muted">{t("identity.tools.empty")}</p>
      ) : (
        <div className="v2-agents-rows" data-testid="v2-agent-identity-rows">
          {form.authTools.map((row, i) => {
            const errors = show(i) ? authRowErrors(t, row, issues.rows[i]) : [];
            const oauth = row.kind === "oauth2";
            const connOptions = connectionOptions(t, connections, row, row.type === "rest" && !byoc);
            return (
              <div key={i} className="v2-agents-auth" data-testid={`v2-agent-identity-row-${i}`}>
                <div className="v2-agents-row auth-head">
                  <Select
                    value={row.type}
                    ariaLabel={t("identity.tools.type")}
                    onChange={(v) => patch(i, { type: v as AuthToolRow["type"] })}
                    testId={`v2-agent-identity-type-${i}`}
                    options={[
                      { value: "rest", label: t("identity.tools.typeRest") },
                      { value: "mcp", label: t("identity.tools.typeMcp") },
                    ]}
                  />
                  <input
                    className="v2-input mono"
                    value={row.name}
                    placeholder="crm"
                    aria-label={t("identity.tools.name")}
                    onChange={(e) => patch(i, { name: e.target.value })}
                    data-testid={`v2-agent-identity-name-${i}`}
                  />
                  <Select
                    value={row.connection ? `${row.kind}:${row.connection}` : ""}
                    ariaLabel={t("identity.tools.connection")}
                    onChange={(v) => {
                      const picked = pickConnection(v);
                      // acting as the user is OAuth2-only (3LO)
                      patch(i, "kind" in picked && picked.kind !== "oauth2" ? { ...picked, mode: "as_agent" } : picked);
                    }}
                    testId={`v2-agent-identity-connection-${i}`}
                    options={connOptions}
                    // no "none" option (mcp / byoc need a Connection): an unpicked row reads
                    // "choose", not a blank trigger
                    placeholder={connOptions.some((o) => o.value === "") ? undefined : t("v2.common.choose")}
                  />
                  <Button
                    size="sm"
                    onClick={() => set((prev: AgentForm) => ({ authTools: prev.authTools.filter((_, j) => j !== i) }))}
                    title={t("v2.common.delete")}
                    testId={`v2-agent-identity-remove-${i}`}
                  >
                    <Trash2 size={13} aria-hidden="true" />
                  </Button>
                </div>
                <div className="v2-agents-row auth-body">
                  <input
                    className="v2-input mono"
                    value={row.url}
                    placeholder={row.type === "mcp" ? "https://mcp.example.com/mcp" : "https://api.example.com/v1/items"}
                    aria-label={t("identity.tools.url")}
                    onChange={(e) => patch(i, { url: e.target.value })}
                    data-testid={`v2-agent-identity-url-${i}`}
                  />
                  {row.connection && oauth && (
                    <input
                      className="v2-input mono"
                      value={row.scopes}
                      placeholder={t("identity.tools.scopesPlaceholder")}
                      aria-label={t("identity.tools.scopes")}
                      onChange={(e) => patch(i, { scopes: e.target.value })}
                    />
                  )}
                  {row.connection && !oauth && (
                    <div className="v2-agents-row auth-key">
                      <Select
                        value={row.keyIn}
                        ariaLabel={t("identity.tools.keyIn")}
                        onChange={(v) => patch(i, { keyIn: v as AuthToolRow["keyIn"] })}
                        testId={`v2-agent-identity-keyin-${i}`}
                        options={[
                          { value: "header", label: t("identity.tools.keyInHeader") },
                          { value: "query", label: t("identity.tools.keyInQuery") },
                        ]}
                      />
                      <input
                        className="v2-input mono"
                        value={row.keyName}
                        aria-label={t("identity.tools.keyName")}
                        onChange={(e) => patch(i, { keyName: e.target.value })}
                      />
                    </div>
                  )}
                  {row.connection && oauth && !byoc && (
                    <span title={t(row.mode === "as_user" ? "identity.tools.modeHintUser" : "identity.tools.modeHint")}
                    >
                      <Select
                        value={row.mode}
                        ariaLabel={t("identity.tools.mode")}
                        onChange={(v) => patch(i, { mode: v })}
                        testId={`v2-agent-identity-mode-${i}`}
                        options={[
                          { value: "as_agent", label: t("identity.mode.as_agent") },
                          { value: "as_user", label: t("identity.mode.as_user") },
                          // a stored, unsupported mode stays visible (and flagged) rather than snapping
                          ...(row.mode !== "as_agent" && row.mode !== "as_user"
                            ? [{ value: row.mode, label: t(`identity.mode.${row.mode}`, row.mode) }]
                            : []),
                        ]}
                      />
                    </span>
                  )}
                  {row.connection && (!oauth || byoc) && (
                    <Tag tone={issues.rows[i]?.mode ? "red" : "blue"} title={t("identity.tools.modeHint")}>
                      {t(`identity.mode.${row.mode}`, row.mode)}
                    </Tag>
                  )}
                </div>
                {errors.map((msg) => (
                  <p key={msg} className="v2-agents-auth-err" role="alert">
                    {msg}
                  </p>
                ))}
              </div>
            );
          })}
        </div>
      )}
      <div className="v2-agents-actions">
        <Button size="sm" onClick={() => add("rest")} testId="v2-agent-identity-add-rest">
          <Plus size={13} aria-hidden="true" /> {t("identity.tools.addRest")}
        </Button>
        <Button size="sm" onClick={() => add("mcp")} testId="v2-agent-identity-add-mcp">
          <Plus size={13} aria-hidden="true" /> {t("identity.tools.addMcp")}
        </Button>
      </div>
    </Card>
  );
}
