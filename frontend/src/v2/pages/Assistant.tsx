import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { useAuth } from "../../auth/auth-context";
import { api } from "../../lib/api";
import { useWorkspace } from "../../workspace/workspace-context";
import { useLoad } from "../hooks";
import { Alert, Button, Card, PageHeader, Spin } from "../ui";
import { AssistantDetail } from "./assistant/AssistantDetail";
import { AssistantList } from "./assistant/AssistantList";
import "./assistant/assistant.css";

/**
 * AI 创建助手 (native V2): the conversation history list, and
 * `?view=detail&id=<conversation>` — the discussion → preparation → proposal →
 * deploy → evaluation-assets → next-steps workspace of one conversation. Both need
 * the protected architect preset to be active in this workspace.
 */
export function V2Assistant() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const workspaceId = current?.id ?? null;
  const status = useLoad(() => api.assistantStatus(), `assistant-status:${workspaceId}`);
  const view = params.get("view");
  const id = params.get("id");

  if (workspaceId === null || (!status.data && status.loading)) return <Spin />;
  if (!status.data) {
    return (
      <>
        <PageHeader title={t("nav.assistant")} />
        <Alert tone="error" action={<button type="button" className="v2-link" onClick={status.reload}>{t("v2.common.retry")}</button>}>
          {status.error}
        </Alert>
      </>
    );
  }
  const s = status.data;
  if (!s.available) {
    return (
      <>
        <PageHeader title={t("nav.assistant")} desc={t("assistantPage.description")} />
        <Card title={t("assistantPage.unavailableTitle")} testId="v2-assistant-unavailable">
          <Alert tone="warn">
            {t("assistantPage.unavailableBody", {
              label: s.preset.label,
              status: t(`create.system.status.${s.preset.status}`),
            })}{" "}
            {isAdmin ? t("assistantPage.unavailableAdmin") : t("assistantPage.unavailableMember")}
            {s.preset.requirements.length > 0 && (
              <div style={{ marginTop: 4 }}>
                {t("assistantPage.requirements", {
                  list: s.preset.requirements
                    .map((r) => t(`create.system.requirementCodes.${r.code}`, r.message))
                    .join("; "),
                })}
              </div>
            )}
          </Alert>
          <div className="v2-row">
            <Button kind={isAdmin ? "primary" : undefined} onClick={() => navigate("/agents/new")}>
              {t("assistantPage.goToPresets")}
            </Button>
            <Button onClick={status.reload}>{t("v2.common.refresh")}</Button>
          </div>
        </Card>
      </>
    );
  }
  if (view === "detail" && id) {
    return (
      <AssistantDetail
        key={`${workspaceId}:${id}`}
        id={id}
        status={s}
        workspaceId={workspaceId}
        onUnavailable={status.reload}
      />
    );
  }
  return <AssistantList status={s} workspaceId={workspaceId} onUnavailable={status.reload} />;
}
