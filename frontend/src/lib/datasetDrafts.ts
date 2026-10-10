// Evaluation dataset editor drafts — shared by the classic Datasets view and the
// V2 数据中心 dataset editor. Stored items come in three shapes (the server infers
// the dataset kind from them once, at creation): legacy `{prompt, expected}`,
// devguide predefined scenarios `{scenario_id, turns, assertions,
// expected_trajectory}` and user-simulation personas `{scenario_id, actor_profile,
// input, max_turns, assertions}`. Every draft carries the stored keys its form
// cannot edit and re-emits them, so opening an item and saving never strips it.

export type DatasetItem = Record<string, unknown>;

export interface TurnDraft {
  input: string;
  expected_response: string;
}

export interface ScenarioDraft {
  scenario_id: string;
  turns: TurnDraft[];
  assertions: string[];
  expected_trajectory: string; // comma-separated tool names
  // Every other stored key (metadata, description, provenance…) — carried
  // through the form untouched and re-emitted on save, so opening a scenario in
  // the form and saving never strips it.
  extra: Record<string, unknown>;
  // Non-null = the scenario is being edited as raw JSON; the text is the whole
  // item and is saved as-is (the server validates it).
  json: string | null;
  // Set when the stored item has fields the form cannot hold (turn-level keys,
  // structured input…): the item is JSON-only until it fits — the form never
  // gets a chance to drop them.
  jsonOnly?: "turnKeys" | "structuredInput" | "expected";
}

const KNOWN_TURN_KEYS = new Set(["input", "expected_response"]);

/** Why an item cannot be shown in the form without loss, or null when it can. */
function formLossReason(item: DatasetItem): ScenarioDraft["jsonOnly"] | null {
  for (const turn of (item.turns as unknown[] | undefined) ?? []) {
    if (typeof turn !== "object" || turn === null) return "structuredInput";
    const t = turn as Record<string, unknown>;
    if (Object.keys(t).some((k) => !KNOWN_TURN_KEYS.has(k))) return "turnKeys";
    if (typeof t.input !== "string") return "structuredInput";
    if (t.expected_response !== undefined && typeof t.expected_response !== "string") {
      return "expected";
    }
  }
  return null;
}

const KNOWN_SCENARIO_KEYS = new Set(["scenario_id", "turns", "assertions", "expected_trajectory"]);

export class ScenarioJsonError extends Error {
  constructor(
    public index: number,
    public detail: string,
  ) {
    super(detail);
  }
}

/** One stored predefined item → draft (shared by the list mapper and the
 *  JSON-mode "back to form" action). */
export function toDraft(item: DatasetItem, i: number): ScenarioDraft {
  const extra = Object.fromEntries(
    Object.entries(item).filter(([key]) => !KNOWN_SCENARIO_KEYS.has(key)),
  );
  const lossy = formLossReason(item);
  if (lossy) {
    // JSON-only: keep the exact stored document; the form fields below are
    // placeholders and are never emitted (toItems uses `json`).
    return {
      scenario_id: String(item.scenario_id ?? `scenario_${i + 1}`),
      turns: [{ input: "", expected_response: "" }],
      assertions: [],
      expected_trajectory: "",
      extra,
      json: JSON.stringify(item, null, 2),
      jsonOnly: lossy,
    };
  }
  const turns = ((item.turns as Record<string, unknown>[] | undefined) ?? []).map((turn) => ({
    input: String(turn.input ?? ""),
    expected_response: String(turn.expected_response ?? ""),
  }));
  return {
    scenario_id: String(item.scenario_id ?? `scenario_${i + 1}`),
    turns,
    assertions: ((item.assertions as string[] | undefined) ?? []).map(String),
    expected_trajectory: ((item.expected_trajectory as string[] | undefined) ?? []).join(", "),
    extra,
    json: null,
  };
}

export function draftToItem(s: ScenarioDraft): DatasetItem {
  const assertions = s.assertions.map((a) => a.trim()).filter(Boolean);
  const trajectory = s.expected_trajectory
    .split(",")
    .map((x) => x.trim())
    .filter(Boolean);
  return {
    scenario_id: s.scenario_id.trim(),
    turns: s.turns.map((turn) => ({
      input: turn.input,
      ...(turn.expected_response.trim()
        ? { expected_response: turn.expected_response.trim() }
        : {}),
    })),
    ...(assertions.length ? { assertions } : {}),
    ...(trajectory.length ? { expected_trajectory: trajectory } : {}),
    ...s.extra,
  };
}

/** A JSON-mode scenario's text → the item it stands for, or the reason it cannot be one. */
export function parseScenarioJson(text: string): DatasetItem {
  const parsed: unknown = JSON.parse(text);
  if (typeof parsed !== "object" || parsed === null || Array.isArray(parsed)) {
    throw new Error("expected a JSON object");
  }
  return parsed as DatasetItem;
}

export interface TraitDraft {
  key: string;
  value: string;
}

