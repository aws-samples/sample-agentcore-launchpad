import { Check, ChevronDown, Settings2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import type { WorkspaceBootstrapStatus } from "../lib/api";
import { useWorkspace } from "../workspace/workspace-context";

/** Only `ready` accepts mutating traffic — the menu says so per row. */
const STATUS_CLASS: Record<WorkspaceBootstrapStatus, string> = {
  ready: "ok",
  bootstrapping: "wait",
  registered: "wait",
  failed: "bad",
};

/**
 * The environment selector. Members see their grants, administrators every
 * workspace plus a way into the management page.
 */
export function WorkspaceSwitcher() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { workspaces, current, select } = useWorkspace();
  const [open, setOpen] = useState(false);
  const box = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    const onAway = (event: MouseEvent) => {
      if (!box.current?.contains(event.target as Node)) setOpen(false);
    };
    const onEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onAway);
    document.addEventListener("keydown", onEscape);
    return () => {
      document.removeEventListener("mousedown", onAway);
      document.removeEventListener("keydown", onEscape);
    };
  }, [open]);

  if (!current) return null;

  return (
    <div className="wsswitch" ref={box} data-testid="workspace-switcher">
      <button
        type="button"
        className="wsswitch-btn"
        aria-haspopup="menu"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
        title={t("topbar.workspaceTitle")}
        data-testid="workspace-switcher-btn"
      >
        <span className={`wsdot ${STATUS_CLASS[current.bootstrap_status]}`} aria-hidden="true" />
        <span className="wsname">{current.name}</span>
        <ChevronDown size={12} aria-hidden="true" />
      </button>
      {open ? (
        <div className="wsmenu" role="menu" data-testid="workspace-menu">
          <div className="wsmenu-label">{t("topbar.workspaceLabel")}</div>
          {workspaces.map((row) => (
            <button
              key={row.id}
              type="button"
              role="menuitem"
              className={`wsmenu-item${row.id === current.id ? " on" : ""}`}
              onClick={() => {
                setOpen(false);
                select(row.id);
              }}
              data-testid={`workspace-option-${row.id}`}
            >
              <span
                className={`wsdot ${STATUS_CLASS[row.bootstrap_status]}`}
                aria-hidden="true"
              />
              <span className="wsmenu-name">
                {row.name}
                <em>
                  {row.region} · {t(`workspacesPage.status.${row.bootstrap_status}`)}
                </em>
              </span>
              {row.id === current.id ? <Check size={12} aria-hidden="true" /> : null}
            </button>
          ))}
          {isAdmin ? (
            <Link
              className="wsmenu-manage"
              to="/workspaces"
              onClick={() => setOpen(false)}
              data-testid="workspace-manage-link"
            >
              <Settings2 size={12} aria-hidden="true" />
              {t("topbar.workspaceManage")}
            </Link>
          ) : null}
        </div>
      ) : null}
    </div>
  );
}
