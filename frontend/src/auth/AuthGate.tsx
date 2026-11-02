import {
  Activity,
  CircleCheck,
  Hourglass,
  LoaderCircle,
  LogIn,
  MessagesSquare,
  Rocket,
  Sparkles,
  UserPlus,
} from "lucide-react";
import {
  type FormEvent,
  type ReactNode,
  useCallback,
  useEffect,
  useMemo,
  useState,
} from "react";
import { useTranslation } from "react-i18next";

import {
  type AgentPermission,
  api,
  ApiError,
  AUTH_UNAUTHORIZED_EVENT,
  type AuthLoginResult,
  type AuthStatus,
  type RegisterResult,
} from "../lib/api";
import { ssoApi } from "../lib/api/sso";
import { useUiVersion } from "../lib/ui-version";
import { V2AuthFrame, V2AuthLoading } from "../v2/AuthFrame";
import { V3AuthFrame, V3AuthLoading } from "../v3/AuthFrame";
import { SsoSignIn } from "../v3/pages/sso/SsoSignIn";
import { Lamp } from "../v3/ui";
import { AuthContext } from "./auth-context";

const AUTH_DISABLED: AuthStatus = {
  auth_required: false,
  authenticated: true,
  registration_enabled: false,
  registration_requires_approval: false,
  username: null,
  role: null,
  email: null,
  account_expires_at: null,
  permissions: [],
};

const LOGGED_OUT = (previous: AuthStatus | null): AuthStatus => ({
  auth_required: true,
  authenticated: false,
  registration_enabled: previous?.registration_enabled ?? false,
  registration_requires_approval: previous?.registration_requires_approval ?? true,
  registration_closed_notice: previous?.registration_closed_notice,
  username: null,
  role: null,
  email: null,
  account_expires_at: null,
  permissions: [],
});

/** Backend error code → i18n key, so the gate never invents its own wording. */
const LOGIN_ERROR_KEYS: Record<string, string> = {
  "auth.invalid_credentials": "auth.invalidCredentials",
  "auth.account_pending": "auth.accountPending",
  "auth.account_expired": "auth.accountExpired",
  "auth.account_disabled": "auth.accountDisabled",
  "auth.too_many_attempts": "auth.errTooManyAttempts",
  "auth.login_locked": "auth.errLoginLocked",
};

const REGISTER_ERROR_KEYS: Record<string, string> = {
  "auth.invalid_username": "auth.errInvalidUsername",
  "auth.username_taken": "auth.errUsernameTaken",
  "auth.invalid_email": "auth.errInvalidEmail",
  "auth.email_domain_blocked": "auth.errEmailDomainBlocked",
  "auth.email_taken": "auth.errEmailTaken",
  "auth.weak_password": "auth.errWeakPassword",
  "auth.registration_disabled": "auth.errRegistrationDisabled",
  "auth.too_many_attempts": "auth.errTooManyAttempts",
  "auth.registration_queue_full": "auth.errRegistrationQueueFull",
};

/** Whole minutes until a throttled sign-in / registration may retry (≥ 1). */
function retryMinutes(caught: unknown): number {
  const detail = caught instanceof ApiError ? caught.detail : null;
  const seconds =
    detail && typeof detail === "object" && "retry_after_seconds" in detail
      ? Number((detail as { retry_after_seconds: unknown }).retry_after_seconds)
      : NaN;
  return Number.isFinite(seconds) && seconds > 0 ? Math.max(1, Math.ceil(seconds / 60)) : 1;
}