// Devguide user-simulation scenario (actor_profile persona). max_turns stays
// a string in the draft for friction-free number-input editing; parsed on emit.
export interface SimScenarioDraft {
  scenario_id: string;
  scenario_description: string;
  context: string;
  goal: string;
  traits: TraitDraft[];
  input: string;
  max_turns: string;
  assertions: string[];
  // stored keys the persona form does not edit (provenance, metadata…) —
  // carried through and re-emitted so a form save never strips them
  extra?: Record<string, unknown>;
  profile_extra?: Record<string, unknown>;
}

const KNOWN_SIM_KEYS = new Set([
  "scenario_id", "scenario_description", "actor_profile", "input", "max_turns", "assertions",
]);
const KNOWN_PROFILE_KEYS = new Set(["traits", "context", "goal"]);

export const emptyScenario = (index: number): ScenarioDraft => ({
  scenario_id: `scenario_${index}`,
  turns: [{ input: "", expected_response: "" }],
  assertions: [],
  expected_trajectory: "",
  extra: {},
  json: null,
});

export const emptySimScenario = (index: number): SimScenarioDraft => ({
  scenario_id: `persona_${index}`,
  scenario_description: "",
  context: "",
  goal: "",
  traits: [],
  input: "",
  max_turns: "10",
  assertions: [],
});

// 2-persona support sample adapted from the devguide user-simulation examples
// (双语注释:LLM actor 扮演用户,goal 达成或到 max_turns 即停;assertions 是
// simulated 数据集唯一的真值)。
export const SIM_SAMPLE_SCENARIOS = (): SimScenarioDraft[] => [
  {
    scenario_id: "frustrated_laptop_customer",
    scenario_description: "Customer with a cracked laptop screen wants a warranty fix",
    context:
      "You bought a laptop 3 weeks ago and the screen cracked on its own. " +
      "You already restarted it twice and searched the FAQ without luck.",
    goal: "Get a repair or replacement arranged under warranty",
    traits: [
      { key: "expertise", value: "non-technical" },
      { key: "tone", value: "frustrated but polite" },
    ],
    input: "My brand new laptop screen cracked on its own and I need this fixed.",
    max_turns: "8",
    assertions: [
      "The agent acknowledges the customer's frustration",
      "The agent offers a warranty repair or replacement path",
    ],
  },
  {
    scenario_id: "billing_duplicate_charge",
    scenario_description: "Calm customer asking about a duplicate subscription charge",
    context:
      "The same subscription charge appears twice on this month's statement. " +
      "You want an explanation and a refund of the extra charge.",
    goal: "Confirm the duplicate charge and get a refund initiated",
    traits: [
      { key: "expertise", value: "intermediate" },
      { key: "tone", value: "calm" },
    ],
    input: "Hi, I think I was charged twice this month — can you check my invoice?",
    max_turns: "6",
    assertions: ["The agent verifies the charge before promising a refund"],
  },
];

// 3-scenario math sample with ground truth (expected_trajectory names the
// zip template's real `calculator` tool) — sync-ready for dataset runs.
export const SAMPLE_SCENARIOS = (): ScenarioDraft[] => [
  {
    scenario_id: "add_two_numbers",
    turns: [{ input: "What is 17 + 25? Use your calculator tool.", expected_response: "42" }],
    assertions: ["The agent returns the exact sum 42"],
    expected_trajectory: "calculator",
    extra: {},
    json: null,
  },
  {
    scenario_id: "multiply_then_add",
    turns: [
      { input: "Multiply 6 by 7 with your calculator tool.", expected_response: "42" },
      { input: "Now add 8 to that result.", expected_response: "50" },
    ],
    assertions: ["The agent keeps the running result across turns"],
    expected_trajectory: "calculator",
    extra: {},
    json: null,
  },
  {
    scenario_id: "plain_greeting",
    turns: [{ input: "Say hello in one short sentence.", expected_response: "" }],
    assertions: [],
    expected_trajectory: "",
    extra: {},
    json: null,
  },
];

// Any stored item (legacy prompt or devguide scenario) → editor draft.
export function toDrafts(items: DatasetItem[]): ScenarioDraft[] {
  return items.map((item, i) => {
    if ("turns" in item) return toDraft(item, i);
    return {
      scenario_id: `item_${i + 1}`,
      turns: [
        { input: String(item.prompt ?? ""), expected_response: String(item.expected ?? "") },
      ],
      assertions: [],
      expected_trajectory: "",
      // legacy provenance / description keys ride along and are re-emitted
      extra: Object.fromEntries(
        Object.entries(item).filter(([key]) => key !== "prompt" && key !== "expected"),
      ),
      json: null,
    };
  });
}

