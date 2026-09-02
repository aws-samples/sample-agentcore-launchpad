import { Activity, Database, FlaskConical, ListChecks, Radar } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

const ITEMS = [
  { view: "", key: "runs", icon: Activity },
  { view: "experiment", key: "experiments", icon: FlaskConical },
  { view: "online", key: "online", icon: Radar },
  { view: "datasets", key: "datasets", icon: Database },
  { view: "evaluators", key: "evaluators", icon: ListChecks },
] as const;

export function EvaluationNav() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const currentView = searchParams.get("view") ?? "";
  const activeView = currentView === "new" ? "" : currentView;

  return (
    <nav className="evaluation-nav" aria-label={t("evalPage.nav.label")}>
      {ITEMS.map(({ view, key, icon: Icon }) => {
        const active = activeView === view;
        return (
          <Link
            key={key}
            to={view ? `/evaluation?view=${view}` : "/evaluation"}
            className={`evaluation-nav-item${active ? " active" : ""}`}
            aria-current={active ? "page" : undefined}
          >
            <Icon size={14} />
            <span>{t(`evalPage.nav.${key}`)}</span>
          </Link>
        );
      })}
    </nav>
  );
}
