import { LogOut } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../auth/auth-context";
import { LAB_GUIDE_URL } from "../lib/links";
import type { HealthInfo } from "./useHealth";
import { LangSwitcher } from "./LangSwitcher";

interface TopbarProps {
  crumbKey: string;
  health: HealthInfo | null;
}

export function Topbar({ crumbKey, health }: TopbarProps) {
  const { t } = useTranslation();
  const { authRequired, username, logout } = useAuth();
  const displayName = authRequired ? (username ?? "—") : "river";
  const initials = displayName.slice(0, 2).toUpperCase();
  return (
    <div className="topbar">
      <div className="brand">
        <span className="glyph">▲</span>AGENTCORE<em>//</em>LAUNCHPAD
      </div>
      <div className="crumb">
        {t("topbar.console")} / <b>{t(crumbKey).toUpperCase()}</b>
      </div>
      <div className="right">
        <div className="syschip">
          <span className="led"></span>
          {t("topbar.allSystemsGo")}
        </div>
        <div className="syschip">{health?.region ?? "—"}</div>
        <div className="syschip">
          {t("topbar.acct")} {health?.account_id || "—"}
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
            {displayName} ·{" "}
            <b className="role">
              {authRequired ? t("auth.operator") : "PLATFORM-ADMIN"}
            </b>
          </span>
          {authRequired ? (
            <button
              type="button"
              className="logout-btn"
              onClick={() => void logout()}
              aria-label={t("auth.logout")}
              title={t("auth.logout")}
            >
              <LogOut size={14} aria-hidden="true" />
            </button>
          ) : null}
        </div>
      </div>
    </div>
  );
}
