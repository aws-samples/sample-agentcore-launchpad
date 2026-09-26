import type { TFunction } from "i18next";

import { ApiError, type RegistryRecordOut } from "../../../lib/api";

export type RegistryRecord = RegistryRecordOut;

export function statusLabel(t: TFunction, status: string): string {
  return t(`v2.registry.status.${status}`, { defaultValue: status });
}

export function typeLabel(t: TFunction, type: string): string {
  return t(`v2.registry.type.${type}`, { defaultValue: type });
}

/** 503 `registry.unavailable`: the account has no Registry — a full-page state. */
export function isRegistryUnavailable(err: unknown): err is ApiError {
  return err instanceof ApiError && err.code === "registry.unavailable";
}

/** The staging session behind an inspect expired (410) — re-inspect the source. */
export function isStagingExpired(err: unknown): boolean {
  return err instanceof ApiError && err.code === "registry.staging_expired";
}
