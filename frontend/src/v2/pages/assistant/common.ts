import { useCallback } from "react";
import { useTranslation } from "react-i18next";

import { ApiError, type AssistantProposalStatus, errorMessage } from "../../../lib/api";
import type { TagTone } from "../../ui";

export const PROPOSAL_TONE: Record<AssistantProposalStatus, TagTone> = {
  draft: "orange",
  invalid: "red",
  approved: "green",
  rejected: "gray",
  superseded: "gray",
};

/** Classic chip tones (lib helpers return them) → V2 tag tones. */
export const CHIP_TAG: Record<string, TagTone> = {
  good: "green",
  warn: "orange",
  amber: "orange",
  crit: "red",
  muted: "gray",
};

export const AGENT_TONE: Record<string, TagTone> = {
  deploying: "blue",
  active: "green",
  failed: "red",
};

export const JOB_TONE: Record<string, TagTone> = {
  queued: "gray",
  running: "blue",
  succeeded: "green",
  failed: "red",
};

export const STAGE_TONE: Record<string, TagTone> = {
  pending: "outline",
  running: "blue",
  succeeded: "green",
  skipped: "gray",
  failed: "red",
};

/** API errors in the console's copy (`apiErrors.<code>`), else the raw message. */
export function useApiMessage() {
  const { t } = useTranslation();
  return useCallback(
    (err: unknown) => (err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : errorMessage(err)),
    [t],
  );
}

export const shortId = (id: string | null | undefined, n = 8) => (id ?? "").slice(0, n);

/** Section ids the progress steps scroll to (discussion, resources, proposal ×2). */
export const SECTION_IDS = {
  discussion: "v2-assistant-discussion",
  resources: "v2-assistant-resources",
  proposal: "v2-assistant-proposal",
  evaluation: "v2-assistant-evaluation",
} as const;
export const STEP_TARGETS = [SECTION_IDS.discussion, SECTION_IDS.resources, SECTION_IDS.proposal, SECTION_IDS.proposal];

export function scrollToSection(id: string) {
  document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" });
}

