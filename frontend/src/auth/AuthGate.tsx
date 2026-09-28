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
import { V2AuthFrame, V2AuthLoading } from "../v2/AuthFrame";
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
};

const REGISTER_ERROR_KEYS: Record<string, string> = {
  "auth.invalid_username": "auth.errInvalidUsername",
  "auth.username_taken": "auth.errUsernameTaken",
  "auth.invalid_email": "auth.errInvalidEmail",
  "auth.email_domain_blocked": "auth.errEmailDomainBlocked",
  "auth.email_taken": "auth.errEmailTaken",
  "auth.weak_password": "auth.errWeakPassword",
  "auth.registration_disabled": "auth.errRegistrationDisabled",
};

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
      username: result.username,
      role: result.role,
      email: result.email,
      account_expires_at: result.account_expires_at,
      permissions: result.permissions ?? [],
    });
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.logout();
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
      isAdmin,
      can: (permission: AgentPermission) => isAdmin || granted.has(permission),
      logout,
    };
  }, [
    logout,
    status?.account_expires_at,
    status?.auth_required,
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
      />
    );
  }
  return <AuthContext.Provider value={context}>{children}</AuthContext.Provider>;
}

function AuthLoading() {
  const { t } = useTranslation();
  return <V2AuthLoading label={t("auth.checking")} />;
}

/** The console's create → deploy → invoke → observe loop, beside the form. */
const HERO_STEPS = [
  { key: "create", icon: Sparkles },
  { key: "deploy", icon: Rocket },
  { key: "invoke", icon: MessagesSquare },
  { key: "observe", icon: Activity },
] as const;

function LoginPage({
  onLogin,
  registrationEnabled,
  requiresApproval,
}: {
  onLogin: (result: AuthLoginResult) => void;
  registrationEnabled: boolean;
  requiresApproval: boolean;
}) {
  const { t } = useTranslation();
  const [mode, setMode] = useState<"signin" | "register">("signin");
  const [prefill, setPrefill] = useState("");

  const onRegistered = (result: RegisterResult) => {
    setPrefill(result.username);
    setMode("signin");
  };

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
          <div className="v2-card-body">
            {registrationEnabled ? (
              <div className="v2-auth-tabs" role="tablist" data-testid="auth-tabs">
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
              <SignInForm onLogin={onLogin} prefill={prefill} />
            ) : (
              <RegisterForm onRegistered={onRegistered} requiresApproval={requiresApproval} />
            )}
          </div>
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
  return (
    <div className="v2-field">
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
  return (
    <button className="v2-btn primary v2-auth-submit" type="submit" disabled={busy}>
      {busy ? <LoaderCircle className="spin" size={16} aria-hidden="true" /> : icon}
      {busy ? busyLabel : label}
    </button>
  );
}

function SignInForm({
  onLogin,
  prefill,
}: {
  onLogin: (result: AuthLoginResult) => void;
  prefill: string;
}) {
  const { t } = useTranslation();
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
      setError(t(key ?? "auth.loginFailed"));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <form onSubmit={submit} noValidate>
      <h2>{t("auth.title")}</h2>
      <p className="sub">{t("auth.v2.subtitle")}</p>

      <div className="v2-auth-fields">
        <Field id="auth-username" label={t("auth.v2.username")}>
          <input
            id="auth-username"
            className="v2-input"
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
            className="v2-input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
          />
        </Field>
      </div>

      <div className="v2-auth-error" role="alert" aria-live="polite">
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
      setError(t(key ?? "auth.registerFailed"));
    } finally {
      setSubmitting(false);
    }
  };

  if (done) {
    const pending = done.requires_approval;
    return (
      <div data-testid="register-success">
        <div className={`v2-auth-icon${pending ? " wait" : " ok"}`} aria-hidden="true">
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
        <div className="v2-auth-facts">
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
        <button className="v2-btn primary v2-auth-submit" type="button" onClick={() => onRegistered(done)}>
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

      <div className="v2-auth-fields">
        <Field id="reg-username" label={t("auth.v2.username")} hint={t("auth.usernameHint")}>
          <input
            id="reg-username"
            className="v2-input"
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
            className="v2-input"
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
            className="v2-input"
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
            className="v2-input"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            disabled={submitting}
          />
        </Field>
      </div>

      <div className="v2-auth-error" role="alert" aria-live="polite">
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
