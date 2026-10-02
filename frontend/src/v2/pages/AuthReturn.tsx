import "../v2.css";
import "./connections/connections.css";

import { CheckCircle2, CircleAlert, KeyRound, Loader2 } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { api, type CompleteOauthSessionResult, errorMessage } from "../../lib/api";
import { Button } from "../ui";
import { chatPathFor, parseReturnParams } from "./authReturnParams";

// A session uri is single-use: completing it twice (StrictMode re-running the
// effect, a remount) would fail the second time, so each is posted once.
const posted = new Map<string, Promise<CompleteOauthSessionResult>>();

type Phase = "binding" | "done" | "failed";

/**
 * `/auth/return` — where AgentCore Identity sends the browser after the user
 * consented at the IdP. It performs the binding leg (CompleteResourceTokenAuth,
 * server-side, for the user recorded with the session — the signed-in caller
 * must be that user), then sends the user back to the Chat tab, whose auth card
 * has been polling and flips to authorized.
 */
export function AuthReturn() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const [ret] = useState(() => parseReturnParams(window.location.search));
  const [phase, setPhase] = useState<Phase>(ret.sessionUri ? "binding" : "failed");
  const [result, setResult] = useState<CompleteOauthSessionResult | null>(null);
  const [error, setError] = useState<string | null>(ret.sessionUri ? null : t("v2.authReturn.missing"));
  const live = useRef(true);

  useEffect(() => {
    document.body.classList.add("v2-body");
    return () => document.body.classList.remove("v2-body");
  }, []);

  useEffect(() => {
    live.current = true;
    const sessionUri = ret.sessionUri;
    if (!sessionUri) return;
    let pending = posted.get(sessionUri);
    if (!pending) {
      pending = api.completeOauthSession(sessionUri);
      posted.set(sessionUri, pending);
    }
    pending
      .then((done) => {
        if (!live.current) return;
        setResult(done);
        setPhase("done");
      })
      .catch((err: unknown) => {
        if (!live.current) return;
        setError(errorMessage(err)); // already the localized apiErrors.<code> copy
        setPhase("failed");
      })
      .finally(() => {
        // the one-time capability has been spent: keep it out of the address bar
        if (live.current) setParams({}, { replace: true });
      });
    return () => {
      live.current = false;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const backToChat = () => {
    // The Chat tab opened this one: closing it returns the user there. A tab
    // the script may not close (the IdP redirects count as navigations) falls
    // back to opening that conversation here.
    window.close();
    window.setTimeout(() => {
      if (!window.closed) window.location.assign(chatPathFor(ret));
    }, 250);
  };

  const Icon = phase === "binding" ? Loader2 : phase === "done" ? CheckCircle2 : CircleAlert;
  const connection = result?.provider;
  const agentName = result?.agent_name;

  return (
    <div className="v2 v2-auth-return" data-testid="auth-return" data-phase={phase}>
      <section className="v2-card v2-auth-return-card">
        <div className="v2-card-body">
          <div className={`v2-auth-return-icon ${phase}`}>
            <Icon size={28} aria-hidden="true" />
          </div>
          <h1 className="v2-auth-return-title">{t(`v2.authReturn.title.${phase}`)}</h1>
          <p className="v2-auth-return-body" data-testid="auth-return-body">
            {phase === "binding"
              ? t("v2.authReturn.binding")
              : phase === "done"
                ? agentName
                  ? t("v2.authReturn.doneAgent", { connection, agent: agentName })
                  : t("v2.authReturn.done", { connection })
                : error}
          </p>
          {phase === "done" && (
            <p className="v2-auth-return-hint">{t("v2.authReturn.doneHint")}</p>
          )}
          {ret.tool && (
            <p className="v2-auth-return-meta">
              <KeyRound size={13} aria-hidden="true" />
              {t("v2.authReturn.tool")} <span className="mono">{ret.tool}</span>
            </p>
          )}
          <div className="v2-auth-return-actions">
            <Button kind="primary" disabled={phase === "binding"} onClick={backToChat} testId="auth-return-back">
              {t("v2.authReturn.back")}
            </Button>
            <Link to="/v2/my-connections" className="v2-link" data-testid="auth-return-grants">
              {t("v2.authReturn.myConnections")}
            </Link>
          </div>
        </div>
      </section>
    </div>
  );
}
