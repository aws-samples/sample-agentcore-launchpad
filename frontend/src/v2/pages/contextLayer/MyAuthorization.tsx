import { ExternalLink, KeyRound, UserRound } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { api, type ContextLayerIdentity, type ContextLayerStatus, errorMessage } from "../../../lib/api";
import { safeAuthUrl } from "../../../lib/user-grants";
import { fmtTime } from "../../format";
import { useV2Toast } from "../../hooks";
import { Button, Tag } from "../../ui";
import { DemoCredentials, SignOutLink, SwitchGuide } from "./DemoAccount";
import { accountView, AUTH_TONE, authStatusOf, choiceSettled, readAccountIntent, writeAccountIntent } from "./state";
import { useAuthorize } from "./useAuthorize";

/**
 * The overview's 我的授权 card body: the member's own COA authorization, and which
 * COA account it is. With a demo account on the layer, a member who is not
 * authorized chooses between their own COA account and the shared demo one; an
 * authorization as the demo account is tagged and can be switched; a consent that
 * resolved to the other account than the one chosen (a COA browser session was
 * still signed in) opens the revoke → sign out → authorize again guide.
 */
export function MyAuthorization({ status, onChanged }: { status: ContextLayerStatus; onChanged: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const auth = authStatusOf(status);
  const identity = status.authorization?.identity ?? null;
  const demo = status.demo_account?.available ? status.demo_account : null;
  const [intent, setIntent] = useState(readAccountIntent);
  const [guide, setGuide] = useState<ContextLayerIdentity | null>(null);
  const [showDemo, setShowDemo] = useState(false);
  const [revoking, setRevoking] = useState(false);
  const { authorize, busy, waiting, pendingUrl } = useAuthorize(onChanged);
  const link = safeAuthUrl(pendingUrl);
  const view = accountView({ auth, identity, intent, guide });

  const remember = (value: ContextLayerIdentity | null) => {
    writeAccountIntent(value);
    setIntent(value);
  };
  useEffect(() => {
    if (choiceSettled(intent, auth, identity)) {
      writeAccountIntent(null);
      setIntent(null);
    }
    if (choiceSettled(guide, auth, identity)) setGuide(null);
  }, [auth, identity, intent, guide]);
  useEffect(() => {
    if (view.kind !== "choose") setShowDemo(false);
  }, [view.kind]);

  /** Start a consent for an account. `choice` is what the request names ("demo" is
   *  refused without a demo account; the consent itself is the same); the own button
   *  of the plain choice stays the ordinary authorize. */
  const start = (account: ContextLayerIdentity, choice: ContextLayerIdentity | undefined) => {
    remember(account);
    if (account === "demo") setShowDemo(true);
    void authorize(choice);
  };
  const revoke = async (target: ContextLayerIdentity | null) => {
    // the guide stays open through the revoke (the card reads "revoked" next)
    if (target) setGuide(target);
    else remember(null);
    setRevoking(true);
    try {
      await api.revokeContextLayer();
      toast("success", t("v2.contextLayer.auth.revokedToast"));
      onChanged();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setRevoking(false);
    }
  };

  return (
    <>
      <div className="v2-row" data-testid="context-layer-auth" data-status={auth}>
        <Tag tone={AUTH_TONE[auth]} dot={auth === "pending"}>
          <span data-testid="context-layer-auth-status">{t(`v2.contextLayer.auth.${auth}`)}</span>
        </Tag>
        {auth === "authorized" && identity === "demo" && (
          <Tag tone="blue" testId="context-layer-identity-demo">
            <UserRound size={12} aria-hidden="true" />
            {t("v2.contextLayer.demo.tag")}
          </Tag>
        )}
        {view.kind === "choose" && (
          <>
            <Button size="sm" kind="primary" disabled={busy || waiting} onClick={() => start("own", undefined)} testId="context-layer-auth-authorize">
              <KeyRound size={13} aria-hidden="true" />
              {t(demo ? "v2.contextLayer.demo.useOwn" : auth === "revoked" ? "v2.contextLayer.auth.reauthorize" : "v2.contextLayer.auth.authorize")}
            </Button>
            {demo && (
              <Button size="sm" disabled={busy || waiting} onClick={() => start("demo", "demo")} testId="context-layer-auth-demo">
                <UserRound size={13} aria-hidden="true" />
                {t("v2.contextLayer.demo.useDemo")}
              </Button>
            )}
          </>
        )}
        {view.kind === "demo" && (
          <Button size="sm" onClick={() => setGuide("own")} testId="context-layer-switch-own">
            {t("v2.contextLayer.demo.switchToOwn")}
          </Button>
        )}
        {auth === "authorized" && view.kind !== "guide" && (
          <Button size="sm" kind="danger" disabled={revoking} onClick={() => void revoke(null)} testId="context-layer-revoke">
            {t("v2.contextLayer.auth.revoke")}
          </Button>
        )}
        {waiting && <span className="v2-muted">{t("v2.contextLayer.auth.waiting")}</span>}
        {link && (
          <a className="v2-btn sm" href={link} target="_blank" rel="noopener noreferrer" data-testid="context-layer-auth-open">
            <ExternalLink size={13} aria-hidden="true" />
            {t("v2.contextLayer.auth.open")}
          </a>
        )}
        <Link to="/v2/my-connections" className="v2-link">
          {t("v2.contextLayer.auth.myConnections")}
        </Link>
      </div>
      {view.kind === "choose" && demo && showDemo && <DemoCredentials username={demo.username} />}
      {view.kind === "choose" && demo && status.sign_out_url && (
        <p className="v2-muted v2-cl-hint v2-row" data-testid="context-layer-other-session">
          {t("v2.contextLayer.demo.otherSession")}
          <SignOutLink signOutUrl={status.sign_out_url} consoleUrl={status.console_url} />
        </p>
      )}
      {view.kind === "guide" && (
        <SwitchGuide
          view={view}
          demoUsername={demo?.username ?? null}
          signOutUrl={status.sign_out_url}
          consoleUrl={status.console_url}
          busy={busy || waiting || revoking}
          onRevoke={() => void revoke(view.target)}
          onAuthorize={() => start(view.target, view.target)}
          onCancel={() => {
            setGuide(null);
            remember(null);
          }}
        />
      )}
      {status.authorization?.authorized_at && (
        <p className="v2-muted">{t("v2.contextLayer.auth.since", { at: fmtTime(status.authorization.authorized_at) })}</p>
      )}
    </>
  );
}
