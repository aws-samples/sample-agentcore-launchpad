import { ExternalLink, KeyRound } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ContextLayerAuthStatus } from "../../../lib/api";
import { safeAuthUrl } from "../../../lib/user-grants";
import { Button, Tag } from "../../ui";
import { AUTH_TONE } from "./state";
import { useAuthorize } from "./useAuthorize";

/** The member's own COA authorization: its state, and the button that starts
 *  (or renews) consent. Shared by the Context Layer page and the agent wizard. */
export function AuthorizationControl({
  status,
  onChanged,
  testId = "context-layer-auth",
}: {
  status: ContextLayerAuthStatus;
  /** re-read the status (consent completed in the other tab) */
  onChanged: () => void;
  testId?: string;
}) {
  const { t } = useTranslation();
  const { authorize, busy, waiting, pendingUrl } = useAuthorize(onChanged);
  const link = safeAuthUrl(pendingUrl);
  return (
    <div className="v2-row" data-testid={testId} data-status={status}>
      <Tag tone={AUTH_TONE[status]} dot={status === "pending"}>
        <span data-testid={`${testId}-status`}>{t(`v2.contextLayer.auth.${status}`)}</span>
      </Tag>
      {status !== "authorized" && (
        <Button size="sm" kind="primary" disabled={busy || waiting} onClick={() => void authorize()} testId={`${testId}-authorize`}>
          <KeyRound size={13} aria-hidden="true" />
          {t(status === "revoked" ? "v2.contextLayer.auth.reauthorize" : "v2.contextLayer.auth.authorize")}
        </Button>
      )}
      {waiting && <span className="v2-muted">{t("v2.contextLayer.auth.waiting")}</span>}
      {link && (
        <a className="v2-btn sm" href={link} target="_blank" rel="noopener noreferrer" data-testid={`${testId}-open`}>
          <ExternalLink size={13} aria-hidden="true" />
          {t("v2.contextLayer.auth.open")}
        </a>
      )}
    </div>
  );
}
