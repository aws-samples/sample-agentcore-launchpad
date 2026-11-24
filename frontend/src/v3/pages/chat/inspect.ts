import type { ChatTraceInfo, ChatTraceSpan } from "../../../lib/api";

/** The chat inspector's tabs; `?inspect=` holds the open one (absent: the rail is closed). */
export type InspectTab = "trace" | "memory" | "files";

export function inspectTab(raw: string | null): InspectTab | null {
  return raw === "trace" || raw === "memory" || raw === "files" ? raw : null;
}

/** 0 → "0 B", 1536 → "1.5 KB" — the Files tab's sizes. */
export function fileSize(bytes: number): string {
  if (!Number.isFinite(bytes) || bytes < 0) return "—";
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  if (bytes < 1024 * 1024 * 1024) return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
  return `${(bytes / (1024 * 1024 * 1024)).toFixed(1)} GB`;
}

/** The spans the waterfall draws (V2 parity: the first 12). */
export const SPAN_LIMIT = 12;

export interface SpanBar {
  span: ChatTraceSpan;
  /** left edge and width, in % of the drawn spans' total extent */
  left: number;
  width: number;
}

/** The waterfall rows: each span placed on the extent of the drawn spans, at least
 *  0.8 % wide so an instant span stays visible. */
export function spanBars(trace: ChatTraceInfo | null, limit = SPAN_LIMIT): SpanBar[] {
  const spans = trace?.spans.slice(0, limit) ?? [];
  const total = Math.max(...spans.map((s) => (s.start_ms ?? 0) + (s.duration_ms ?? 0)), 1);
  return spans.map((span) => ({
    span,
    left: ((span.start_ms ?? 0) / total) * 100,
    width: Math.max(((span.duration_ms ?? 0) / total) * 100, 0.8),
  }));
}
