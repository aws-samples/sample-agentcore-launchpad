import type { PolicySimulateHistoryEvent } from "../../../lib/api";
import { parseArgumentsObject } from "../../../lib/governance";
import { isoFromMs } from "../../../lib/timestamps";

/** One editable row of the simulated session's history (the console form of a
 *  `PolicySimulateHistoryEvent`): the time is "minutes before the request". */
export interface HistoryRow {
  tool: string;
  kind: PolicySimulateHistoryEvent["kind"];
  input: string;
  output: string;
  minutesBefore: string;
}

export const emptyHistoryRow = (tool = ""): HistoryRow => ({
  tool, kind: "response", input: "{}", output: "{}", minutesBefore: "20",
});

/**
 * The `engine.history` of a request at `atMs`, or the index of the first row that does not
 * parse (a tool missing, input / output not a JSON object, minutes not a number ≥ 0).
 */
export function historyEvents(rows: HistoryRow[], atMs: number): PolicySimulateHistoryEvent[] | { invalid: number } {
  const out: PolicySimulateHistoryEvent[] = [];
  for (const [i, row] of rows.entries()) {
    const input = parseArgumentsObject(row.input);
    const output = parseArgumentsObject(row.output);
    const minutes = Number(row.minutesBefore);
    if (!row.tool.trim() || input === null || output === null || !Number.isFinite(minutes) || minutes < 0 || row.minutesBefore.trim() === "") {
      return { invalid: i };
    }
    out.push({ tool: row.tool.trim(), kind: row.kind, input, output, at: isoFromMs(atMs - minutes * 60_000) });
  }
  // the engine replays in time order
  return out.sort((a, b) => (a.at < b.at ? -1 : a.at > b.at ? 1 : 0));
}
