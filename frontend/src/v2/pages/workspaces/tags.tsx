import { useTranslation } from "react-i18next";

import type { WorkspaceBootstrapStatus } from "../../../lib/api";
import { Tag } from "../../ui";
import { STATUS_TONE } from "./status";

export function StatusTag({ status }: { status: WorkspaceBootstrapStatus }) {
  const { t } = useTranslation();
  return (
    <Tag tone={STATUS_TONE[status]} dot>
      {t(`v2.workspaces.status.${status}`)}
    </Tag>
  );
}

export function HubTag() {
  const { t } = useTranslation();
  return <Tag tone="outline">{t("v2.workspaces.hub")}</Tag>;
}

export function ExternalTag() {
  const { t } = useTranslation();
  return <Tag tone="blue">{t("v2.workspaces.external")}</Tag>;
}
