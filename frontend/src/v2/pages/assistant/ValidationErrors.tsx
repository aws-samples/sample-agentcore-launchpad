import { useTranslation } from "react-i18next";

import type { ValidationErrorDetail } from "../../../lib/api";
import { localizedError } from "../../../lib/validation-errors";

/** A list of validation errors in the member's language (N17-obs296): a known rule code
 *  is translated and its English sentence is kept as the collapsed detail; any other
 *  error shows the sentence itself. */
export function ValidationErrorList({ errors, testId }: { errors: ValidationErrorDetail[]; testId?: string }) {
  const { t } = useTranslation();
  return (
    <ul className="v2-list" data-testid={testId}>
      {errors.map((e, i) => {
        const text = localizedError(t, e);
        return (
          <li key={i} style={{ fontSize: 12, wordBreak: "break-word" }} data-code={e.code}>
            {text === null ? (
              <span className="mono">{e.message}</span>
            ) : (
              <>
                <span>{text}</span>
                <details>
                  <summary className="v2-muted" data-testid="v2-assistant-error-detail-toggle">{t("assistantPage.validationError.detail")}</summary>
                  <span className="mono">{e.message}</span>
                </details>
              </>
            )}
          </li>
        );
      })}
    </ul>
  );
}
