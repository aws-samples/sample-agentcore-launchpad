import type { TFunction } from "i18next";

import type { AuthRowIssues, AuthToolIssues, AuthToolRow, ConnectionRef } from "./agent-spec";

/** Messages for one Identity row's problems — the same copy in classic and v2. */
export function authRowErrors(t: TFunction, row: AuthToolRow, issues: AuthRowIssues | undefined): string[] {
  if (!issues) return [];
  const out: string[] = [];
  if (issues.name === "invalid") out.push(t(row.type === "rest" ? "identity.tools.err.nameRest" : "identity.tools.err.name"));
  if (issues.name === "duplicate") out.push(t("identity.tools.err.nameDuplicate"));
  if (issues.url) out.push(t("identity.tools.err.url"));
  if (issues.connection === "required")
    out.push(t(row.type === "mcp" ? "identity.tools.err.connectionMcp" : "identity.tools.err.connectionByoc"));
  if (issues.connection === "missing") out.push(t("identity.tools.err.connectionMissing", { name: row.connection }));
  if (issues.connection === "kind")
    out.push(t("identity.tools.err.connectionKind", { name: row.connection, kind: t(`identity.kind.${row.kind}`) }));
  if (issues.mode) out.push(t("identity.tools.err.mode", { mode: row.mode }));
  if (issues.scopes) out.push(t("identity.tools.err.scopes"));
  return out;
}

/** Section-level problems (not tied to one row). A catalog still loading is not an
 *  error — callers show it as a neutral notice (`authCatalogLoading`). */
export function authSectionErrors(t: TFunction, issues: AuthToolIssues, catalogError: boolean): string[] {
  const out: string[] = [];
  if (issues.unsupported) out.push(t("identity.tools.err.unsupported"));
  if (issues.catalogPending && catalogError) out.push(t("identity.tools.err.catalogError"));
  return out;
}

/** True while the Connection catalog is still loading and a row needs it. */
export function authCatalogLoading(issues: AuthToolIssues, catalogError: boolean): boolean {
  return issues.catalogPending && !catalogError;
}

/** The Connection picker's options: live Connections, plus the row's stored value
 *  when the catalog no longer has it (kept visible so it is never silently lost).
 *  Before the catalog has loaded the stored value is shown as-is, not as gone. */
export function connectionOptions(
  t: TFunction,
  connections: ConnectionRef[] | null,
  row: AuthToolRow,
  allowNone: boolean,
): { value: string; label: string }[] {
  const live = (connections ?? []).filter((c) => c.status !== "missing");
  const options = live
    .filter((c) => row.type !== "mcp" || c.kind === "oauth2")
    .map((c) => ({ value: `${c.kind}:${c.name}`, label: `${c.name} · ${t(`identity.kind.${c.kind}`)}` }));
  const current = row.connection ? `${row.kind}:${row.connection}` : "";
  if (current && !options.some((o) => o.value === current))
    options.unshift({
      value: current,
      label: connections === null ? row.connection : t("identity.tools.connectionGone", { name: row.connection }),
    });
  return [...(allowNone ? [{ value: "", label: t("identity.tools.connectionNone") }] : []), ...options];
}

/** Apply a picked `kind:name` option value to a row. */
export function pickConnection(value: string): Pick<AuthToolRow, "connection" | "kind"> | { connection: "" } {
  if (!value) return { connection: "" };
  const at = value.indexOf(":");
  return { kind: value.slice(0, at) as AuthToolRow["kind"], connection: value.slice(at + 1) };
}
