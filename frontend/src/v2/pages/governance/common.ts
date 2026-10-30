import { copyBrowserText } from "../../../lib/browser-compat";
import type { TFunction } from "i18next";
import { useCallback } from "react";

import type { GovernanceGatewayTarget } from "../../../lib/api";
import { governanceError, governanceStatusLevel } from "../../../lib/governance";
import { useV2Toast } from "../../hooks";
import type { TagTone } from "../../ui";

const LEVEL_TONE = { good: "green", crit: "red", warn: "orange", muted: "gray" } as const;

export function govTone(status: string | null | undefined): TagTone {
  return LEVEL_TONE[governanceStatusLevel(status)];
}

/** Top-level Gateway tabs. The Policy module has its own page (`/v2/policy`, Global structure). */
export type GovernanceTab = "gateways" | "tools";
export const GOVERNANCE_TABS: GovernanceTab[] = ["gateways", "tools"];
export const isGovernanceTab = (value: string | null): value is GovernanceTab =>
  !!value && (GOVERNANCE_TABS as string[]).includes(value);

/** Detail sections of one Gateway (`?view=gateway&gateway=…&section=…`); `policy-engine` is
 *  the attached policy engine and its mode (Global `policyEngineConfiguration`). */
export type GatewaySection = "overview" | "targets" | "rate-limits" | "policy-engine";
export const GATEWAY_SECTIONS: GatewaySection[] = ["overview", "targets", "rate-limits", "policy-engine"];

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

/** Copy text to the clipboard with a toast. */
export function useCopy(t: TFunction) {
  const toast = useV2Toast();
  return useCallback(
    (text: string) => {
      void copyBrowserText(text)
        .then((copied) => { if (copied) toast("success", t("governance.messages.copied")); })
        .catch((error: unknown) => toast("error", governanceError(error)));
    },
    [t, toast],
  );
}
