import type { TFunction } from "i18next";
import { describe, expect, it } from "vitest";

import { authToolIssues, emptyAgentForm, emptyAuthToolRow } from "./agent-spec";
import { authCatalogLoading, authSectionErrors, connectionOptions } from "./identity-ui";

const t = ((key: string, opts?: { name?: string }) => (opts?.name ? `${key}:${opts.name}` : key)) as unknown as TFunction;
const row = { ...emptyAuthToolRow("rest"), name: "crm", connection: "team-idp", kind: "oauth2" as const };

describe("identity section while the catalog loads", () => {
  it("shows a stored Connection as-is, not as gone, before the catalog arrives", () => {
    expect(connectionOptions(t, null, row, true).map((o) => o.label)).toContain("team-idp");
    expect(connectionOptions(t, [], row, true).map((o) => o.label)).toContain("identity.tools.connectionGone:team-idp");
  });

  it("reports loading as a notice, and only a failed load as an error", () => {
    const form = { ...emptyAgentForm(), method: "zip_runtime" as const, protocol: "http" as const, authTools: [row] };
    const issues = authToolIssues(form, null);
    expect(authCatalogLoading(issues, false)).toBe(true);
    expect(authSectionErrors(t, issues, false)).toEqual([]);
    expect(authSectionErrors(t, issues, true)).toEqual(["identity.tools.err.catalogError"]);
  });
});
