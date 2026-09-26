import { Check } from "lucide-react";
import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import type { ConsoleRole, ConsoleUser, UserState } from "../../../lib/api";
import { fmtTime } from "../../format";
import { Tag } from "../../ui";
import { STATE_TONE } from "./common";

export function RoleTag({ role }: { role: ConsoleRole }) {
  const { t } = useTranslation();
  return <Tag tone={role === "admin" ? "blue" : "gray"}>{t(`v2.users.role.${role}`)}</Tag>;
}

export function StateTag({ state }: { state: UserState }) {
  const { t } = useTranslation();
  return (
    <Tag tone={STATE_TONE[state]} dot>
      {t(`v2.users.state.${state}`)}
    </Tag>
  );
}

/** Expiry + days left; a pending account's window only starts on approval. */
export function ValidityCell({ user }: { user: ConsoleUser }) {
  const { t } = useTranslation();
  if (!user.expires_at) {
    return (
      <span className="v2-muted">
        {t(user.state === "pending" ? "usersPage.startsOnApproval" : "usersPage.neverExpires")}
      </span>
    );
  }
  return (
    <>
      {fmtTime(user.expires_at)}
      <span className={`sub${user.state === "active" && (user.days_remaining ?? 0) <= 3 ? " v2-users-soon" : ""}`}>
        {t("usersPage.daysRemaining", { count: user.days_remaining ?? 0 })}
      </span>
    </>
  );
}

/** Toggle chip for a workspace / permission grant (on = granted). */
export function GrantChip({
  on,
  disabled,
  title,
  onClick,
  testId,
  children,
}: {
  on: boolean;
  disabled?: boolean;
  title?: string;
  onClick: () => void;
  testId?: string;
  children: ReactNode;
}) {
  return (
    <button
      type="button"
      className={on ? "v2-users-chip on" : "v2-users-chip"}
      aria-pressed={on}
      disabled={disabled}
      title={title}
      onClick={onClick}
      data-testid={testId}
    >
      {on && <Check size={13} aria-hidden="true" />}
      {children}
    </button>
  );
}
