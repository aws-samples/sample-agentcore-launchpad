import { useTranslation } from "react-i18next";

import { Btn } from "./Btn";

interface LoadErrorProps {
  /** Console copy for the failure (already localized — see `errorMessage`). */
  message: string;
  /** Re-issues the fetch. Omitted ⇒ message only (the caller polls anyway). */
  onRetry?: () => void;
  /** Tighter padding for a table cell / panel row instead of a full panel. */
  inline?: boolean;
  "data-testid"?: string;
}

/**
 * The one "… failed · RETRY" block every list surface renders when its load
 * fails. A failed fetch must never fall through to the "create your first …"
 * empty copy — that reads as an empty account, which is the wrong diagnosis
 * when the backend is unreachable. Same look as the Observability block.
 */
export function LoadError({
  message,
  onRetry,
  inline = false,
  "data-testid": testId = "load-error",
}: LoadErrorProps) {
  const { t } = useTranslation();
  return (
    <div className={inline ? "load-error inline" : "load-error"} role="alert" data-testid={testId}>
      <span>{t("common.loadFailed", { msg: message })}</span>
      {onRetry && (
        <Btn onClick={onRetry} data-testid={`${testId}-retry`}>
          {t("common.retry")}
        </Btn>
      )}
    </div>
  );
}
