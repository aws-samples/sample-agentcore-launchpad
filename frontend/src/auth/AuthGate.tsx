import {
  CircleCheck,
  Hourglass,
  KeyRound,
  LoaderCircle,
  LogIn,
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

import { Btn } from "../components";
import { LangSwitcher } from "../layout/LangSwitcher";
import {
  type AgentPermission,
  api,
  ApiError,
  AUTH_UNAUTHORIZED_EVENT,
  type AuthLoginResult,
  type AuthStatus,
  type RegisterResult,
} from "../lib/api";
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
  return (
    <div className="auth-loading" role="status">
      <LoaderCircle size={22} strokeWidth={1.8} aria-hidden="true" />
      <span className="sr-only">{t("auth.checking")}</span>
    </div>
  );
}

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
    <div className="auth-page">
      <header className="auth-topbar">
        <div className="brand">
          <span className="glyph" aria-hidden="true" />
          AGENTCORE<em>//</em>LAUNCHPAD
        </div>
        <LangSwitcher />
      </header>
      <main className="auth-main">
        {registrationEnabled ? (
          <div className="auth-tabs" role="tablist" data-testid="auth-tabs">
            <button
              type="button"
              role="tab"
              aria-selected={mode === "signin"}
              className={mode === "signin" ? "active" : ""}
              onClick={() => setMode("signin")}
              data-testid="auth-tab-signin"
            >
              {t("auth.signIn")}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={mode === "register"}
              className={mode === "register" ? "active" : ""}
              onClick={() => setMode("register")}
              data-testid="auth-tab-register"
            >
              {t("auth.register")}
            </button>
          </div>
        ) : null}
        {mode === "signin" ? (
          <SignInForm onLogin={onLogin} prefill={prefill} />
        ) : (
          <RegisterForm onRegistered={onRegistered} requiresApproval={requiresApproval} />
        )}
      </main>
    </div>
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
    <form className="auth-panel" onSubmit={submit} noValidate>
      <div className="auth-icon" aria-hidden="true">
        <KeyRound size={24} strokeWidth={1.7} />
      </div>
      <div className="kicker">{t("auth.kicker")}</div>
      <h1>{t("auth.title")}</h1>
      <p className="auth-subtitle">{t("auth.subtitle")}</p>

      <div className="auth-fields">
        <div className="auth-field">
          <label htmlFor="auth-username">{t("auth.username")}</label>
          <input
            id="auth-username"
            className="input"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={submitting}
            autoFocus
          />
        </div>
        <div className="auth-field">
          <label htmlFor="auth-password">{t("auth.password")}</label>
          <input
            id="auth-password"
            className="input"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
          />
        </div>
      </div>

      <div className="auth-error" role="alert" aria-live="polite">
        {error}
      </div>
      <Btn className="auth-submit" type="submit" primary disabled={submitting}>
        {submitting ? (
          <LoaderCircle className="spin" size={16} aria-hidden="true" />
        ) : (
          <LogIn size={16} aria-hidden="true" />
        )}
        {submitting ? t("auth.signingIn") : t("auth.signIn")}
      </Btn>
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
      <div className="auth-panel" data-testid="register-success">
        <div className={`auth-icon${pending ? " wait" : " ok"}`} aria-hidden="true">
          {pending ? (
            <Hourglass size={24} strokeWidth={1.7} />
          ) : (
            <CircleCheck size={24} strokeWidth={1.7} />
          )}
        </div>
        <div className="kicker">{t("auth.registerKicker")}</div>
        <h1>{t(pending ? "auth.registerPendingTitle" : "auth.registerDoneTitle")}</h1>
        <p className="auth-subtitle">
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
        <div className="auth-success">
          <div>
            <span>{t("auth.email")}</span>
            <b>{done.email}</b>
          </div>
          <div>
            <span>{t(pending ? "auth.accountStatus" : "auth.validUntil")}</span>
            <b data-testid="register-status">
              {pending
                ? t("auth.statusPending")
                : done.expires_at
                  ? new Date(done.expires_at).toLocaleString()
                  : "—"}
            </b>
          </div>
        </div>
        <Btn className="auth-submit" primary onClick={() => onRegistered(done)}>
          <LogIn size={16} aria-hidden="true" />
          {t("auth.goToSignIn")}
        </Btn>
      </div>
    );
  }

  return (
    <form className="auth-panel" onSubmit={submit} noValidate>
      <div className="auth-icon" aria-hidden="true">
        <UserPlus size={24} strokeWidth={1.7} />
      </div>
      <div className="kicker">{t("auth.registerKicker")}</div>
      <h1>{t("auth.registerTitle")}</h1>
      <p className="auth-subtitle">
        {t(requiresApproval ? "auth.registerSubtitleApproval" : "auth.registerSubtitle")}
      </p>

      <div className="auth-fields">
        <div className="auth-field">
          <label htmlFor="reg-username">{t("auth.username")}</label>
          <input
            id="reg-username"
            className="input"
            autoComplete="username"
            value={username}
            onChange={(event) => setUsername(event.target.value)}
            disabled={submitting}
            autoFocus
          />
          <span className="auth-hint">{t("auth.usernameHint")}</span>
        </div>
        <div className="auth-field">
          <label htmlFor="reg-email">{t("auth.companyEmail")}</label>
          <input
            id="reg-email"
            className="input"
            type="email"
            autoComplete="email"
            value={email}
            onChange={(event) => setEmail(event.target.value)}
            disabled={submitting}
          />
          <span className="auth-hint">{t("auth.companyEmailHint")}</span>
        </div>
        <div className="auth-field">
          <label htmlFor="reg-password">{t("auth.password")}</label>
          <input
            id="reg-password"
            className="input"
            type="password"
            autoComplete="new-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            disabled={submitting}
          />
          <span className="auth-hint">{t("auth.passwordHint")}</span>
        </div>
        <div className="auth-field">
          <label htmlFor="reg-confirm">{t("auth.confirmPassword")}</label>
          <input
            id="reg-confirm"
            className="input"
            type="password"
            autoComplete="new-password"
            value={confirm}
            onChange={(event) => setConfirm(event.target.value)}
            disabled={submitting}
          />
        </div>
      </div>

      <div className="auth-error" role="alert" aria-live="polite">
        {error}
      </div>
      <Btn className="auth-submit" type="submit" primary disabled={submitting}>
        {submitting ? (
          <LoaderCircle className="spin" size={16} aria-hidden="true" />
        ) : (
          <UserPlus size={16} aria-hidden="true" />
        )}
        {submitting ? t("auth.registering") : t("auth.createAccount")}
      </Btn>
    </form>
  );
}
