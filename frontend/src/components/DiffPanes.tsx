import type { CSSProperties } from "react";
import { useMemo } from "react";

/** One aligned row of the split view: a replacement pairs the k-th removed
 * line with the k-th added line; the shorter side pads with `null`. */
type DiffRow = { left: string | null; right: string | null; same: boolean };

/**
 * Line-level LCS pairing. O(n·m) time/space over line counts — the inputs
 * here are SKILL.md files and prompt texts, not source trees; oversized
 * inputs fall back to positional pairing in the component below.
 */
function buildRows(before: string[], after: string[]): DiffRow[] {
  const n = before.length;
  const m = after.length;
  const width = m + 1;
  const lcs = new Uint32Array((n + 1) * width);
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      lcs[i * width + j] =
        before[i] === after[j]
          ? lcs[(i + 1) * width + j + 1] + 1
          : Math.max(lcs[(i + 1) * width + j], lcs[i * width + j + 1]);
    }
  }
  const rows: DiffRow[] = [];
  let removed: string[] = [];
  let added: string[] = [];
  const flush = () => {
    for (let k = 0; k < Math.max(removed.length, added.length); k++) {
      rows.push({ left: removed[k] ?? null, right: added[k] ?? null, same: false });
    }
    removed = [];
    added = [];
  };
  let i = 0;
  let j = 0;
  while (i < n && j < m) {
    if (before[i] === after[j]) {
      flush();
      rows.push({ left: before[i], right: after[j], same: true });
      i++;
      j++;
    } else if (lcs[(i + 1) * width + j] >= lcs[i * width + j + 1]) {
      removed.push(before[i++]);
    } else {
      added.push(after[j++]);
    }
  }
  while (i < n) removed.push(before[i++]);
  while (j < m) added.push(after[j++]);
  flush();
  return rows;
}

const REMOVED_BG = "rgba(208,59,59,.14)";
const ADDED_BG = "rgba(12,163,12,.14)";
const VOID_BG = "rgba(255,255,255,.03)";

// Side-by-side line diff (LCS-aligned rows, one shared scroll container so the
// panes cannot drift apart). Line-level on purpose — not token-level.
export function DiffPanes({
  before,
  after,
  beforeLabel,
  afterLabel,
}: {
  before: string;
  after: string;
  beforeLabel: string;
  afterLabel: string;
}) {
  const changed = before.trim() !== after.trim();
  const rows = useMemo(() => {
    const b = (before || "").split("\n");
    const a = (after || "").split("\n");
    if (b.length * a.length > 2_000_000) {
      // Too big for the DP table — degrade to positional pairing.
      return Array.from({ length: Math.max(b.length, a.length) }, (_, k) => ({
        left: b[k] ?? null,
        right: a[k] ?? null,
        same: b[k] === a[k],
      }));
    }
    return buildRows(b, a);
  }, [before, after]);

  const numCell: CSSProperties = {
    padding: "0 4px",
    textAlign: "right",
    fontSize: 9,
    color: "var(--line-2)",
    userSelect: "none",
  };
  const textCell: CSSProperties = {
    padding: "0 6px",
    whiteSpace: "pre-wrap",
    overflowWrap: "anywhere",
    borderLeft: "1px solid var(--line)",
  };

  let leftNo = 0;
  let rightNo = 0;
  return (
    <div>
      <div
        className="mono"
        style={{
          display: "grid",
          gridTemplateColumns: "1fr 1fr",
          gap: 8,
          fontSize: 9.5,
          marginBottom: 3,
        }}
      >
        <span className="dim">{beforeLabel}</span>
        <span
          style={{
            display: "flex",
            justifyContent: "space-between",
            color: changed ? "var(--good)" : undefined,
          }}
        >
          <span>{afterLabel}</span>
          {changed && <span>CHANGED</span>}
        </span>
      </div>
      <div
        className="code mono"
        style={{
          maxHeight: 300,
          overflow: "auto",
          fontSize: 10.5,
          lineHeight: 1.5,
          border: changed ? "1px solid rgba(63,185,80,.4)" : undefined,
        }}
      >
        <div style={{ display: "grid", gridTemplateColumns: "30px 1fr 30px 1fr" }}>
          {rows.map((row, idx) => {
            const l = row.left === null ? null : ++leftNo;
            const r = row.right === null ? null : ++rightNo;
            const leftBg = row.same ? undefined : row.left === null ? VOID_BG : REMOVED_BG;
            const rightBg = row.same ? undefined : row.right === null ? VOID_BG : ADDED_BG;
            return (
              // A fragment keyed per row keeps the four cells on one grid row.
              <div key={idx} style={{ display: "contents" }}>
                <span style={{ ...numCell, background: leftBg }}>{l ?? ""}</span>
                <span style={{ ...textCell, background: leftBg }}>{row.left ?? ""}</span>
                <span style={{ ...numCell, background: rightBg }}>{r ?? ""}</span>
                <span style={{ ...textCell, background: rightBg }}>{row.right ?? ""}</span>
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}