export function AuthGate({ children }: { children: ReactNode }) {
  const [status, setStatus] = useState<AuthStatus | null>(null);

  useEffect(() => {
    let active = true;
    api
      .authStatus()
      .then((next) => {
        if (active) setStatus(next);
      })
      .catch(() => {
        if (active) setStatus(AUTH_DISABLED);
      });
    return () => {
      active = false;
    };
  }, []);

  useEffect(() => {
    const onUnauthorized = () => {
      setStatus(LOGGED_OUT);
    };
    window.addEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
    return () => window.removeEventListener(AUTH_UNAUTHORIZED_EVENT, onUnauthorized);
  }, []);

  const onLogin = useCallback((result: AuthLoginResult) => {
    setStatus({
      auth_required: result.auth_required,
      authenticated: true,
      registration_enabled: result.registration_enabled,
      registration_requires_approval: result.registration_requires_approval,
      registration_closed_notice: result.registration_closed_notice,
      username: result.username,
      role: result.role,
      email: result.email,
      account_expires_at: result.account_expires_at,
      permissions: result.permissions ?? [],
      builtin: result.builtin ?? false,
      account_source: result.account_source,
    });
  }, []);

  const logout = useCallback(async () => {
    try {
      // an SSO session also ends at its IdP (RP-initiated logout, sso D-5)
      const answer = await ssoApi.logout();
      if (answer?.end_session_url) {
        window.location.assign(answer.end_session_url);
        return;
      }
    } finally {
      setStatus(LOGGED_OUT);
    }
  }, []);

  const context = useMemo(() => {
    // an open console keeps the pre-multi-user behavior: full local access
    const isAdmin = !(status?.auth_required ?? false) || status?.role === "admin";
    const granted = new Set(status?.permissions ?? []);
    return {
      authRequired: status?.auth_required ?? false,
      username: status?.username ?? null,
      role: status?.role ?? null,
      email: status?.email ?? null,
      accountExpiresAt: status?.account_expires_at ?? null,
      builtin: status?.builtin ?? false,
      accountSource: status?.account_source ?? null,
      isAdmin,
      can: (permission: AgentPermission) => isAdmin || granted.has(permission),
      logout,
    };
  }, [
    logout,
    status?.account_expires_at,
    status?.account_source,
    status?.auth_required,
    status?.builtin,
    status?.email,
    status?.permissions,
    status?.role,
    status?.username,
  ]);

  if (status === null) return <AuthLoading />;
  if (status.auth_required && !status.authenticated) {
    return (
      <LoginPage
        onLogin={onLogin}
        registrationEnabled={status.registration_enabled}
        requiresApproval={status.registration_requires_approval}
        closedNotice={status.registration_closed_notice}
      />
    );
  }
  return <AuthContext.Provider value={context}>{children}</AuthContext.Provider>;
}

/**
 * The sign-in page wears the chrome of the console the browser will open: V3 (the
 * default) or V2 — the classic console's operators land in V2's light frame, as
 * before. The forms are shared; their class names carry the console's prefix.
 */
function useSkin(): "v2" | "v3" {
  return useUiVersion() === "v3" ? "v3" : "v2";
}

function AuthLoading() {
  const { t } = useTranslation();
  const Loading = useSkin() === "v3" ? V3AuthLoading : V2AuthLoading;
  return <Loading label={t("auth.checking")} />;
}

/** The console's create → deploy → invoke → observe loop, beside the form. */
const HERO_STEPS = [
  { key: "create", icon: Sparkles },
  { key: "deploy", icon: Rocket },
  { key: "invoke", icon: MessagesSquare },
  { key: "observe", icon: Activity },
] as const;

/** Under the sign-in form while an operator closed registration: their own text
 *  (`auth_registration_closed_notice`, shown as written) or the generic copy. */
export function RegistrationClosedNotice({
  skin,
  notice,
}: {
  skin: "v2" | "v3";
  notice: string;
}) {
  const { t } = useTranslation();
  return (
    <p className={`${skin}-auth-notice`} role="note" data-testid="auth-registration-closed">
      {notice.trim() || t("auth.registrationClosedNotice")}
    </p>
  );
}

