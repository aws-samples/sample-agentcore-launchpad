/** Category colours of the Chat trace rail (shared with the vitest). */
export const SPAN_COLOR: Record<string, string> = {
  model: "#1664ff",
  tool: "#00b42a",
  memory: "#ff7d00",
  policy: "#722ed1",
  runtime: "#86909c",
  other: "#c9cdd4",
};

/** How many spans the rail draws; the Observability session detail has the rest. */
export const TRACE_ROWS = 12;
