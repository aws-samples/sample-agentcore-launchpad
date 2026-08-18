import { Dumbbell, FlaskConical, ListChecks } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

const ITEMS = [
  { view: "", key: "tasksets", icon: ListChecks },
  { view: "eval", key: "eval", icon: FlaskConical },
  { view: "train", key: "train", icon: Dumbbell },
] as const;

/** Sub-page switcher for the Skill Lab `?view=` surfaces (no view = task sets). */
export function SkillLabNav() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const currentView = searchParams.get("view") ?? "";
  const activeView = currentView === "tasksets" ? "" : currentView;

  return (
    <nav className="evaluation-nav" aria-label={t("skillLab.nav.label")}>
      {ITEMS.map(({ view, key, icon: Icon }) => {
        const active = activeView === view;
        return (
          <Link
            key={key}
            to={view ? `/skill-lab?view=${view}` : "/skill-lab"}
            className={`evaluation-nav-item${active ? " active" : ""}`}
            aria-current={active ? "page" : undefined}
            data-testid={`skill-lab-nav-${key}`}
          >
            <Icon size={14} />
            <span>{t(`skillLab.nav.${key}`)}</span>
          </Link>
        );
      })}
    </nav>
  );
}
