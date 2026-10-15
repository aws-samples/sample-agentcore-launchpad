// The spend-budget notices of the V2 shell: a banner once the signed-in person has used
// 80 % of their monthly budget (or reached it), and the link a budget refusal shows
// next to its message. Both read the shared My usage cache (myUsage.ts).
import { X } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useLocation } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import {
  BUDGET_EXCEEDED,
  BUDGET_MODEL_UNPRICED,
  type BudgetRefusalCode,
  bannerDismissKey,
  bannerLevel,
  fmtPercent,
  fmtResetDate,
  fmtUsd,
} from "../lib/budgets";
import { useMyUsage } from "./myUsage";
import { Alert } from "./ui";

export const MY_USAGE_PATH = "/v2/usage";
const DISMISS_KEY = "launchpad_v2_budget_banner";

function readDismissed(): string[] {
  try {
    const value: unknown = JSON.parse(localStorage.getItem(DISMISS_KEY) ?? "[]");
    return Array.isArray(value) ? value.filter((v): v is string => typeof v === "string") : [];
  } catch {
    return [];
  }
}

/** Under the top bar, on every V2 page but My usage itself. Dismissed per period and
 *  level, so the "reached" banner still shows after the 80 % one was dismissed. */
export function BudgetBanner({ who }: { who: string }) {
  const { t } = useTranslation();
  const location = useLocation();
  const { data } = useMyUsage(who);
  const [dismissed, setDismissed] = useState<string[]>(readDismissed);
  if (!data || location.pathname === MY_USAGE_PATH) return null;
  const level = bannerLevel(data);
  if (!level) return null;
  const key = bannerDismissKey(data.period, level);
  if (dismissed.includes(key)) return null;

  const dismiss = () => {
    // keep only this period's keys: last month's dismissals can never match again
    const next = [...dismissed.filter((k) => k.startsWith(`${data.period}:`)), key];
    setDismissed(next);
    try {
      localStorage.setItem(DISMISS_KEY, JSON.stringify(next));
    } catch {
      // per-browser convenience only
    }
  };

  const values = {
    used: fmtUsd(data.used_usd),
    limit: fmtUsd(data.limit_usd),
    percent: fmtPercent(data.percent_used),
    date: fmtResetDate(data.resets_at),
  };
  return (
    <div className="v2-budget-banner" data-testid={`v2-budget-banner-${level}`}>
      <Alert
        tone={level === "exceeded" ? "error" : "warn"}
        action={
          <span className="v2-row" style={{ gap: 12, marginLeft: "auto" }}>
            <Link className="v2-link" to={MY_USAGE_PATH} data-testid="v2-budget-banner-link">
              {t("v2.usage.open")}
            </Link>
            <button
              type="button"
              className="v2-budget-banner-x"
              onClick={dismiss}
              aria-label={t("v2.usage.dismiss")}
              title={t("v2.usage.dismiss")}
              data-testid="v2-budget-banner-dismiss"
            >
              <X size={14} aria-hidden="true" />
            </button>
          </span>
        }
      >
        {t(level === "exceeded" ? "v2.usage.bannerExceeded" : "v2.usage.bannerWarn", values)}
      </Alert>
    </div>
  );
}

/** Where a budget refusal points: My usage when the limit is reached; the Models page
 *  (admins only — members cannot price a model) when the model has no price. */
export function BudgetRefusalLink({ code }: { code: BudgetRefusalCode }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  if (code === BUDGET_EXCEEDED) {
    return (
      <Link className="v2-link" to={MY_USAGE_PATH} data-testid="v2-budget-refusal-usage">
        {t("v2.usage.open")}
      </Link>
    );
  }
  if (code === BUDGET_MODEL_UNPRICED && isAdmin) {
    return (
      <Link className="v2-link" to="/v2/models?view=prices" data-testid="v2-budget-refusal-models">
        {t("budgetRefusal.openModels")}
      </Link>
    );
  }
  return null;
}
