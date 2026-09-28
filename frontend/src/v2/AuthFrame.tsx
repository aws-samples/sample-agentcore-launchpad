import "./v2.css";
import "./v2-classic.css";
import "./auth.css";

import { type ReactNode, useEffect } from "react";
import { useTranslation } from "react-i18next";

import { V2Lang } from "./Lang";
import { V2Logo } from "./Logo";

/**
 * The V2 chrome of the pages shown before the console shell: sign-in / register
 * and "no workspace granted". Same top bar as the shell (brand + language), no
 * sidebar, and the V2 light theme on <body> while mounted — so the classic
 * components some of these pages still embed render light too (`.v2-classic`).
 */
export function V2AuthFrame({ end, children, testId, narrow = false }: {
  end?: ReactNode;
  children: ReactNode;
  testId?: string;
  /** a top-aligned single column (a notice page) instead of the centered form */
  narrow?: boolean;
}) {
  const { t } = useTranslation();
  useEffect(() => {
    document.body.classList.add("v2-body");
    return () => document.body.classList.remove("v2-body");
  }, []);
  return (
    <div className="v2 v2-auth" data-testid={testId}>
      <header className="v2-top">
        <span className="v2-brand">
          <V2Logo className="v2-brand-logo" />
          {t("v2.brand")}
          <small>V2</small>
        </span>
        <div className="v2-top-right">
          <V2Lang />
          {end}
        </div>
      </header>
      <main className={narrow ? "v2-auth-main narrow" : "v2-auth-main"}>{children}</main>
    </div>
  );
}

/** Centered spinner on the V2 background while a session / workspace resolves. */
export function V2AuthLoading({ label }: { label: string }) {
  useEffect(() => {
    document.body.classList.add("v2-body");
    return () => document.body.classList.remove("v2-body");
  }, []);
  return (
    <div className="v2 v2-auth-loading" role="status">
      <span className="v2-auth-spinner" aria-hidden="true" />
      <span className="sr-only">{label}</span>
    </div>
  );
}
