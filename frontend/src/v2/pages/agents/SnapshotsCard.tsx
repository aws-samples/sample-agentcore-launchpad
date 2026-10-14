import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { ApiError, api, errorMessage, type SnapshotDiff, type SpecChange, type SpecSnapshotInfo } from "../../../lib/api";
import { creatorLabel, fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, LinkButton, Modal, Pager, Spin, Table, Tag, type TagTone } from "../../ui";
import {
  canShowFull,
  changeView,
  groupChanges,
  groupLabelKey,
  newestSeq,
  noteLabel,
  pickCompare,
  rollbackable,
  SNAPSHOT_TONE,
  snapshotStateKey,
  type SideView,
  togglePick,
  valueAtPath,
  valueText,
} from "./snapshots";

/** A diff side: a snapshot seq, or the agent's current spec. */
type DiffSide = number | "current";

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

/** A diff, grouped (prompt, model, tools, …). `from` / `to` say where a full body is read. */
function DiffBody({
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

function DiffModal({
  agentId,
  currentSpec,
  pair,
  onClose,
}: {
  agentId: string;
  currentSpec: Record<string, unknown>;
  pair: [number, number];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const [from, to] = pair;
  const { data, loading, error } = useLoad<SnapshotDiff>(
    () => api.snapshotDiff(agentId, from, to),
    `snapdiff:${agentId}:${from}:${to}`,
  );
  return (
    <Modal open wide title={t("v2.agents.snapshots.diffTitle", { from, to })} onClose={onClose} testId="v2-snapshot-diff">
      {loading ? (
        <Spin />
      ) : error ? (
        <Alert tone="error">{error}</Alert>
      ) : data && data.changes.length === 0 ? (
        <Alert tone="info">{t("v2.agents.snapshots.noChanges")}</Alert>
      ) : data ? (
        <DiffBody agentId={agentId} currentSpec={currentSpec} from={from} to={to} changes={data.changes} />
      ) : null}
    </Modal>
  );
}

function rollbackError(err: unknown): string {
  const message = errorMessage(err);
  if (err instanceof ApiError && err.code === "snapshot.stale_reference") {
    const ids = (err.detail as { kb_ids?: unknown } | null)?.kb_ids;
    if (Array.isArray(ids) && ids.length) return `${message} (${ids.join(", ")})`;
  }
  return message;
}

/** Danger confirm of a rollback: what changes from the current spec to the target,
 *  and that it is a real redeploy on (possibly shared) AWS. */
function RollbackModal({
  agentId,
  currentSpec,
  target,
  onClose,
  onDone,
}: {
  agentId: string;
  currentSpec: Record<string, unknown>;
  target: SpecSnapshotInfo;
  onClose: () => void;
  onDone: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const seq = target.seq;
  const diff = useLoad<SnapshotDiff>(() => api.snapshotDiff(agentId, null, seq), `snaprollback:${agentId}:${seq}`);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // a second click lands before React re-renders the disabled button
  const inFlight = useRef(false);

  const run = async () => {
    if (inFlight.current) return;
    inFlight.current = true;
    setBusy(true);
    setError(null);
    try {
      await api.rollbackSnapshot(agentId, seq);
      toast("success", t("v2.agents.snapshots.rolledBack", { seq }));
      onDone();
    } catch (err) {
      setError(rollbackError(err));
    } finally {
      inFlight.current = false;
      setBusy(false);
    }
  };

  return (
    <Modal
      open
      wide
      title={t("v2.agents.snapshots.rollbackTitle", { seq })}
      onClose={onClose}
      testId="v2-snapshot-rollback"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button
            kind="danger"
            disabled={busy || diff.loading}
            onClick={() => void run()}
            testId="v2-snapshot-rollback-ok"
          >
            {busy ? t("v2.agents.snapshots.rollingBack") : t("v2.agents.snapshots.rollbackConfirm")}
          </Button>
        </>
      }
    >
      <div className="v2-stack">
        <Alert tone="warn">{t("v2.agents.snapshots.rollbackWarn")}</Alert>
        {error && (
          <div data-testid="v2-snapshot-rollback-error">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        {diff.loading ? (
          <Spin />
        ) : diff.error ? (
          <Alert tone="error">{diff.error}</Alert>
        ) : diff.data && diff.data.changes.length === 0 ? (
          <Alert tone="info">{t("v2.agents.snapshots.rollbackSame", { seq })}</Alert>
        ) : diff.data ? (
          <>
            <span>{t("v2.agents.snapshots.rollbackChanges", { seq })}</span>
            <DiffBody
              agentId={agentId}
              currentSpec={currentSpec}
              from="current"
              to={seq}
              changes={diff.data.changes}
            />
          </>
        ) : null}
      </div>
    </Modal>
  );
}

/**
 * The spec every publish deployed (one snapshot per publish): compare any two field by
 * field, or roll back — re-publish a deployed snapshot through the redeploy path as a
 * new AWS version (never an in-place revert), behind a confirm that shows the diff.
 */
export function SnapshotsCard({
  agentId,
  agentName,
  currentSpec,
  refreshKey,
  canRollback,
  onRolledBack,
}: {
  agentId: string;
  agentName: string;
  /** the agent's stored spec — the "current" side of a rollback diff */
  currentSpec: Record<string, unknown>;
  /** changes whenever the agent (re)publishes, so the list follows */
  refreshKey: string;
  canRollback: boolean;
  onRolledBack: () => void;
}) {
  const { t } = useTranslation();
  const { data, loading, error, reload } = useLoad(
    () => api.agentSnapshots(agentId),
    `snapshots:${agentId}:${refreshKey}`,
  );
  const [picked, setPicked] = useState<number[]>([]);
  const [diff, setDiff] = useState<[number, number] | null>(null);
  const [target, setTarget] = useState<SpecSnapshotInfo | null>(null);

  const rows = data?.snapshots ?? [];
  const newest = newestSeq(rows);
  const paged = usePaged(rows, 10);
  const pair = pickCompare(picked);

  const note = (raw: string | null) => {
    const label = noteLabel(raw);
    if (!label) return "—";
    return "text" in label ? label.text : t(label.key, label.params);
  };

  const columns: Column<SpecSnapshotInfo>[] = [
    {
      key: "pick",
      title: "",
      width: 32,
      render: (r) => (
        <input
          type="checkbox"
          className="v2-snap-pick"
          checked={picked.includes(r.seq)}
          onChange={() => setPicked((prev) => togglePick(prev, r.seq))}
          aria-label={t("v2.agents.snapshots.pick", { seq: r.seq })}
          data-testid={`v2-snapshot-pick-${r.seq}`}
        />
      ),
    },
    { key: "seq", title: "#", render: (r) => <span className="mono">#{r.seq}</span> },
    {
      key: "version",
      title: t("v2.agents.snapshots.colAwsVersion"),
      render: (r) => <span className="mono">{r.aws_version ?? "—"}</span>,
    },
    {
      key: "state",
      title: t("v2.agents.colStatus"),
      render: (r) => <Tag tone={SNAPSHOT_TONE[r.state]}>{t(snapshotStateKey(r.state))}</Tag>,
    },
    { key: "by", title: t("v2.agents.snapshots.by"), render: (r) => creatorLabel(r) },
    { key: "at", title: t("v2.common.createdAt"), className: "nowrap", render: (r) => fmtTime(r.created_at) },
    { key: "note", title: t("v2.agents.snapshots.note"), render: (r) => note(r.note) },
    {
      key: "act",
      title: "",
      className: "right nowrap",
      render: (r) =>
        r.seq === newest ? (
          <span className="v2-muted">{t("v2.agents.snapshots.current")}</span>
        ) : canRollback && rollbackable(r, newest) ? (
          <LinkButton onClick={() => setTarget(r)} testId={`v2-snapshot-rollback-${r.seq}`}>
            {t("v2.agents.snapshots.rollback")}
          </LinkButton>
        ) : null,
    },
  ];

  return (
    <Card
      title={t("v2.agents.snapshots.title")}
      sub={t("v2.agents.snapshots.sub")}
      end={
        <span className="v2-row">
          <Link
            className="v2-link"
            to={`/v2/environments?view=compare&agent=${encodeURIComponent(agentName)}`}
            data-testid="v2-snapshot-compare-envs"
          >
            {t("v2.agents.snapshots.compareAcross")}
          </Link>
          <Button
            size="sm"
            disabled={!pair}
            title={pair ? undefined : t("v2.agents.snapshots.compareHint")}
            onClick={() => pair && setDiff(pair)}
            testId="v2-snapshot-compare"
          >
            {t("v2.agents.snapshots.compare")}
          </Button>
        </span>
      }
      testId="v2-agent-snapshots"
    >
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(r) => String(r.seq)}
        loading={loading && !data}
        error={data ? null : error}
        onRetry={reload}
        empty={t("v2.agents.snapshots.empty")}
        density="dense"
        testId="v2-snapshots-table"
      />
      {paged.pages > 1 && <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />}
      {diff && (
        <DiffModal
          key={diff.join(":")}
          agentId={agentId}
          currentSpec={currentSpec}
          pair={diff}
          onClose={() => setDiff(null)}
        />
      )}
      {target && (
        <RollbackModal
          key={target.seq}
          agentId={agentId}
          currentSpec={currentSpec}
          target={target}
          onClose={() => setTarget(null)}
          onDone={() => {
            setTarget(null);
            setPicked([]);
            onRolledBack();
          }}
        />
      )}
    </Card>
  );
}
