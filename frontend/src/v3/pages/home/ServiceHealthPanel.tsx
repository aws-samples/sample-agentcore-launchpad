import { ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { OverviewInfo } from "../../../lib/api";
import { Lamp, Notice, Panel, Skeleton } from "../../ui";
import { serviceRows } from "./modules";

/** Service health (V2 Workbench): one row per AgentCore service, each opening its V3 page. */
export function ServiceHealthPanel({
  info,
  error,
  onRetry,
  activeAgents,
}: {
  info: OverviewInfo | null;
  error: string | null;
  onRetry: () => void;
  activeAgents: number | null;
}) {
  const { t } = useTranslation();
  return (
    <div data-testid="v3-home-health">
      <Panel title={t("v2.home.health.title")} end={<span>{t("v2.home.health.sub")}</span>}>
        {/* never answered: every row would otherwise claim "not created yet" */}
        {info === null && error ? (
          <Notice s="act">{error}{" "}
            <button type="button" className="v3-btn sm" onClick={onRetry}>{t("v3.ux.retry")}</button>
          </Notice>
        ) : info === null ? (
          <Skeleton rows={5} />
        ) : (
          <ul className="v3-home-health">
            {serviceRows(info, activeAgents).map((row) => {
              const text =
                row.id === "runtime" && row.state === "ready"
                  ? t("v2.home.health.activeCount", { count: activeAgents ?? 0 })
                  : row.state === "ready"
                    ? t("v2.home.health.ready")
                    : row.state === "idle"
                      ? t(`v2.home.health.creates.${row.id}`)
                      : t("v2.home.health.pending");
              return (
                <li key={row.id} data-testid={`v3-health-${row.id}`}>
                  <Link to={row.to}>
                    <Lamp s={row.signal} />
                    <span className="name">{t(`overview.health.${row.id}`)}</span>
                    <span className="state" data-s={row.signal} title={row.detail || undefined}>{text}</span>
                    <ChevronRight size={14} aria-hidden="true" />
                  </Link>
                </li>
              );
            })}
          </ul>
        )}
      </Panel>
    </div>
  );
}
