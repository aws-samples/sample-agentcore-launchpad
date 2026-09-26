import type { UserState, UserStatusFilter } from "../../../lib/api";
import type { TagTone } from "../../ui";

export const USER_STATES: UserState[] = ["pending", "active", "expired", "disabled"];
export const STATUS_FILTERS: UserStatusFilter[] = ["all", ...USER_STATES];

/** Server-side page size of the account list (the backend pages, not the client). */
export const PAGE_SIZE = 25;

/** Grant every approved account the hub workspace unless the admin says otherwise. */
export const DEFAULT_GRANT = "default";

export const STATE_TONE: Record<UserState, TagTone> = {
  pending: "blue",
  active: "green",
  expired: "orange",
  disabled: "gray",
};

/** Upper bound the backend accepts for one extension. */
export const MAX_EXTEND_DAYS = 3650;

export function clampDays(raw: string): number {
  return Math.min(MAX_EXTEND_DAYS, Math.max(1, Math.round(Number(raw)) || 1));
}

export function isStatusFilter(value: string | null): value is UserStatusFilter {
  return value !== null && (STATUS_FILTERS as string[]).includes(value);
}
