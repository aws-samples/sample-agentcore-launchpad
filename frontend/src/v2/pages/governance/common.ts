import type { TFunction } from "i18next";
import { useCallback, useEffect, useState } from "react";

import { api, type GovernanceGatewayTarget, type GovernanceOperation } from "../../../lib/api";
import {
  GOVERNANCE_OPERATION_POLL_MS,
  governanceError,
  governanceStatusLevel,
  isOperationPending,
} from "../../../lib/governance";
import { useV2Toast } from "../../hooks";
import type { TagTone } from "../../ui";

const LEVEL_TONE = { good: "green", crit: "red", warn: "orange", muted: "gray" } as const;

export function govTone(status: string | null | undefined): TagTone {
  // the enforcing Gateway mode is a state, not a verdict: blue like "running"
  if (status?.toUpperCase() === "ENFORCE") return "blue";
  return LEVEL_TONE[governanceStatusLevel(status)];
}

/** Detail sections of one Gateway (`?view=gateway&gateway=…&section=…`). */
export type GatewaySection = "overview" | "policies" | "targets" | "rateLimits" | "decisions" | "audit";
export const GATEWAY_SECTIONS: GatewaySection[] = [
  "overview",
  "policies",
  "targets",
  "rateLimits",
  "decisions",
  "audit",
];

/**
 * Short label for a target's `TargetConfiguration` union member. Unknown
 * (protocol, variant) pairs fall back to the raw `protocol/variant` pair
 * (`known: false`, rendered mono) so a future union member stays legible.
 */
export function targetKind(
  t: TFunction,
  exists: (key: string) => boolean,
  kind: GovernanceGatewayTarget["kind"],
): { label: string; known: boolean } {
  if (kind.protocol === "unknown") return { label: t("governance.targetKind.unknown"), known: true };
  const key = `governance.targetKind.${kind.protocol}.${kind.variant ?? "default"}`;
  if (exists(key)) return { label: t(key), known: true };
  return { label: kind.variant ? `${kind.protocol}/${kind.variant}` : kind.protocol, known: false };
}

/**
 * A governance operation accepted by the backend (202 + operation row): polled
 * until it settles, then `onSettled` reloads the page state.
 */
export function useGovernanceOperation(onSettled: () => void) {
  const toast = useV2Toast();
  const [operation, setOperation] = useState<GovernanceOperation | null>(null);
  useEffect(() => {
    if (!operation || !isOperationPending(operation)) return;
    const timer = window.setTimeout(() => {
      api
        .governanceOperation(operation.id)
        .then((next) => {
          setOperation(next);
          if (!isOperationPending(next)) onSettled();
        })
        .catch((error: unknown) => toast("error", governanceError(error)));
    }, GOVERNANCE_OPERATION_POLL_MS);
    return () => window.clearTimeout(timer);
  }, [operation, onSettled, toast]);
  const pending = isOperationPending(operation);
  return { operation, setOperation, pending };
}

/** Copy text to the clipboard with a toast. */
export function useCopy(t: TFunction) {
  const toast = useV2Toast();
  return useCallback(
    (text: string) => {
      void navigator.clipboard
        .writeText(text)
        .then(() => toast("success", t("governance.messages.copied")))
        .catch((error: unknown) => toast("error", governanceError(error)));
    },
    [t, toast],
  );
}
