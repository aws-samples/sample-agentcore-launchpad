import { useTranslation } from "react-i18next";

import { Btn } from "./Btn";

interface StaleLinkProps {
  /** Localized noun for the linked resource (`staleLink.kind.*`). */
  kind: string;
  /** The id the deep link carried; "" ⇒ the link named no id at all. */
  id: string;
  /** Where the user picks a replacement from — a table below, or a picker (Chat). */
  pickFrom?: "table" | "picker";
  onDismiss: () => void;
  "data-testid"?: string;
}

/**
 * The one notice every deep-linkable surface renders when the linked id no
 * longer resolves (list loaded without it, or the detail fetch answered 4xx).
 * Distinct from `LoadError`: the load SUCCEEDED, the thing is just gone — so
 * the page must say so instead of silently selecting something else. Callers
 * pair it with `useStaleParam`, which also strips the stale param from the URL.
 */
export function StaleLink({
  kind,
  id,
  pickFrom = "table",
  onDismiss,
  "data-testid": testId = "stale-link",
}: StaleLinkProps) {
  const { t } = useTranslation();
  const body = !id
    ? t("staleLink.bodyMissing", { kind })
    : pickFrom === "picker"
      ? t("staleLink.bodyPicker", { kind, id })
      : t("staleLink.body", { kind, id });
  return (
    <div className="note stale-link" role="status" data-testid={testId}>
      <span className="i">!</span>
      <span className="stale-link-body">{body}</span>
      <Btn onClick={onDismiss} aria-label={t("staleLink.dismiss")} data-testid={`${testId}-dismiss`}>
        {t("staleLink.dismiss")}
      </Btn>
    </div>
  );
}