// Editor drafts → items to store. Legacy datasets keep their shape when the
// content still fits it (kind is immutable server-side). A scenario in JSON
// mode is emitted exactly as typed (ScenarioJsonError on a parse failure).
export function toItems(scenarios: ScenarioDraft[], kind: string): DatasetItem[] {
  const fitsLegacy = scenarios.every(
    (s) =>
      s.json == null &&
      s.turns.length === 1 &&
      !s.assertions.some((a) => a.trim()) &&
      !s.expected_trajectory.trim(),
  );
  if (kind === "legacy" && fitsLegacy) {
    return scenarios.map((s) => ({
      prompt: s.turns[0].input,
      ...(s.turns[0].expected_response.trim()
        ? { expected: s.turns[0].expected_response.trim() }
        : {}),
      ...s.extra,
    }));
  }
  return scenarios.map((s, i) => {
    if (s.json != null) {
      try {
        return parseScenarioJson(s.json);
      } catch (err) {
        throw new ScenarioJsonError(i + 1, err instanceof Error ? err.message : String(err));
      }
    }
    return draftToItem(s);
  });
}

// Stored simulated items (devguide actor_profile shape) → editor drafts.
export function toSimDrafts(items: DatasetItem[]): SimScenarioDraft[] {
  return items
    .filter((item) => "actor_profile" in item)
    .map((item, i) => {
      const profile = (item.actor_profile ?? {}) as Record<string, unknown>;
      const traits = (profile.traits ?? {}) as Record<string, unknown>;
      return {
        scenario_id: String(item.scenario_id ?? `persona_${i + 1}`),
        scenario_description: String(item.scenario_description ?? ""),
        context: String(profile.context ?? ""),
        goal: String(profile.goal ?? ""),
        traits: Object.entries(traits).map(([key, value]) => ({
          key: String(key),
          value: String(value),
        })),
        input: String(item.input ?? ""),
        max_turns: String(item.max_turns ?? 10),
        assertions: ((item.assertions as string[] | undefined) ?? []).map(String),
        extra: Object.fromEntries(
          Object.entries(item).filter(([key]) => !KNOWN_SIM_KEYS.has(key)),
        ),
        profile_extra: Object.fromEntries(
          Object.entries(profile).filter(([key]) => !KNOWN_PROFILE_KEYS.has(key)),
        ),
      };
    });
}

// Sim drafts → devguide user-simulation items. Optional fields only when
// non-empty; max_turns only when it differs from the schema default 10.
export function toSimItems(drafts: SimScenarioDraft[]): DatasetItem[] {
  return drafts.map((s) => {
    const traits = Object.fromEntries(
      s.traits.filter((tr) => tr.key.trim()).map((tr) => [tr.key.trim(), tr.value]),
    );
    const assertions = s.assertions.map((a) => a.trim()).filter(Boolean);
    const maxTurns = Number.parseInt(s.max_turns, 10);
    return {
      scenario_id: s.scenario_id.trim(),
      ...(s.scenario_description.trim()
        ? { scenario_description: s.scenario_description.trim() }
        : {}),
      actor_profile: {
        context: s.context,
        goal: s.goal,
        ...(Object.keys(traits).length ? { traits } : {}),
        ...(s.profile_extra ?? {}),
      },
      input: s.input,
      ...(Number.isFinite(maxTurns) && maxTurns >= 1 && maxTurns !== 10
        ? { max_turns: maxTurns }
        : {}),
      ...(assertions.length ? { assertions } : {}),
      ...(s.extra ?? {}),
    };
  });
}

/** kind=simulated with non-actor items can only come from import — the persona
 *  form cannot represent those rows, so the editor degrades to a warning. */
export function isMixedSimulated(kind: string, items: DatasetItem[]): boolean {
  return kind === "simulated" && items.some((item) => !("actor_profile" in item));
}

export type ImportError = { code: "noScenarios" } | { code: "badLine"; line: number };

/** Pasted import text → items. Whole-document JSON first: `{scenarios:[...]}`, a
 *  bare array, or one object. Legacy JSONL also starts with "{" but fails that
 *  parse on the second line, so it falls through to the per-line branch. */
export function parseDatasetImport(text: string): { items: DatasetItem[]; error: ImportError | null } {
  const trimmed = text.trim();
  if (!trimmed) return { items: [], error: null };
  try {
    const parsed: unknown = JSON.parse(trimmed);
    if (Array.isArray(parsed)) return { items: parsed as DatasetItem[], error: null };
    if (parsed && typeof parsed === "object") {
      const scen = (parsed as { scenarios?: unknown }).scenarios;
      if (Array.isArray(scen)) return { items: scen as DatasetItem[], error: null };
      if (scen !== undefined) return { items: [], error: { code: "noScenarios" } };
      return { items: [parsed as DatasetItem], error: null };
    }
    return { items: [], error: { code: "noScenarios" } };
  } catch {
    /* not a single JSON document — try JSONL */
  }
  const items: DatasetItem[] = [];
  const lines = trimmed.split("\n");
  for (let i = 0; i < lines.length; i++) {
    if (!lines[i].trim()) continue;
    try {
      items.push(JSON.parse(lines[i]) as DatasetItem);
    } catch {
      return { items: [], error: { code: "badLine", line: i + 1 } };
    }
  }
  return { items, error: null };
}
