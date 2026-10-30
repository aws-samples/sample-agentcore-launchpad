import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Card } from "../ui";

/** The catch-all of the console: an unknown URL (typo, stale bookmark, a retired
 *  classic path with no V2 counterpart) and a module this deployment does not expose. */
export function V2NotFound() {
  const { t } = useTranslation();
  return (
    <Card>
      <div className="v2-table-empty">
        <h2 style={{ fontSize: 18, marginBottom: 8, color: "var(--v2-ink)" }}>{t("v2.notFound.title")}</h2>
        <p style={{ marginBottom: 16 }}>{t("v2.notFound.desc")}</p>
        <Link to="/v2" className="v2-btn primary">
          {t("v2.notFound.home")}
        </Link>
      </div>
    </Card>
  );
}
