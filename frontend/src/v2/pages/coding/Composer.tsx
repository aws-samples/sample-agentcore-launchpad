import { CircleStop, SendHorizontal } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import type { BudgetRefusalCode } from "../../../lib/budgets";
import { BudgetRefusalLink } from "../../BudgetBanner";
import { Alert, Button, Select } from "../../ui";

/** The runtime refuses prompts above 32 KB (UTF-8). */
export const PROMPT_MAX_BYTES = 32 * 1024;

export interface ComposerError {
  text: string;
  budget?: BudgetRefusalCode | null;
}

/**
 * The prompt box: model and reasoning-effort selects (the runtime's offer), Enter to
 * send (Shift+Enter for a newline, never mid-IME composition), and Interrupt while a
 * turn runs. A start refused for budget shows the refusal with its link.
 */
export function Composer({
  models,
  efforts,
  model,
  effort,
  onModel,
  onEffort,
  running,
  disabled,
  disabledReason,
  sending,
  interrupting,
  error,
  onSend,
  onInterrupt,
}: {
  models: string[];
  efforts: string[];
  model: string;
  effort: string;
  onModel: (model: string) => void;
  onEffort: (effort: string) => void;
  running: boolean;
  disabled: boolean;
  disabledReason?: string | null;
  sending: boolean;
  interrupting: boolean;
  error: ComposerError | null;
  onSend: (prompt: string) => Promise<boolean>;
  onInterrupt: () => void;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const [tooLong, setTooLong] = useState(false);
  const canSend = !disabled && !running && !sending && text.trim().length > 0;

  const send = async () => {
    const prompt = text.trim();
    if (!canSend || !prompt) return;
    if (new TextEncoder().encode(prompt).length > PROMPT_MAX_BYTES) {
      setTooLong(true);
      return;
    }
    setTooLong(false);
    if (await onSend(prompt)) setText("");
  };

  return (
    <div className="v2-coding-composer-wrap">
      {error && (
        <Alert tone="error" action={error.budget ? <BudgetRefusalLink code={error.budget} /> : undefined}>
          {error.text}
        </Alert>
      )}
      {tooLong && <Alert tone="error">{t("v2.coding.composer.tooLong", { max: PROMPT_MAX_BYTES / 1024 })}</Alert>}
      <form
        className="v2-coding-composer"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <textarea
          className="v2-coding-input"
          rows={3}
          value={text}
          disabled={disabled}
          placeholder={disabled && disabledReason ? disabledReason : t("v2.coding.composer.placeholder")}
          aria-label={t("v2.coding.composer.placeholder")}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            // never mid-IME: Safari ends a composition with an Enter whose isComposing is
            // already false but whose keyCode is still 229
            const composing = e.nativeEvent.isComposing || e.nativeEvent.keyCode === 229;
            if (e.key === "Enter" && !e.shiftKey && !composing) {
              e.preventDefault();
              void send();
            }
          }}
          data-testid="v2-coding-prompt"
        />
        <div className="v2-coding-composer-bar">
          <Select
            value={model}
            options={models.map((m) => ({ value: m, label: m }))}
            onChange={onModel}
            disabled={running || sending || models.length === 0}
            mono
            ariaLabel={t("v2.coding.composer.model")}
            className="v2-coding-model"
            testId="v2-coding-model"
          />
          <Select
            value={effort}
            options={efforts.map((e) => ({ value: e, label: t(`v2.coding.effort.${e}`, e) }))}
            onChange={onEffort}
            disabled={running || sending || efforts.length === 0}
            ariaLabel={t("v2.coding.composer.effort")}
            className="v2-coding-effort"
            testId="v2-coding-effort"
          />
          <span className="v2-coding-composer-gap" />
          {running ? (
            <Button kind="danger" size="sm" disabled={interrupting} onClick={onInterrupt} testId="v2-coding-interrupt">
              <CircleStop size={14} aria-hidden="true" />
              {t(interrupting ? "v2.coding.composer.interrupting" : "v2.coding.composer.interrupt")}
            </Button>
          ) : (
            <Button kind="primary" size="sm" type="submit" disabled={!canSend} testId="v2-coding-send">
              <SendHorizontal size={14} aria-hidden="true" />
              {t(sending ? "v2.coding.composer.sending" : "v2.coding.composer.send")}
            </Button>
          )}
        </div>
      </form>
      {running && <div className="v2-coding-composer-hint">{t("v2.coding.composer.hint")}</div>}
    </div>
  );
}
