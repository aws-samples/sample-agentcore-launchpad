import { CheckCircle2, Copy, ExternalLink, Eye, EyeOff, LogOut } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, type ContextLayerDemoCredentials, type ContextLayerDemoInfo, errorMessage } from "../../../lib/api";
import { safeAuthUrl } from "../../../lib/user-grants";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useV2Toast } from "../../hooks";
import { Alert, Button, LinkButton } from "../../ui";
import "./contextLayer.css";
import { loadDemoInfo } from "./demoInfo";
import { type AccountView, demoOffered } from "./state";

const MASK = "••••••••••••";

/**
 * The shared COA demo account's credentials, for the member to type at COA's own
 * login page (Launchpad never signs in for anyone). The username comes with the
 * status; the password is fetched only when the member shows or copies it
 * (`POST …/demo-account/reveal`, logged server-side) and lives in this
 * component's state only — gone when the panel closes.
 */
export function DemoCredentials({
  username,
  hint,
  testId = "context-layer-demo",
}: {
  username: string | null;
  /** what to do with the credentials; the default is the COA login page's */
  hint?: string;
  testId?: string;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [creds, setCreds] = useState<ContextLayerDemoCredentials | null>(null);
  const [shown, setShown] = useState(false);
  const [busy, setBusy] = useState(false);
  const name = username ?? creds?.username ?? null;

  const reveal = async (): Promise<ContextLayerDemoCredentials | null> => {
    if (creds) return creds;
    setBusy(true);
    try {
      const got = await api.revealContextLayerDemoAccount();
      setCreds(got);
      return got;
    } catch (err) {
      toast("error", errorMessage(err));
      return null;
    } finally {
      setBusy(false);
    }
  };
  const copy = async (value: () => Promise<string | null>, isPassword: boolean) => {
    const text = await value();
    if (!text) return;
    try {
      await navigator.clipboard.writeText(text);
      toast("success", t("v2.contextLayer.demo.copied"));
    } catch {
      // no clipboard (http LAN, or the gesture expired during the reveal): show it
      if (isPassword) setShown(true);
      toast("error", t("v2.contextLayer.demo.copyFailed"));
    }
  };
  const toggle = async () => {
    if (shown) {
      setShown(false);
      return;
    }
    if (await reveal()) setShown(true);
  };

  return (
    <div className="v2-cl-demo" data-testid={testId}>
      <Alert tone="warn">{t("v2.contextLayer.demo.notice")}</Alert>
      <div className="v2-cl-demo-creds">
        <span className="k">{t("v2.contextLayer.demo.username")}</span>
        <code data-testid={`${testId}-username`}>{name ?? "—"}</code>
        <span />
        <Button
          size="sm"
          disabled={busy}
          onClick={() => void copy(async () => name ?? (await reveal())?.username ?? null, false)}
          testId={`${testId}-copy-username`}
        >
          <Copy size={13} aria-hidden="true" />
          {t("v2.contextLayer.demo.copy")}
        </Button>
        <span className="k">{t("v2.contextLayer.demo.password")}</span>
        <code data-testid={`${testId}-password`} data-shown={shown && creds ? "true" : "false"}>
          {shown && creds ? creds.password : MASK}
        </code>
        <Button size="sm" disabled={busy} onClick={() => void toggle()} testId={`${testId}-show`}>
          {shown ? <EyeOff size={13} aria-hidden="true" /> : <Eye size={13} aria-hidden="true" />}
          {t(shown ? "v2.contextLayer.demo.hide" : "v2.contextLayer.demo.show")}
        </Button>
        <Button
          size="sm"
          disabled={busy}
          onClick={() => void copy(async () => (await reveal())?.password ?? null, true)}
          testId={`${testId}-copy-password`}
        >
          <Copy size={13} aria-hidden="true" />
          {t("v2.contextLayer.demo.copy")}
        </Button>
      </div>
      <p className="v2-muted">{hint ?? t("v2.contextLayer.demo.signInHint")}</p>
    </div>
  );
}

/** COA's sign-out link (web-client `/logout`, lands on the COA console), else a link
 *  to the COA console to sign out there. Either only when it is https. */
export function SignOutLink({
  signOutUrl,
  consoleUrl,
  testId,
}: {
  signOutUrl: string | null | undefined;
  consoleUrl: string | null | undefined;
  testId?: string;
}) {
  const { t } = useTranslation();
  const signOut = safeAuthUrl(signOutUrl);
  const href = signOut ?? safeAuthUrl(consoleUrl);
  if (!href) return null;
  return (
    <a className="v2-btn sm" href={href} target="_blank" rel="noopener noreferrer" data-testid={testId}>
      {signOut ? <LogOut size={13} aria-hidden="true" /> : <ExternalLink size={13} aria-hidden="true" />}
      {t(signOut ? "v2.contextLayer.demo.guide.signOutAction" : "v2.contextLayer.demo.guide.signOutManual")}
    </a>
  );
}

/**
 * Switching COA accounts: revoke, sign out of COA, authorize again. COA's classic
 * hosted UI consents silently as whoever its browser session is signed in as, so the
 * sign-out in the middle is what makes the next consent ask for credentials.
 */
export function SwitchGuide({
  view,
  demoUsername,
  signOutUrl,
  consoleUrl,
  busy,
  onRevoke,
  onAuthorize,
  onCancel,
}: {
  view: Extract<AccountView, { kind: "guide" }>;
  demoUsername: string | null;
  signOutUrl: string | null | undefined;
  consoleUrl: string | null | undefined;
  busy: boolean;
  onRevoke: () => void;
  onAuthorize: () => void;
  onCancel: () => void;
}) {
  const { t } = useTranslation();
  const toDemo = view.target === "demo";
  const done = <CheckCircle2 size={14} aria-hidden="true" className="v2-cl-done" />;
  return (
    <div className="v2-cl-guide" data-testid="context-layer-guide" data-target={view.target} data-revoked={String(view.revoked)}>
      {view.wrong && (
        <Alert tone="warn">
          <span data-testid="context-layer-wrong-account">{t(toDemo ? "v2.contextLayer.demo.wrongDemo" : "v2.contextLayer.demo.wrongOwn")}</span>
        </Alert>
      )}
      <strong>{t(toDemo ? "v2.contextLayer.demo.guide.titleDemo" : "v2.contextLayer.demo.guide.titleOwn")}</strong>
      <p className="v2-muted">{t("v2.contextLayer.demo.guide.intro")}</p>
      <ol>
        <li>
          <span>{t("v2.contextLayer.demo.guide.revoke")}</span>
          {view.revoked ? (
            done
          ) : (
            <Button size="sm" kind="danger" disabled={busy} onClick={onRevoke} testId="context-layer-guide-revoke">
              {t("v2.contextLayer.auth.revoke")}
            </Button>
          )}
        </li>
        <li>
          <span>{t("v2.contextLayer.demo.guide.signOut")}</span>
          <SignOutLink signOutUrl={signOutUrl} consoleUrl={consoleUrl} testId="context-layer-guide-signout" />
        </li>
        <li>
          <span>{t(toDemo ? "v2.contextLayer.demo.guide.authorizeDemo" : "v2.contextLayer.demo.guide.authorizeOwn")}</span>
          <Button size="sm" kind="primary" disabled={!view.revoked || busy} onClick={onAuthorize} testId="context-layer-guide-authorize">
            <ExternalLink size={13} aria-hidden="true" />
            {t("v2.contextLayer.auth.authorize")}
          </Button>
        </li>
      </ol>
      {toDemo && view.revoked && <DemoCredentials username={demoUsername} />}
      <div>
        <LinkButton onClick={onCancel} testId="context-layer-guide-cancel">
          {t("v2.common.cancel")}
        </LinkButton>
      </div>
    </div>
  );
}

/**
 * The Chat (and playground) consent card's offer: when the ask is for the context
 * layer's Connection and the layer has a demo account, a link that opens its
 * credentials. The consent link itself stays the one AgentCore issued; nothing is
 * prefilled at COA's login page, so the member copies both fields.
 */
export function DemoAccountOffer({ provider }: { provider: string }) {
  const { t } = useTranslation();
  const { current } = useWorkspace();
  const workspaceId = current?.id ?? "";
  const [info, setInfo] = useState<ContextLayerDemoInfo | null>(null);
  const [open, setOpen] = useState(false);
  useEffect(() => {
    let live = true;
    loadDemoInfo(workspaceId).then(
      (got) => live && setInfo(got),
      () => undefined, // no offer: the card works as before
    );
    return () => {
      live = false;
    };
  }, [workspaceId]);
  if (!info || !demoOffered({ provider }, info)) return null;
  return (
    <div className="v2-cl-demo-offer" data-testid="auth-card-demo">
      <LinkButton onClick={() => setOpen((v) => !v)} testId="auth-card-demo-toggle">
        {t("v2.contextLayer.demo.chatLink")}
      </LinkButton>
      {open && <DemoCredentials username={info.username} hint={t("v2.contextLayer.demo.chatHint")} testId="auth-card-demo-panel" />}
    </div>
  );
}
