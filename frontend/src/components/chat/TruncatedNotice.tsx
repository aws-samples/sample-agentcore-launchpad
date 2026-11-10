import { useTranslation } from "react-i18next";

/**
 * N-SCN-bug4 follow-up (r8-a): the console surface of the runtime's `truncated: true`
 * marker. `notice` (chat bubbles) is a one-line inline note under the
 * answer; `badge` (observability transcript turns) is a short tag with the full copy as
 * its tooltip. Renders nothing unless `truncated` is true — the marker is additive and
 * absent on every complete answer.
 */
export function TruncatedNotice({
  truncated,
  variant = "notice",
  className,
}: {
  truncated?: boolean;
  variant?: "notice" | "badge";
  className?: string;
}) {
  const { t } = useTranslation();
  if (!truncated) return null;
  if (variant === "badge") {
    return (
      <span
        className={className}
        title={t("chatPage.truncated")}
        data-testid="turn-truncated"
      >
        {t("chatPage.truncatedBadge")}
      </span>
    );
  }
  return (
    <div className={className} role="note" data-testid="bubble-truncated">
      {t("chatPage.truncated")}
    </div>
  );
}
