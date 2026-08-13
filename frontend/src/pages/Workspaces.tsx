import { type CSSProperties, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import {
  AdminRequired,
  Btn,
  Chip,
  type Column,
  DataTable,
  Pager,
  Panel,
  useTablePage,
  useToast,
  ViewHead,
} from "../components";
import { api, type Workspace } from "../lib/api";
import { useWorkspace } from "../workspace/workspace-context";
import { CreateWorkspaceView } from "./workspaces/CreateView";
import { WorkspaceDetailView } from "./workspaces/DetailView";
import { STATUS_TONE } from "./workspaces/status";

function fmt(value: string | null): string {
  return value ? new Date(value).toLocaleString() : "—";
}

/**
 * Admin surface for the environments the console can target: register a
 * workspace, drive its bootstrap job, grant it to members, detach it.
 *
 * `?view=create` and `?view=detail&ws=<id>` are sub-pages of this one route,
 * following the console's `?view=` convention.
 */
export function Workspaces() {
  const { t } = useTranslation();
  const toast = useToast();
  const { isAdmin } = useAuth();
  const { refresh: refreshSwitcher } = useWorkspace();
  const [params, setParams] = useSearchParams();
  const view = params.get("view");
  const selectedId = params.get("ws") ?? "";

  const [rows, setRows] = useState<Workspace[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const seq = useRef(0);

  const load = useCallback(async () => {
    if (!isAdmin) return;
    const id = ++seq.current;
    setLoading(true);
    try {
      const result = await api.listWorkspaces();
      if (id !== seq.current) return; // ignore out-of-order responses
      setRows(result.workspaces);
      setError(null);
    } catch (err) {
      if (id !== seq.current) return;
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      if (id === seq.current) setLoading(false);
    }
  }, [isAdmin]);

  useEffect(() => {
    void load();
  }, [load]);

  /** The switcher's list has to follow every register / delete / bootstrap. */
  const reload = useCallback(async () => {
    await load();
    await refreshSwitcher();
  }, [load, refreshSwitcher]);

  const open = (id: string) =>
    setParams({ view: "detail", ws: id }, { replace: false });
  const backToList = () => setParams({}, { replace: true });

  const selectedIndex = rows.findIndex((row) => row.id === selectedId);
  const { rows: visible, pagerProps } = useTablePage(rows, selectedIndex);

  if (!isAdmin) {
    return (
      <AdminRequired
        kicker={t("workspacesPage.kicker")}
        title={t("workspacesPage.title")}
        body={t("workspacesPage.forbiddenBody")}
        testId="workspaces-forbidden-body"
      />
    );
  }

  if (view === "create") {
    return (
      <CreateWorkspaceView
        hubAccountId={rows.find((row) => row.is_default)?.account_id ?? ""}
        takenRegions={rows.map((row) => row.region)}
        onBack={backToList}
        onDone={async (created) => {
          await reload();
          toast(t("workspacesPage.created", { name: created.name }), "good");
          setParams({ view: "detail", ws: created.id }, { replace: true });
        }}
      />
    );
  }

  if (view === "detail") {
    return (
      <WorkspaceDetailView
        // Keyed on the selection: the view remembers which bootstrap job it
        // watches, so a `?ws=` change has to reset it rather than keep polling
        // the previous workspace's job (which the scoped route 404s).
        key={selectedId}
        workspaceId={selectedId}
        onBack={backToList}
        onChanged={reload}
      />
    );
  }

  const columns: Column[] = [
    { key: "id", label: t("workspacesPage.cols.id") },
    { key: "name", label: t("workspacesPage.cols.name") },
    { key: "account", label: t("workspacesPage.cols.account") },
    { key: "region", label: t("workspacesPage.cols.region") },
    { key: "status", label: t("workspacesPage.cols.status") },
    { key: "created", label: t("workspacesPage.cols.created") },
  ];

  return (
    <>
      <ViewHead
        kicker={t("workspacesPage.kicker")}
        title={t("workspacesPage.title")}
        meta={t("workspacesPage.meta")}
      />

      <Panel
        brk
        pad={false}
        title={t("workspacesPage.listTitle")}
        sub={t("workspacesPage.listSub", { count: rows.length })}
        end={
          <Btn
            primary
            onClick={() => setParams({ view: "create" })}
            data-testid="new-workspace-btn"
          >
            + {t("workspacesPage.new")}
          </Btn>
        }
        style={{ "--i": 0 } as CSSProperties}
      >
        <DataTable
          columns={columns}
          isEmpty={!loading && rows.length === 0}
          empty={error ?? t("workspacesPage.empty")}
        >
          {visible.map((row) => (
            <tr
              key={row.id}
              onClick={() => open(row.id)}
              style={{ cursor: "pointer" }}
              data-testid={`workspace-row-${row.id}`}
            >
              <td>
                <b>{row.id}</b>
                {row.is_default ? (
                  <>
                    {" "}
                    <Chip tone="muted">{t("workspacesPage.hub")}</Chip>
                  </>
                ) : null}
              </td>
              <td>{row.name}</td>
              <td className="mono">
                {row.account_id}
                {row.cross_account ? (
                  <>
                    {" "}
                    <Chip tone="blue">{t("workspacesPage.external")}</Chip>
                  </>
                ) : null}
              </td>
              <td className="mono">{row.region}</td>
              <td>
                <Chip tone={STATUS_TONE[row.bootstrap_status]}>
                  {t(`workspacesPage.status.${row.bootstrap_status}`)}
                </Chip>
              </td>
              <td>{fmt(row.created_at)}</td>
            </tr>
          ))}
        </DataTable>
        <Pager {...pagerProps} />
      </Panel>
    </>
  );
}
