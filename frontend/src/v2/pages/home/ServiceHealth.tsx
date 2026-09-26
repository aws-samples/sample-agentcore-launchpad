import { ChevronRight } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import type { OverviewInfo } from "../../../lib/api";
import { Alert, Card, Spin } from "../../ui";
import { SERVICES } from "./common";

/** 平台服务状态 — the classic Overview's service-health panel, one row per AgentCore service. */
export function ServiceHealth({
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
    <Card title={t("v2.home.health.title")} sub={t("v2.home.health.sub")} testId="v2-home-health">
      {/* never answered: every row would otherwise claim "not created yet" */}
      {info === null && error ? (
        <Alert tone="error">
          {error}{" "}
          <button type="button" className="v2-link" onClick={onRetry}>
            {t("v2.common.retry")}
          </button>
        </Alert>
      ) : info === null ? (
        <Spin />
      ) : (
        <ul className="v2-home-health">
          {SERVICES.map(({ id, kind, to }) => {
            const ready = id === "runtime" ? (activeAgents ?? 0) > 0 : Boolean(info.services[id]);
            const detail = id === "runtime" ? "" : (info.service_detail[id] ?? "");
            const state = ready ? "ok" : kind === "usage" ? "idle" : "warn";
            const text =
              id === "runtime" && ready
                ? t("v2.home.health.activeCount", { count: activeAgents ?? 0 })
                : ready
                  ? t("v2.home.health.ready")
                  : kind === "usage"
                    ? t(`v2.home.health.creates.${id}`)
                    : t("v2.home.health.pending");
            return (
              <li key={id} data-testid={`v2-health-${id}`}>
                <Link to={to}>
                  <span className={`dot ${state}`} aria-hidden="true" />
                  <span className="name">{t(`overview.health.${id}`)}</span>
                  <span className={`state ${state}`} title={detail || undefined}>
                    {text}
                  </span>
                  <ChevronRight size={14} aria-hidden="true" />
                </Link>
              </li>
            );
          })}
        </ul>
      )}
    </Card>
  );
}
