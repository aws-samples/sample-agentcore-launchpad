import { useTranslation } from "react-i18next";
import { NavLink } from "react-router-dom";

import { NAV_ENTRIES, PLATFORM_COUNT, type NavEntry } from "./nav";
import type { HealthInfo } from "./useHealth";

export function Sidebar({ health }: { health: HealthInfo | null }) {
  const { t } = useTranslation();

  const renderLink = (entry: NavEntry) => (
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
      <div className="label">{t("nav.phase02")}</div>
      <div className="nav-item dim">
        <span className="idx">10</span>
        {t("nav.payments")}
      </div>
      <div className="nav-item dim">
        <span className="idx">11</span>
        {t("nav.settings")}
      </div>
      <div className="sys">
        {t("sidebar.region")} <b>{health?.region ?? "—"}</b>
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
