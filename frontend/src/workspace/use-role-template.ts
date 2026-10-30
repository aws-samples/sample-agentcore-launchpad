import { useCallback, useEffect, useState } from "react";

import { api, type Workspace, type WorkspaceRoleTemplate } from "../lib/api";

/**
 * The spoke role template check for one workspace, shared by the V2 and V3
 * workspace pages. Only a READY cross-account workspace is asked: the hub's own
 * account has no spoke role, and before bootstrap the stack may not exist yet.
 * A failed read is not surfaced — the check is advisory, and the page has its own
 * error handling for the reads that matter.
 */
export function useRoleTemplate(row: Workspace | null) {
  const eligible = !!row && row.cross_account && row.bootstrap_status === "ready";
  const id = row?.id ?? "";
  const [data, setData] = useState<WorkspaceRoleTemplate | null>(null);
  const [loading, setLoading] = useState(false);

  const load = useCallback(
    async (refresh: boolean) => {
      if (!eligible) return;
      setLoading(true);
      try {
        setData(await api.getWorkspaceRoleTemplate(id, { refresh }));
      } catch {
        setData(null);
      } finally {
        setLoading(false);
      }
    },
    [eligible, id],
  );

  useEffect(() => {
    setData(null);
    void load(false);
  }, [load]);

  return { data: eligible ? data : null, eligible, loading, recheck: () => load(true) };
}

type T = (key: string, opts?: Record<string, unknown>) => string;

/** The deployed version as the copy names it: `v3`, or "from before version tracking". */
export function deployedLabel(t: T, d: WorkspaceRoleTemplate): string {
  return d.deployed != null ? `v${d.deployed}` : t("v2.workspaces.roleTemplate.untagged");
}

/** One line for the workspace summary's "Role template" row. */
export function roleTemplateSummary(t: T, d: WorkspaceRoleTemplate | null, loading: boolean): string {
  if (!d) return loading ? t("v2.workspaces.roleTemplate.checking") : "—";
  const k = "v2.workspaces.roleTemplate";
  switch (d.status) {
    case "current":
      return t(`${k}.current`, { deployed: d.deployed });
    case "outdated":
      return t(`${k}.outdatedShort`, { deployed: deployedLabel(t, d), expected: d.expected });
    case "newer":
      return t(`${k}.newer`, { deployed: d.deployed, expected: d.expected });
    case "unknown":
      return t(`${k}.unknown`, { reason: d.reason ?? "?" });
    default:
      return "—";
  }
}
