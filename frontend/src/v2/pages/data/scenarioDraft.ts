// N17-obs424: the dataset editor's scenario model — what a scenario (devguide schema) or a
// legacy prompt item looks like while it is edited, and back. Pure, so vitest covers it.
//
// Ground truth lives where the backend reads it: each TURN carries its `expected_response`;
// `assertions`, `expected_trajectory` and `split` belong to the SCENARIO (the trajectory is
// matched against the whole session's tool calls). Every key the editor does not own
// (`metadata`, `launchpad_assets`, a turn's extra keys) is carried through untouched.
import type { V2Dataset } from "../../../lib/api";

type Item = Record<string, unknown>;

export type SplitChoice = "" | "tuning" | "held_out";
/** `none`: no expectation (the field is absent); `list`: these tools, an empty list being
 *  "no tool may be called" (N17-obs376). */
export type TrajectoryMode = "none" | "list";

export interface TurnDraft {
  input: string;
  expected: string;
  original: Item | null;
}

export interface ScenarioDraft {
  scenarioId: string;
  turns: TurnDraft[];
  /** one assertion per line */
  assertions: string;
  trajectoryMode: TrajectoryMode;
  /** tool names, comma or newline separated */
  trajectory: string;
  /** "" = the dataset's deterministic default */
  split: SplitChoice;
  original: Item | null;
}

function text(raw: unknown): string {
  if (raw && typeof raw === "object") {
    const o = raw as { content?: unknown; prompt?: unknown };
    return String(o.content ?? o.prompt ?? "");
  }
  return raw == null ? "" : String(raw);
}

export function emptyScenario(index: number): ScenarioDraft {
  return {
    scenarioId: `case-${index}`,
    turns: [{ input: "", expected: "", original: null }],
    assertions: "",
    trajectoryMode: "none",
    trajectory: "",
    split: "",
    original: null,
  };
}

/** The draft of one stored item (a predefined scenario or a legacy prompt item). */
export function toDraft(item: Item, index: number): ScenarioDraft {
  const split = item.split === "tuning" || item.split === "held_out" ? item.split : "";
  if (Array.isArray(item.turns)) {
    const traj = item.expected_trajectory;
    return {
      scenarioId: String(item.scenario_id ?? `case-${index + 1}`),
      turns: (item.turns as Item[]).map((turn) => ({
        input: text(turn.input),
        expected: turn.expected_response == null ? "" : String(turn.expected_response),
        original: turn,
      })),
      assertions: Array.isArray(item.assertions) ? (item.assertions as unknown[]).map(String).join("\n") : "",
      trajectoryMode: Array.isArray(traj) ? "list" : "none",
      trajectory: Array.isArray(traj) ? (traj as unknown[]).map(String).join(", ") : "",
      split,
      original: item,
    };
  }
  return {
    scenarioId: `item_${index + 1}`,
    turns: [{ input: text(item.prompt), expected: item.expected == null ? "" : String(item.expected), original: null }],
    assertions: "",
    trajectoryMode: "none",
    trajectory: "",
    split,
    original: item,
  };
}

export function toDrafts(ds: V2Dataset | null): ScenarioDraft[] {
  if (!ds || ds.items.length === 0) return [emptyScenario(1)];
  return ds.items.map((item, i) => toDraft(item, i));
}

export function toolNames(raw: string): string[] {
  return raw.split(/[\n,]/).map((s) => s.trim()).filter(Boolean);
}

function lines(raw: string): string[] {
  return raw.split("\n").map((s) => s.trim()).filter(Boolean);
}

/** The item a draft saves as. A legacy dataset keeps its `prompt` / `expected` shape (one
 *  turn; the kind is immutable); a predefined one gets the full scenario. Empty drafts
 *  (no input in any turn) are dropped by {@link fromDrafts}. */
export function fromDraft(draft: ScenarioDraft, kind: string): Item {
  const original: Item = { ...(draft.original ?? {}) };
  const setSplit = (item: Item) => {
    if (draft.split) item.split = draft.split;
    else delete item.split;
    return item;
  };
  if (kind === "legacy") {
    const turn = draft.turns[0] ?? { input: "", expected: "" };
    const item: Item = { ...original, prompt: turn.input };
    if (turn.expected.trim()) item.expected = turn.expected;
    else delete item.expected;
    return setSplit(item);
  }
  const turns = draft.turns
    .filter((t) => t.input.trim())
    .map((t) => {
      const turn: Item = { ...(t.original ?? {}), input: t.input };
      if (t.expected.trim()) turn.expected_response = t.expected;
      else delete turn.expected_response;
      return turn;
    });
  const item: Item = { ...original, scenario_id: draft.scenarioId.trim(), turns };
  const assertions = lines(draft.assertions);
  if (assertions.length) item.assertions = assertions;
  else delete item.assertions;
  if (draft.trajectoryMode === "list") item.expected_trajectory = toolNames(draft.trajectory);
  else delete item.expected_trajectory;
  return setSplit(item);
}

export function fromDrafts(drafts: ScenarioDraft[], kind: string): Item[] {
  return drafts.filter((d) => d.turns.some((t) => t.input.trim())).map((d) => fromDraft(d, kind));
}

/** What the read-only scenario list shows for one stored item. */
export interface ScenarioView {
  scenarioId: string;
  turns: { input: string; expected: string }[];
  assertions: string[];
  /** null = no expectation; [] = no tool may be called */
  trajectory: string[] | null;
}

export function viewOf(item: Item, index: number): ScenarioView {
  const d = toDraft(item, index);
  return {
    scenarioId: "actor_profile" in item ? String(item.scenario_id ?? d.scenarioId) : d.scenarioId,
    turns: "actor_profile" in item ? [{ input: text(item.input), expected: "" }] : d.turns.map(({ input, expected }) => ({ input, expected })),
    assertions: lines(d.assertions),
    trajectory: d.trajectoryMode === "list" ? toolNames(d.trajectory) : null,
  };
}
