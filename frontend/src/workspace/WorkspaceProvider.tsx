import { LoaderCircle, LogOut } from "lucide-react";
import { type ReactNode, useCallback, useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { Panel, ViewHead } from "../components";
import { LangSwitcher } from "../layout/LangSwitcher";
import { api, AUTH_UNAUTHORIZED_EVENT, type Workspace } from "../lib/api";
import { storedWorkspaceId, storeWorkspaceId } from "../lib/workspace-header";
import { WorkspaceContext } from "./workspace-context";

interface ListState {
  status: "loading" | "ready" | "error";
  workspaces: Workspace[];
  allWorkspaces: boolean;
}

const EMPTY: ListState = { status: "loading", workspaces: [], allWorkspaces: false };

/**
 * Which workspace a selection means. A stored id that no longer resolves
 * (deleted workspace, revoked grant) falls back the way the backend does: the
 * hub for an admin, the first grant for a member.
 */
function resolveCurrent(
  rows: Workspace[],
  stored: string | null,
  isAdmin: boolean,
): Workspace | null {
  if (rows.length === 0) return null;
  return (
    rows.find((row) => row.id === stored) ??
    (isAdmin ? rows.find((row) => row.is_default) : undefined) ??
    rows[0]
  );
}

/**
 * Owns the workspace selection for the whole console.
 *
 * Mounted inside `AuthGate` (so it only ever runs for an authenticated caller)
 * and inside the router (switching navigates). Children render only once the
 * list has resolved: the selection has to be in localStorage before any page's
 * mount effect fires, or that first request reaches the backend without a
 * workspace and a member with several grants meets `workspace.header_required`.
 */
export function WorkspaceProvider({ children }: { children: ReactNode }) {
  const { isAdmin } = useAuth();
  const location = useLocation();
  const navigate = useNavigate();
  const [list, setList] = useState<ListState>(EMPTY);
  const [selected, setSelected] = useState<string | null>(() => storedWorkspaceId());

  const refresh = useCallback(async () => {
    try {
      const result = await api.listWorkspaces();
      // Persisted here rather than in an effect: React runs a child's effects
      // before its parent's, so a page's very first fetch would otherwise leave
      // before the selection was stored — and an unstamped request is a 400 for
      // a member with several grants.
      const resolved = resolveCurrent(result.workspaces, storedWorkspaceId(), isAdmin);
      if (resolved) storeWorkspaceId(resolved.id);
      setSelected(resolved?.id ?? null);
      setList({
        status: "ready",
        workspaces: result.workspaces,
        allWorkspaces: result.all_workspaces,
      });
    } catch {
      // A switcher that cannot list is a missing chip, not a dead console: the
      // pages render and keep sending whatever selection is stored (an admin or
      // a single-grant member also has the backend's own fallback behind them).
      // The stored id is deliberately NOT cleared here — a transient failure
      // must not silently move an operator to another environment.
      setList((prev) => ({ ...prev, status: "error" }));
    }
  }, [isAdmin]);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  useEffect(() => {
    // AuthGate swaps children for the login page on a 401, which unmounts this
    // provider — the listener is here so a stale list can never outlive the
    // session if that order ever changes.
    const onUnauthorized = () => setList(EMPTY);
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  const current = useMemo(
    () => resolveCurrent(list.workspaces, selected, isAdmin),
    [isAdmin, list.workspaces, selected],
  );

  useEffect(() => {
    // Keeps storage and state agreeing after the list changes under us (the
    // selected workspace was deleted, a grant was revoked).
    if (!current) return;
    storeWorkspaceId(current.id);
    if (selected !== current.id) setSelected(current.id);
  }, [current, selected]);

  const select = useCallback(
    (id: string) => {
      if (id === current?.id) return;
      // Written before the navigation so the remounted subtree's first requests
      // already name the new workspace.
      storeWorkspaceId(id);
      setSelected(id);
      // Search params carry the old workspace's ids (?agent=, ?record=, ?exp=);
      // keeping them would deep-link every page at a foreign resource.
      navigate({ pathname: location.pathname, search: "" });
    },
    [current?.id, location.pathname, navigate],
  );

  const value = useMemo(
    () => ({
      workspaces: list.workspaces,
      current,
      allWorkspaces: list.allWorkspaces,
      select,
      refresh,
    }),
    [current, list.allWorkspaces, list.workspaces, refresh, select],
  );

  if (list.status === "loading") return <WorkspaceLoading />;
  if (list.status === "ready" && !isAdmin && list.workspaces.length === 0) {
    return <NoWorkspaceGranted />;
  }
  return <WorkspaceContext.Provider value={value}>{children}</WorkspaceContext.Provider>;
}

function WorkspaceLoading() {
  const { t } = useTranslation();
  return (
    <div className="auth-loading" role="status">
      <LoaderCircle size={22} strokeWidth={1.8} aria-hidden="true" />
      <span className="sr-only">{t("workspacesPage.resolving")}</span>
    </div>
  );
}

/**
 * A member whose account has no grant yet. Every workspace-scoped route would
 * answer 403, so the console says who can fix it instead of rendering pages that
 * all fail. Wearing the login page's chrome, because it is the same kind of
 * dead end: no navigation is useful, and signing out has to stay reachable.
 */
function NoWorkspaceGranted() {
  const { t } = useTranslation();
  const { authRequired, username, logout } = useAuth();
  return (
    <div className="auth-page">
      <header className="auth-topbar">
        <div className="brand">
          <span className="glyph" aria-hidden="true" />
          AGENTCORE<em>//</em>LAUNCHPAD
        </div>
        <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
          <LangSwitcher />
          {authRequired ? (
            <button
              type="button"
              className="logout-btn"
              onClick={() => void logout()}
              aria-label={t("auth.logout")}
              data-testid="no-workspace-logout"
            >
              <LogOut size={14} aria-hidden="true" />
              <span className="logout-label">{t("auth.logout")}</span>
            </button>
          ) : null}
        </div>
      </header>
      <main className="auth-main">
        <div className="no-workspace">
          <ViewHead
            kicker={t("workspacesPage.kicker")}
            title={t("workspacesPage.noneTitle")}
            meta={t("workspacesPage.noneMeta", { username: username ?? "—" })}
          />
          <Panel brk>
            <div className="empty" data-testid="no-workspace-body">
              {t("workspacesPage.noneBody")}
            </div>
          </Panel>
        </div>
      </main>
    </div>
  );
}
