import { useTranslation } from "react-i18next";

import { api } from "../../../lib/api";
import { useLoad } from "../../hooks";
import { Tag } from "../../ui";

/** N17-obs465: the container agent's trace badge — the aws-cn exporter layer's start-up line
 *  and whether the newest session left spans (GET /api/agents/{id}/trace-status). */
export function TraceStatusTag({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const { data } = useLoad(() => api.agentTraceStatus(agentId), `trace-status:${agentId}`);
  if (!data) return <span className="v2-muted">—</span>;
  const tone = data.badge === "ok" ? "green" : data.badge === "unknown" ? "gray" : "red";
  const label = data.badge === "unavailable"
    ? t("v2.agents.traceStatus.unavailable", { reason: data.hook?.reason || data.hook?.state || "—" })
    : t(`v2.agents.traceStatus.${data.badge}`);
  return (
    <span data-testid="v2-agent-trace-status">
      <Tag tone={tone}>{label}</Tag>
      {data.cn_exporter ? (
        <span className="v2-muted"> · {t(`v2.agents.traceStatus.layer.${data.cn_exporter}`)}</span>
      ) : null}
    </span>
  );
}