function LoginPage({
  onLogin,
  registrationEnabled,
  requiresApproval,
  closedNotice,
}: {
  onLogin: (result: AuthLoginResult) => void;
  registrationEnabled: boolean;
  requiresApproval: boolean;
  /** set only while an operator closed registration; "" = the generic copy */
  closedNotice?: string;
}) {
  const { t } = useTranslation();
  const skin = useSkin();
  const [mode, setMode] = useState<"signin" | "register">("signin");
  const [prefill, setPrefill] = useState("");

  const onRegistered = (result: RegisterResult) => {
    setPrefill(result.username);
    setMode("signin");
  };

  const body = (
    <>
      {registrationEnabled ? (
        <div className={`${skin}-auth-tabs`} role="tablist" data-testid="auth-tabs">
          <button
            type="button"
            role="tab"
            aria-selected={mode === "signin"}
            className={mode === "signin" ? "on" : ""}
            onClick={() => setMode("signin")}
            data-testid="auth-tab-signin"
          >
            {t("auth.v2.signIn")}
          </button>
          <button
            type="button"
            role="tab"
            aria-selected={mode === "register"}
            className={mode === "register" ? "on" : ""}
            onClick={() => setMode("register")}
            data-testid="auth-tab-register"
          >
            {t("auth.v2.register")}
          </button>
        </div>
      ) : null}
      {mode === "signin" ? (
        <SsoSignIn skin={skin}>
          {(nested) => <SignInForm onLogin={onLogin} prefill={prefill} nested={nested} />}
        </SsoSignIn>
      ) : (
        <RegisterForm onRegistered={onRegistered} requiresApproval={requiresApproval} />
      )}
      {!registrationEnabled && closedNotice !== undefined ? (
        <RegistrationClosedNotice skin={skin} notice={closedNotice} />
      ) : null}
    </>
  );

  if (skin === "v3") {
    return (
      <V3AuthFrame testId="auth-page">
        <div className="v3-auth-split">
          <section className="v3-auth-hero">
            <span className="v3-auth-kicker">
              <Lamp s="ok" />
              Amazon Bedrock AgentCore
            </span>
            <h1>{t("auth.v2.heroTitle")}</h1>
            <p>{t("auth.v2.heroBody")}</p>
            <ol className="v3-auth-steps">
              {HERO_STEPS.map(({ key, icon: Icon }, index) => (
                <li key={key}>
                  <span className="ico"><Icon size={17} aria-hidden="true" /></span>
                  <div>
                    <b>
                      <span className="idx">{String(index + 1).padStart(2, "0")}</span>
                      {t(`auth.v2.steps.${key}.title`)}
                    </b>
                    <span className="body">{t(`auth.v2.steps.${key}.body`)}</span>
                  </div>
                </li>
              ))}
            </ol>
          </section>
          <div className="v3-auth-card">{body}</div>
        </div>
      </V3AuthFrame>
    );
  }

  return (
    <V2AuthFrame testId="auth-page">
      <div className="v2-auth-split">
        <section className="v2-auth-hero">
          <h1>{t("auth.v2.heroTitle")}</h1>
          <p>{t("auth.v2.heroBody")}</p>
          <ol className="v2-auth-steps">
            {HERO_STEPS.map(({ key, icon: Icon }) => (
              <li key={key}>
                <span className="ico"><Icon size={18} aria-hidden="true" /></span>
                <div>
                  <b>{t(`auth.v2.steps.${key}.title`)}</b>
                  <span>{t(`auth.v2.steps.${key}.body`)}</span>
                </div>
              </li>
            ))}
          </ol>
        </section>
        <div className="v2-card v2-auth-card">
          <div className="v2-card-body">{body}</div>
        </div>
      </div>
    </V2AuthFrame>
  );
}

function Field({ id, label, hint, children }: {
  id: string;
  label: string;
  hint?: string;
  children: ReactNode;
}) {
  const skin = useSkin();
  return (
    <div className={`${skin}-field`}>
      <label htmlFor={id}>{label}</label>
      {children}
      {hint ? <span className="hint">{hint}</span> : null}
    </div>
  );
}

function Submit({ busy, icon, label, busyLabel }: {
  busy: boolean;
  icon: ReactNode;
  label: string;
  busyLabel: string;
}) {
  const skin = useSkin();
  return (
    <button className={`${skin}-btn primary ${skin}-auth-submit`} type="submit" disabled={busy}>
      {busy ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : icon}
      {busy ? busyLabel : label}
    </button>
  );
}

function SignInForm({
  onLogin,
  prefill,
  nested = false,
}: {
  onLogin: (result: AuthLoginResult) => void;
  prefill: string;
  /** under the single sign-on buttons: the card already has its heading */
  nested?: boolean;
}) {
  const { t } = useTranslation();
  const skin = useSkin();
  const [username, setUsername] = useState(prefill);
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!username.trim() || !password) {
      setError(t("auth.missingCredentials"));
      return;
    }

    setError("");
    setSubmitting(true);
    try {
      const result = await api.login(username.trim(), password);
      onLogin(result);
    } catch (caught) {
      const key =
        caught instanceof ApiError ? LOGIN_ERROR_KEYS[caught.code] : undefined;
      setError(t(key ?? "auth.loginFailed", { minutes: retryMinutes(caught) }));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit} noValidate>
      {!nested && (
        <>
          <h2>{t("auth.title")}</h2>
          <p className="sub">{t("auth.v2.subtitle")}</p>
        </>
      )}

      <div className={`${skin}-auth-fields`}>
        <Field id="auth-username" label={t("auth.v2.username")}>
          <input
            id="auth-username"
            className={`${skin}-input`}
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={submitting}
            autoFocus
          />
        </Field>
        <Field id="auth-password" label={t("auth.v2.password")}>
          <input
            id="auth-password"
            className={`${skin}-input`}
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
          />
        </Field>
      </div>

      <div className={`${skin}-auth-error`} role="alert" aria-live="polite">
        {error}
      </div>
      <Submit
        busy={submitting}
        icon={<LogIn size={16} aria-hidden="true" />}
        label={t("auth.v2.signIn")}
        busyLabel={t("auth.v2.signingIn")}
      />
    </form>
  );
}

