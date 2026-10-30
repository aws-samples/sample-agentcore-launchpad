// N17-obs424 / obs318: a dataset write's refusal in the reader's language. The backend's
// message is operator English ("item 3 turn 2: input required"); the codes carry a
// machine-readable `detail` ({reason, item, turn, line} — `evaluation/routers._invalid`;
// {unknown, agent_tools, suggestions} for an unknown tool name), which this states with
// i18n copy. Anything else falls back to the shared `errorMessage` (apiErrors.* copy).
import type { TFunction } from "i18next";

import { ApiError, errorMessage } from "../../../lib/api";

interface InvalidDetail {
  reason?: string;
  item?: number;
  turn?: number;
  line?: number;
  scenario_id?: string;
}

interface UnknownToolDetail {
  unknown?: Record<string, string[]>;
  agent_tools?: string[];
  suggestions?: Record<string, string[]>;
}

const REASONS = new Set([
  "scenario_id_required",
  "scenario_id_duplicate",
  "split_invalid",
  "turns_required",
  "turn_input_required",
  "persona_input_required",
  "persona_profile_required",
  "prompt_required",
  "not_an_object",
  "item_too_large",
]);

export function datasetErrorText(t: TFunction, err: unknown): string {
  if (!(err instanceof ApiError)) return errorMessage(err);
  const detail = (err.detail ?? {}) as InvalidDetail & UnknownToolDetail & { items?: number; max?: number };
  switch (err.code) {
    case "dataset.invalid_item": {
      if (!detail.reason || !REASONS.has(detail.reason)) return errorMessage(err);
      const where = detail.line != null
        ? t("v2.datasets.errors.atLine", { n: detail.line })
        : detail.turn != null
          ? t("v2.datasets.errors.atTurn", { item: detail.item, turn: detail.turn })
          : t("v2.datasets.errors.atItem", { n: detail.item });
      return t("v2.datasets.errors.invalid", { where, reason: t(`v2.datasets.errors.reason.${detail.reason}`, { id: detail.scenario_id ?? "" }) });
    }
    case "dataset.invalid_jsonl":
      return detail.line != null ? t("v2.datasets.errors.jsonl", { n: detail.line }) : errorMessage(err);
    case "dataset.too_many_items":
      return t("v2.datasets.errors.tooMany", { count: detail.items ?? 0, max: detail.max ?? 200 });
    case "dataset.unknown_tool":
    case "run.unknown_tool": {
      const unknown = Object.entries(detail.unknown ?? {})
        .map(([sid, names]) => `${sid}: ${names.join(", ")}`)
        .join("; ");
      const hints = Object.entries(detail.suggestions ?? {})
        .filter(([, close]) => close.length)
        .map(([name, close]) => t("v2.datasets.errors.didYouMean", { name, close: close.join(", ") }))
        .join("; ");
      const tools = (detail.agent_tools ?? []).join(", ");
      return [
        t("v2.datasets.errors.unknownTool", { list: unknown }),
        hints,
        tools ? t("v2.datasets.errors.agentTools", { list: tools }) : t("v2.datasets.errors.agentNoTools"),
      ]
        .filter(Boolean)
        .join(" ");
    }
    default:
      return errorMessage(err);
  }
}
