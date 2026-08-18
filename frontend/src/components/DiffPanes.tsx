import type { CSSProperties } from "react";

// Side-by-side before/after panes; the after pane goes green when it differs
// (agentxray DiffView pattern — panel diff, deliberately not token-level).
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
  const pane: CSSProperties = {
    maxHeight: 180,
    overflow: "auto",
    whiteSpace: "pre-wrap",
    fontSize: 10.5,
    margin: 0,
  };
  return (
    <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: 8 }}>
      <div>
        <div className="mono dim" style={{ fontSize: 9.5, marginBottom: 3 }}>
          {beforeLabel}
        </div>
        <pre className="code" style={pane}>
          {before || "—"}
        </pre>
      </div>
      <div>
        <div
          className="mono"
          style={{
            fontSize: 9.5,
            marginBottom: 3,
            display: "flex",
            justifyContent: "space-between",
            color: changed ? "var(--good)" : undefined,
          }}
        >
          <span>{afterLabel}</span>
          {changed && <span>CHANGED</span>}
        </div>
        <pre
          className="code"
          style={{ ...pane, border: changed ? "1px solid rgba(63,185,80,.4)" : undefined }}
        >
          {after || "—"}
        </pre>
      </div>
    </div>
  );
}
