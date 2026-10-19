import { ExternalLink } from "lucide-react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import type { ContextLayerStatus } from "../../../lib/api";
import type { Signal } from "../../signals";
import { Alert, Card, Lamp, LinkButton, Spin } from "../../ui";
import { AuthorizationControl } from "./AuthorizationControl";
import { authStatusOf } from "./state";
import type { CoaError } from "./useCoaLoad";
import { coaErrorKind, type ModuleGate } from "./views";

/** A link into the COA console (new tab: COA forbids framing); nothing without a URL. */
export function ConsoleLink({ href, label, testId }: { href: string | null; label?: string; testId?: string }) {
  const { t } = useTranslation();
  if (!href) return null;
  return (
    <a className="v2-btn sm" href={href} target="_blank" rel="noopener noreferrer" data-testid={testId}>
      <ExternalLink size={13} aria-hidden="true" />
      {label ?? t("v2.contextLayer.openInCoa")}
    </a>
  );
}

/** A COA status, verbatim, with its signal lamp. */
export function StatusLamp({ signal, status, testId }: { signal: Signal; status: string | null | undefined; testId?: string }) {
  return (
    <span className="v2-row" data-signal={signal} data-testid={testId}>
      <Lamp signal={signal} />
      <span className="mono">{status || "—"}</span>
    </span>
  );
}

/**
 * A failed COA read, presented by what the member can do about it: authorize (or
 * authorize again after COA rejected the token), ask a COA admin for a grant, or
 * retry. `onAuthorized` re-reads once the consent tab finished.
 */
export function CoaErrorNotice({
  error,
  onAuthorized,
  onRetry,
  consoleUrl,
  testId = "context-layer-error",
}: {
  error: CoaError;
  onAuthorized: () => void;
  onRetry?: () => void;
  consoleUrl?: string | null;
  testId?: string;
}) {
  const { t } = useTranslation();
  const kind = coaErrorKind(error.code);
  return (
    <div data-testid={testId} data-kind={kind}>
      {kind === "authorize" || kind === "reauthorize" ? (
        <Alert tone="warn" action={<AuthorizationControl status={kind === "reauthorize" ? "revoked" : "none"} onChanged={onAuthorized} />}>
          {t(kind === "reauthorize" ? "v2.contextLayer.errors.reauthorize" : "v2.contextLayer.errors.authorize")}
        </Alert>
      ) : kind === "forbidden" ? (
        <Alert tone="warn" action={<ConsoleLink href={consoleUrl ?? null} label={t("v2.contextLayer.openConsole")} />}>
          {t("v2.contextLayer.errors.forbidden")}
        </Alert>
      ) : kind === "notFound" ? (
        <Alert tone="warn">{t("v2.contextLayer.errors.notFound")}</Alert>
      ) : (
        <Alert tone="error" action={onRetry ? <LinkButton onClick={onRetry}>{t("v2.common.retry")}</LinkButton> : undefined}>
          {error.message}
        </Alert>
      )}
    </div>
  );
}

/**
 * What a sub-page shows instead of its content while it cannot read COA yet:
 * loading, a failed status read, no connected layer, or the member's consent.
 */
export function GateNotice({
  gate,
  status,
  error,
  onChanged,
  onOverview,
}: {
  gate: Exclude<ModuleGate, "ready">;
  status: ContextLayerStatus | null;
  error: string | null;
  onChanged: () => void;
  onOverview: () => void;
}) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  if (gate === "loading") return <Spin />;
  if (gate === "error") return <Alert tone="error">{error}</Alert>;
  if (gate === "notConnected") {
    return (
      <Alert
        tone="warn"
        action={isAdmin ? <LinkButton onClick={onOverview}>{t("v2.contextLayer.connect.submit")}</LinkButton> : undefined}
      >
        <span data-testid="context-layer-gate-not-connected">{t(isAdmin ? "v2.contextLayer.gate.connectFirst" : "v2.contextLayer.askAdmin")}</span>
      </Alert>
    );
  }
  return (
    <Card title={t("v2.contextLayer.myAuth")} sub={t("v2.contextLayer.myAuthSub")} testId="context-layer-gate-auth">
      <p className="v2-muted">{t("v2.contextLayer.gate.authorizeFirst")}</p>
      <AuthorizationControl status={authStatusOf(status)} onChanged={onChanged} />
    </Card>
  );
}
