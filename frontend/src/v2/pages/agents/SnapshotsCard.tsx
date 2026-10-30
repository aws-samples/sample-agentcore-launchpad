// The grouped field-level diff of two agent spec snapshots (upstream #277 / #290).
//
// CN (r15-a): upstream's file also holds the agent page's Snapshots card (list, compare
// pick, rollback) — not ported, as the CN agent detail has no V2 twin for it and the
// rollback route is not ported. Only `DiffBody` (and `DiffSide`) live here, under the
// upstream path so `v2/pages/tasks/RunCompare.tsx` imports it verbatim: the evaluation
// compare page renders the agent's configuration change between two runs' versions.
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api, errorMessage, type SpecChange } from "../../../lib/api";
import { Alert, LinkButton, Tag, type TagTone } from "../../ui";
import { canShowFull, changeView, groupChanges, groupLabelKey, type SideView, valueAtPath, valueText } from "./snapshots";

/** A diff side: a snapshot seq, or the agent's current spec. */
export type DiffSide = number | "current";

const KIND_TONE: Record<SpecChange["kind"], TagTone> = { added: "green", removed: "red", changed: "blue" };

/** Lazily reads (and caches, per dialog) the full spec of a diff side — only needed to
 *  show an elided prompt / code body in full. */
function useSideSpecs(agentId: string, currentSpec: Record<string, unknown>) {
  const cache = useRef(new Map<DiffSide, Promise<Record<string, unknown>>>());
  return (side: DiffSide): Promise<Record<string, unknown>> => {
    if (side === "current") return Promise.resolve(currentSpec);
    let hit = cache.current.get(side);
    if (!hit) {
      hit = api.agentSnapshot(agentId, side).then((snap) => snap.spec ?? {});
      // a failed read is not cached, so "Show full text" can be retried
      hit.catch(() => cache.current.delete(side));
      cache.current.set(side, hit);
    }
    return hit;
  };
}

function SidePane({ label, view }: { label: string; view: SideView }) {
  const { t } = useTranslation();
  return (
    <div>
      <div className="v2-snap-side">{label}</div>
      {view.kind === "absent" ? (
        <span className="v2-muted">{t("v2.agents.snapshots.absent")}</span>
      ) : view.kind === "elided" ? (
        <span className="v2-muted">{t("v2.agents.snapshots.elided", { length: view.length.toLocaleString() })}</span>
      ) : (
        <pre className="v2-pre">{view.text}</pre>
      )}
    </div>
  );
}

function ChangeItem({
  change,
  readSide,
}: {
  change: SpecChange;
  readSide: (which: "before" | "after") => Promise<unknown>;
}) {
  const { t } = useTranslation();
  const view = changeView(change);
  const [full, setFull] = useState<{ before: SideView; after: SideView } | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const showFull = async () => {
    if (loading) return;
    setLoading(true);
    setError(null);
    try {
      const [before, after] = await Promise.all([readSide("before"), readSide("after")]);
      const pane = (value: unknown, absent: boolean): SideView =>
        absent ? { kind: "absent" } : { kind: "text", text: valueText(value) };
      setFull({ before: pane(before, change.kind === "added"), after: pane(after, change.kind === "removed") });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="v2-snap-change" data-testid="v2-snapshot-change">
      <div className="v2-row">
        <span className="mono">{change.path}</span>
        <Tag tone={KIND_TONE[change.kind]}>{t(`v2.agents.snapshots.kind.${change.kind}`)}</Tag>
      </div>
      {view.kind === "list" ? (
        <pre className="v2-pre">
          {view.removed.map((item, i) => (
            <span key={`r${i}`} className="v2-snap-line del">{`− ${item}\n`}</span>
          ))}
          {view.added.map((item, i) => (
            <span key={`a${i}`} className="v2-snap-line add">{`+ ${item}\n`}</span>
          ))}
        </pre>
      ) : (
        <>
          <div className="v2-grid-2">
            <SidePane label={t("v2.agents.snapshots.before")} view={full?.before ?? view.before} />
            <SidePane label={t("v2.agents.snapshots.after")} view={full?.after ?? view.after} />
          </div>
          {canShowFull(change) && !full && (
            <div>
              <LinkButton disabled={loading} onClick={() => void showFull()} testId="v2-snapshot-show-full">
                {loading ? t("v2.common.loading") : t("v2.agents.snapshots.showFull")}
              </LinkButton>
            </div>
          )}
          {error && <Alert tone="error">{error}</Alert>}
        </>
      )}
    </div>
  );
}

/** A diff, grouped (prompt, model, tools, …). `from` / `to` say where a full body is read;
 *  `currentSpec` is only read for a `"current"` side. Rendered by the evaluation compare
 *  page (`v2/pages/tasks/RunCompare.tsx`) between two snapshots. */
export function DiffBody({
  agentId,
  currentSpec,
  from,
  to,
  changes,
}: {
  agentId: string;
  currentSpec: Record<string, unknown>;
  from: DiffSide;
  to: DiffSide;
  changes: SpecChange[];
}) {
  const { t } = useTranslation();
  const specOf = useSideSpecs(agentId, currentSpec);
  return (
    <div className="v2-snap-diff" data-testid="v2-snapshot-diff-body">
      {groupChanges(changes).map(({ group, changes: items }) => (
        <section key={group}>
          <h3 className="v2-sub-title">
            {t(groupLabelKey(group))} <span className="v2-count">{items.length}</span>
          </h3>
          {items.map((change) => (
            <ChangeItem
              key={change.path}
              change={change}
              readSide={(which) =>
                specOf(which === "before" ? from : to).then((spec) => valueAtPath(spec, change.path))
              }
            />
          ))}
        </section>
      ))}
    </div>
  );
}