function RegisterForm({
  onRegistered,
  requiresApproval,
}: {
  onRegistered: (result: RegisterResult) => void;
  requiresApproval: boolean;
}) {
  const { t } = useTranslation();
  const skin = useSkin();
  const [username, setUsername] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirm, setConfirm] = useState("");
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [done, setDone] = useState<RegisterResult | null>(null);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!username.trim() || !email.trim() || !password) {
      setError(t("auth.missingRegistrationFields"));
      return;
    }
    if (password !== confirm) {
      setError(t("auth.passwordMismatch"));
      return;
    }

    setError("");
    setSubmitting(true);
    try {
      const result = await api.register(username.trim(), email.trim(), password);
      setDone(result);
    } catch (caught) {
      const key =
        caught instanceof ApiError ? REGISTER_ERROR_KEYS[caught.code] : undefined;
      setError(t(key ?? "auth.registerFailed", { minutes: retryMinutes(caught) }));
    } finally {
      setSubmitting(false);
    }
  };

  if (done) {
    const pending = done.requires_approval;
    return (
      <div data-testid="register-success">
        <div className={`${skin}-auth-icon${pending ? " wait" : " ok"}`} aria-hidden="true">
          {pending ? (
            <Hourglass size={22} strokeWidth={1.8} />
          ) : (
            <CircleCheck size={22} strokeWidth={1.8} />
          )}
        </div>
        <h2>{t(pending ? "auth.registerPendingTitle" : "auth.registerDoneTitle")}</h2>
        <p className="sub">
          {pending
            ? t("auth.registerPendingBody", {
                username: done.username,
                days: done.valid_days,
              })
            : t("auth.registerDoneBody", {
                username: done.username,
                days: done.valid_days,
              })}
        </p>
        <div className={`${skin}-auth-facts`}>
          <div>
            <span>{t("auth.v2.email")}</span>
            <b>{done.email}</b>
          </div>
          <div>
            <span>{t(pending ? "auth.v2.accountStatus" : "auth.v2.validUntil")}</span>
            <b data-testid="register-status">
              {pending
                ? t("auth.v2.statusPending")
                : done.expires_at
                  ? new Date(done.expires_at).toLocaleString()
                  : "—"}
            </b>
          </div>
        </div>
        <button className={`${skin}-btn primary ${skin}-auth-submit`} type="button" onClick={() => onRegistered(done)}>
          <LogIn size={16} aria-hidden="true" />
          {t("auth.v2.goToSignIn")}
        </button>
      </div>
    );
  }

  return (
    <form onSubmit={submit} noValidate>
      <h2>{t("auth.registerTitle")}</h2>
      <p className="sub">
        {t(requiresApproval ? "auth.registerSubtitleApproval" : "auth.registerSubtitle")}
      </p>

      <div className={`${skin}-auth-fields`}>
        <Field id="reg-username" label={t("auth.v2.username")} hint={t("auth.usernameHint")}>
          <input
            id="reg-username"
            className={`${skin}-input`}
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={submitting}
            autoFocus
          />
        </Field>
        <Field id="reg-email" label={t("auth.v2.companyEmail")} hint={t("auth.companyEmailHint")}>
          <input
            id="reg-email"
            className={`${skin}-input`}
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            disabled={submitting}
          />
        </Field>
        <Field id="reg-password" label={t("auth.v2.password")} hint={t("auth.passwordHint")}>
          <input
            id="reg-password"
            className={`${skin}-input`}
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
          />
        </Field>
        <Field id="reg-confirm" label={t("auth.v2.confirmPassword")}>
          <input
            id="reg-confirm"
            className={`${skin}-input`}
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            disabled={submitting}
          />
        </Field>
      </div>

      <div className={`${skin}-auth-error`} role="alert" aria-live="polite">
        {error}
      </div>
      <Submit
        busy={submitting}
        icon={<UserPlus size={16} aria-hidden="true" />}
        label={t("auth.v2.createAccount")}
        busyLabel={t("auth.v2.registering")}
      />
    </form>
  );
}
