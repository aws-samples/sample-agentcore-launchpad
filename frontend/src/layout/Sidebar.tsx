import { useTranslation } from "react-i18next";
import { NavLink } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { useWorkspace } from "../workspace/workspace-context";
import { ADMIN_NAV_ENTRIES, NAV_ENTRIES, PLATFORM_COUNT, type NavEntry } from "./nav";
import type { HealthInfo } from "./useHealth";

export function Sidebar({ health }: { health: HealthInfo | null }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();

  const renderLink = (entry: NavEntry) =>
    entry.adminOnly && !isAdmin ? (
      // Keep the slot so the numbered flow does not shift for members, but do not
      // offer a page whose every action would answer 403.
      <div className="nav-item dim" key={entry.to} title={t("auth.adminRequired.meta")}>
        <span className="idx">{entry.idx}</span>
        {t(entry.labelKey)}
      </div>
    ) : (
      <NavLink
        key={entry.to}
        to={entry.to}
        end={entry.end}
        className={({ isActive }) => `nav-item${isActive ? " active" : ""}`}
      >
        <span className="idx">{entry.idx}</span>
        {t(entry.labelKey)}
      </NavLink>
    );

  return (
    <nav className="side">
      <div className="label">{t("nav.platform")}</div>
      {NAV_ENTRIES.slice(0, PLATFORM_COUNT).map(renderLink)}
      <div className="label">{t("nav.operate")}</div>
      {NAV_ENTRIES.slice(PLATFORM_COUNT).map(renderLink)}
      {isAdmin ? (
        <>
          <div className="label">{t("nav.administration")}</div>
          {ADMIN_NAV_ENTRIES.map(renderLink)}
        </>
      ) : null}
      <div className="label">{t("nav.phase02")}</div>
      {/* 11 belongs to Workspaces since 2026-08-12; the placeholders moved up */}
      <div className="nav-item dim">
        <span className="idx">12</span>
        {t("nav.payments")}
      </div>
      <div className="nav-item dim">
        <span className="idx">13</span>
        {t("nav.settings")}
      </div>
      <div className="sys" data-testid="sidebar-region">
        {t("sidebar.region")} <b>{current?.region ?? health?.region ?? "—"}</b>
        <br />
        {t("sidebar.sdk")} <b>bedrock-agentcore 1.17.0</b>
        <br />
        {t("sidebar.cli")} <b>agentcore 0.21.1</b>
        <br />
        {t("sidebar.store")} <b>{t("sidebar.storeValue")}</b>
      </div>
    </nav>
  );
}
