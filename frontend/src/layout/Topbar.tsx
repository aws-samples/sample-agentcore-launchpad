import { LogOut, ShieldOff } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/auth-context";
import { LAB_GUIDE_URL } from "../lib/links";
import { useWorkspace } from "../workspace/workspace-context";
import type { HealthInfo, HealthStatus } from "./useHealth";
import { LangSwitcher } from "./LangSwitcher";
import { WorkspaceSwitcher } from "./WorkspaceSwitcher";

interface TopbarProps {
  crumbKey: string;
  health: HealthInfo | null;
  healthStatus: HealthStatus;
}

export function Topbar({ crumbKey, health, healthStatus }: TopbarProps) {
  const { t } = useTranslation();
  const { authRequired, username, role, accountExpiresAt, logout } = useAuth();
  const { current } = useWorkspace();
  const displayName = authRequired ? (username ?? "—") : "river";
  const initials = displayName.slice(0, 2).toUpperCase();
  const roleLabel = authRequired
    ? t(role === "member" ? "auth.roleMember" : "auth.roleAdmin")
    : "PLATFORM-ADMIN";
  // registered accounts are time-boxed; surface what is left of the validity
  const daysLeft = accountExpiresAt
    ? Math.max(
        0,
        Math.floor((new Date(accountExpiresAt).getTime() - Date.now()) / 86_400_000),
      )
    : null;
  return (
    <div className="topbar">
      <div className="brand">
        <span className="glyph">▲</span>AGENTCORE<em>//</em>LAUNCHPAD
      </div>
      <div className="crumb">
        {t("topbar.console")} / <b>{t(crumbKey).toUpperCase()}</b>
      </div>
      <div className="right">
        {/* Bound to the /api/health probe: green only while the last probe
            answered 2xx; "loading" and "down" both read as unreachable so a
            dead backend never shows a green chip. Same box either way. */}
        {healthStatus === "ok" ? (
          <div className="syschip" data-testid="topbar-health" data-status="ok">
            <span className="led"></span>
            {t("topbar.allSystemsGo")}
          </div>
        ) : (
          <div className="syschip down" data-testid="topbar-health" data-status={healthStatus}>
            <span className="led crit"></span>
            {t("topbar.backendDown")}
          </div>
        )}
        <WorkspaceSwitcher />
        {/* The workspace owns the environment; health only backs the chips up
            while the workspace list is unavailable. */}
        <div className="syschip" data-testid="topbar-region">
          {current?.region ?? health?.region ?? "—"}
        </div>
        <div className="syschip" data-testid="topbar-account">
          {t("topbar.acct")} {current?.account_id || health?.account_id || "—"}
        </div>
        <a
          className="syschip link"
          href={LAB_GUIDE_URL}
          target="_blank"
          rel="noreferrer"
          title={t("topbar.labGuideTitle")}
          data-testid="lab-guide-topbar"
        >
          ⧉ {t("topbar.labGuide")} ↗
        </a>
        <LangSwitcher />
        <div className="avatar">
          <div className="pic">{initials}</div>
          <span>
            {displayName} · <b className="role">{roleLabel}</b>
            {daysLeft !== null ? (
              <>
                {" · "}
                <em className="validity" data-testid="account-days-left">
                  {t("auth.daysLeft", { days: daysLeft })}
                </em>
              </>
            ) : null}
          </span>
          {authRequired ? (
            <button
              type="button"
              className="logout-btn"
              onClick={() => void logout()}
              aria-label={t("auth.logout")}
              title={t("auth.logout")}
              data-testid="logout-button"
            >
              <LogOut size={14} aria-hidden="true" />
              {/* label collapses under 720px, leaving the icon as the tap target */}
              <span className="logout-label">{t("auth.logout")}</span>
            </button>
          ) : (
            // No gate configured ⇒ no session to end. Say so instead of leaving
            // an empty slot that reads as a missing sign-out button.
            <span
              className="logout-btn auth-off"
              title={t("auth.gateOffHint")}
              data-testid="auth-off-badge"
            >
              <ShieldOff size={14} aria-hidden="true" />
              <span className="logout-label">{t("auth.gateOff")}</span>
            </span>
          )}
        </div>
      </div>
    </div>
  );
}
