import { Plus, Trash2, Upload } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate, useSearchParams } from "react-router-dom";

import { api, type V2Dataset, type V2ToolNameRewrite } from "../../../lib/api";
import { datasetErrorText } from "./datasetErrors";
import {
  emptyScenario,
  fromDrafts,
  type ScenarioDraft,
  type SplitChoice,
  toDrafts,
  type TrajectoryMode,
  viewOf,
} from "./scenarioDraft";
import { creatorLabel, fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import { useMayMutate } from "../../ownership";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  Descriptions,
  Field,
  FilterSelect,
  FlowHeader,
  LinkButton,
  Modal,
  Pager,
  SearchInput,
  Select,
  Spin,
  Table,
  Tag,
} from "../../ui";

/** "trace" when the items came from observed trajectories, else "manual". */
function datasetOrigin(ds: V2Dataset): "trace" | "manual" {
  return ds.items.some((i) => (i.metadata as { source?: string } | undefined)?.source === "trace") ? "trace" : "manual";
}

/** N17-obs424: the trajectory names a save / upload stored under the agent's own names —
 *  carried to the detail page in the navigation state, shown there until the next visit. */
interface RewriteState {
  rewrites?: V2ToolNameRewrite[];
}

// ─── list ──────────────────────────────────────────────────────────────────
export function DatasetsTab() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const { data, loading, error, reload } = useLoad(() => api.v2Datasets(), "datasets");
  const [kind, setKind] = useState("");
  const [origin, setOrigin] = useState("");
  const [q, setQ] = useState("");
  const [deleting, setDeleting] = useState<V2Dataset | null>(null);
  const mayMutate = useMayMutate(); // creator-or-admin on edit / delete (server rule `dataset.forbidden`, r13-g)
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);

  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (data?.datasets ?? []).filter((ds) => {
      if (kind && ds.kind !== kind) return false;
      if (origin && datasetOrigin(ds) !== origin) return false;
      return !needle || `${ds.name} ${ds.description}`.toLowerCase().includes(needle);
    });
  }, [data, kind, origin, q]);
  const paged = usePaged(rows, 12);

  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.v2DeleteDataset(deleting.id);
      toast("success", t("v2.datasets.deleted"));
      setDeleting(null);
      reload();
    } catch (err) {
      toast("error", datasetErrorText(t, err));
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<V2Dataset>[] = [
    {
      key: "name",
      title: t("v2.datasets.colName"),
      render: (ds) => (
        <>
          <LinkButton onClick={() => setParams({ tab: "datasets", view: "dataset", id: ds.id })}>{ds.name}</LinkButton>
          {ds.description && <span className="sub clip">{ds.description}</span>}
        </>
      ),
    },
    {
      key: "kind",
      title: t("v2.datasets.colKind"),
      render: (ds) => <Tag tone="outline">{t(`v2.datasets.kind.${ds.kind}`, { defaultValue: ds.kind })}</Tag>,
    },
    { key: "origin", title: t("v2.datasets.colOrigin"), render: (ds) => t(`v2.datasets.origin.${datasetOrigin(ds)}`) },
    { key: "count", title: t("v2.datasets.colCount"), className: "num", render: (ds) => t("v2.datasets.items", { count: ds.item_count }) },
    {
      key: "gt",
      title: t("v2.datasets.colGt"),
      render: (ds) => (ds.has_ground_truth ? <Tag tone="green">{t("v2.common.yes")}</Tag> : <span className="v2-muted">{t("v2.common.no")}</span>),
    },
    {
      key: "cloud",
      title: t("v2.datasets.colCloud"),
      render: (ds) =>
        ds.cloud?.dataset_id ? (
          <Tag tone={ds.cloud.draft_status === "MODIFIED" ? "orange" : "blue"}>
            {ds.cloud.draft_status === "MODIFIED" ? t("v2.datasets.cloudModified") : t("v2.datasets.cloudSynced")}
          </Tag>
        ) : (
          <span className="v2-muted">{t("v2.datasets.cloudLocal")}</span>
        ),
    },
    {
      key: "created",
      title: t("v2.common.createdAt"),
      className: "nowrap",
      render: (ds) => (
        <>
          {fmtTime(ds.created_at)}
          <span className="sub" title={t("v2.common.creator")}>{creatorLabel(ds)}</span>
        </>
      ),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (ds) => (
        <div className="v2-actions">
          <LinkButton onClick={() => setParams({ tab: "datasets", view: "dataset", id: ds.id })}>{t("v2.common.view")}</LinkButton>
          {mayMutate(ds) && (
            <>
              <LinkButton disabled={ds.kind === "simulated"} title={ds.kind === "simulated" ? t("v2.datasets.simulatedHint") : undefined} onClick={() => setParams({ tab: "datasets", view: "dataset-edit", id: ds.id })}>
                {t("v2.common.edit")}
              </LinkButton>
              <LinkButton danger onClick={() => setDeleting(ds)}>
                {t("v2.common.delete")}
              </LinkButton>
            </>
          )}
        </div>
      ),
    },
  ];

  return (
    <Card>
      <div className="v2-toolbar">
        <Button kind="primary" onClick={() => setParams({ tab: "datasets", view: "dataset-new" })} testId="v2-dataset-new">
          <Plus size={14} aria-hidden="true" />
          {t("v2.datasets.new")}
        </Button>
        <Button onClick={() => setUploading(true)} testId="v2-dataset-upload">
          <Upload size={14} aria-hidden="true" />
          {t("v2.datasets.upload.button")}
        </Button>
        <FilterSelect
          label={t("v2.datasets.colKind")}
          value={kind}
          allLabel={t("v2.common.all")}
          onChange={setKind}
          options={["predefined", "legacy", "simulated"].map((k) => ({ value: k, label: t(`v2.datasets.kind.${k}`) }))}
        />
        <FilterSelect
          label={t("v2.datasets.colOrigin")}
          value={origin}
          allLabel={t("v2.common.all")}
          onChange={setOrigin}
          options={["trace", "manual"].map((o) => ({ value: o, label: t(`v2.datasets.origin.${o}`) }))}
        />
        <Button onClick={reload}>{t("v2.common.refresh")}</Button>
        <div className="end">
          <SearchInput value={q} onChange={setQ} placeholder={t("v2.datasets.search")} />
          <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
        </div>
      </div>
      <Table
        columns={columns}
        rows={paged.slice}
        rowKey={(ds) => ds.id}
        loading={loading}
        error={error}
        onRetry={reload}
        empty={t("v2.datasets.empty")}
        testId="v2-datasets-table"
      />
      <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      <Confirm
        open={deleting !== null}
        title={t("v2.datasets.deleteTitle")}
        body={t("v2.datasets.deleteBody", { name: deleting?.name })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => setDeleting(null)}
      />
      {uploading && (
        <UploadDialog
          onClose={() => setUploading(false)}
          onDone={(created) => {
            setUploading(false);
            reload();
            setParams({ tab: "datasets", view: "dataset", id: created.id }, { state: { rewrites: created.tool_name_rewrites ?? [] } satisfies RewriteState });
          }}
        />
      )}
    </Card>
  );
}

/** N17-obs424: create a dataset from a JSONL file (`POST /api/eval/datasets/upload`, one
 *  item per line — the same item shapes the editor writes). */
function UploadDialog({ onClose, onDone }: { onClose: () => void; onDone: (created: V2Dataset) => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const agents = useLoad(() => api.listAgents(), "agents");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [agentId, setAgentId] = useState("");
  const [file, setFile] = useState<{ name: string; text: string; lines: number } | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const pick = async (f: File | undefined) => {
    if (!f) return;
    const text = await f.text();
    setFile({ name: f.name, text, lines: text.split("\n").filter((l) => l.trim()).length });
    if (!name.trim()) setName(f.name.replace(/\.jsonl?$/i, "").slice(0, 64));
    setError(null);
  };
  const submit = async () => {
    if (!file || !name.trim()) return;
    setBusy(true);
    setError(null);
    try {
      const created = await api.v2UploadDataset({ name: name.trim(), description, jsonl: file.text, ...(agentId ? { agent_id: agentId } : {}) });
      toast("success", t("v2.datasets.upload.done", { name: created.name, count: created.item_count }));
      onDone(created);
    } catch (err) {
      setError(datasetErrorText(t, err));
    } finally {
      setBusy(false);
    }
  };
  return (
    <Modal
      open
      title={t("v2.datasets.upload.title")}
      onClose={onClose}
      testId="v2-dataset-upload-dialog"
      footer={
        <>
          <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
          <Button kind="primary" disabled={busy || !file || !name.trim()} onClick={() => void submit()} testId="v2-dataset-upload-confirm">
            {t("v2.datasets.upload.confirm")}
          </Button>
        </>
      }
    >
      <div className="v2-form">
        <Alert>{t("v2.datasets.upload.hint")}</Alert>
        {error && (
          <div data-testid="v2-dataset-upload-error">
            <Alert tone="error">{error}</Alert>
          </div>
        )}
        <Field label={t("v2.datasets.upload.file")} required hint={file ? t("v2.datasets.upload.lines", { name: file.name, count: file.lines }) : undefined}>
          <input type="file" accept=".jsonl,.json,application/jsonl,text/plain" onChange={(e) => void pick(e.target.files?.[0])} data-testid="v2-dataset-upload-file" />
        </Field>
        <Field label={t("v2.datasets.colName")} required>
          <input className="v2-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} data-testid="v2-dataset-upload-name" />
        </Field>
        <Field label={t("v2.datasets.description")}>
          <input className="v2-input" value={description} maxLength={1000} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label={t("v2.datasets.agent")} hint={t("v2.datasets.agentHint")}>
          <Select
            value={agentId}
            onChange={setAgentId}
            placeholder={t("v2.datasets.agentNone")}
            options={(agents.data?.agents ?? []).filter((a) => a.status === "active").map((a) => ({ value: a.id, label: `${a.name} · ${a.method}` }))}
            testId="v2-dataset-upload-agent"
          />
        </Field>
      </div>
    </Modal>
  );
}

/** N17-obs424: "the stored names changed" — the pairs a save or an upload rewrote. */
export function RewriteNotice({ rewrites }: { rewrites: V2ToolNameRewrite[] | undefined }) {
  const { t } = useTranslation();
  if (!rewrites?.length) return null;
  return (
    <div data-testid="v2-dataset-rewrites">
      <Alert>
        {t("v2.datasets.rewritesTitle", { count: rewrites.length })}
        <ul style={{ margin: "4px 0 0", paddingLeft: 18 }}>
          {rewrites.map((r, i) => (
            <li key={i}>
              <span className="mono">{r.scenario_id}</span>: <span className="mono">{r.from}</span> → <span className="mono">{r.to}</span>
            </li>
          ))}
        </ul>
      </Alert>
    </div>
  );
}

// ─── detail (read-only) ────────────────────────────────────────────────────
interface ScenarioRow {
  i: number;
  view: ReturnType<typeof viewOf>;
}

/** Each turn's input and, under it, its expected response (N17-obs424). */
function TurnsCell({ turns }: { turns: { input: string; expected: string }[] }) {
  const { t } = useTranslation();
  return (
    <div className="v2-stack" style={{ gap: 4 }}>
      {turns.map((turn, k) => (
        <div key={k} style={{ fontSize: 13 }}>
          <span className="v2-muted">{t("v2.datasets.turnN", { n: k + 1 })}</span> <span className="clip" title={turn.input}>{turn.input}</span>
          {turn.expected && (
            <div className="sub clip" title={turn.expected}>
              {t("v2.datasets.expectedShort")}
              {turn.expected}
            </div>
          )}
        </div>
      ))}
    </div>
  );
}
export function DatasetDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const mayMutate = useMayMutate();
  const [, setParams] = useSearchParams();
  const { data, loading, error } = useLoad(() => api.v2Datasets(), "datasets");
  const ds = data?.datasets.find((d) => d.id === id) ?? null;
  const location = useLocation();
  const rewrites = (location.state as RewriteState | null)?.rewrites;
  // N17-obs424: every scenario read-only — turns with their expected responses, the
  // assertions, the expected trajectory and the split
  const rows = useMemo(() => (ds?.items ?? []).map((item, i) => ({ i, view: viewOf(item, i) })), [ds]);
  const paged = usePaged(rows, 20);
  // 复制到新数据集: the picked rows' FULL stored items (assertions, every turn, the
  // expected trajectory) — what the row editor cannot re-type
  const [picked, setPicked] = useState<Set<number>>(new Set());
  const [copyName, setCopyName] = useState<string | null>(null);
  const [copying, setCopying] = useState(false);
  const toggle = (i: number) =>
    setPicked((prev) => { const next = new Set(prev); if (next.has(i)) next.delete(i); else next.add(i); return next; });
  const copy = async () => {
    if (!ds || !copyName?.trim()) return;
    setCopying(true);
    try {
      const items = [...picked].sort((a, b) => a - b).map((i) => {
        const item = structuredClone(ds.items[i] ?? {}) as Record<string, unknown>;
        const meta = (item.metadata && typeof item.metadata === "object" ? item.metadata : {}) as Record<string, unknown>;
        item.metadata = { ...meta, copied_from: { dataset_id: ds.id, dataset_name: ds.name, row: i + 1 } };
        return item;
      });
      const created = await api.v2CreateDataset({ name: copyName.trim(), description: t("v2.datasets.copyDescription", { name: ds.name, count: items.length }), locale: ds.locale, items });
      toast("success", t("v2.datasets.copied", { name: created.name, count: items.length }));
      setCopyName(null);
      setPicked(new Set());
      setParams({ tab: "datasets", view: "dataset", id: created.id });
    } catch (err) {
      toast("error", datasetErrorText(t, err));
    } finally {
      setCopying(false);
    }
  };
  if (loading) return <Spin />;
  if (error) return <Alert tone="error">{error}</Alert>;
  if (!ds) return <Alert tone="error">{t("v2.datasets.notFound")}</Alert>;
  return (
    <>
      <FlowHeader
        title={ds.name}
        onBack={() => setParams({ tab: "datasets" })}
        end={
          <>
            <Button onClick={() => navigate(`/v2/eval/tasks?view=new&dataset=${ds.id}`)}>
              {t("v2.datasets.useInTask")}
            </Button>
            <Button
              disabled={ds.kind === "simulated" || picked.size === 0}
              title={picked.size === 0 ? t("v2.datasets.copyPickHint") : undefined}
              onClick={() => setCopyName(`${ds.name}-subset`.slice(0, 64))}
              testId="v2-dataset-copy"
            >
              {t("v2.datasets.copySelected", { count: picked.size })}
            </Button>
            {mayMutate(ds) && (
              <Button kind="primary" disabled={ds.kind === "simulated"} onClick={() => setParams({ tab: "datasets", view: "dataset-edit", id })}>
                {t("v2.common.edit")}
              </Button>
            )}
          </>
        }
      />
      {!mayMutate(ds) && (
        <Alert data-testid="v2-dataset-creator-only">{t("v2.common.creatorOnlyActions", { name: creatorLabel(ds) })}</Alert>
      )}
      <Card title={t("v2.datasets.basic")}>
        <Descriptions
          items={[
            { label: t("v2.datasets.colName"), value: ds.name },
            { label: "ID", value: <span className="mono">{ds.id}</span> },
            { label: t("v2.datasets.colKind"), value: t(`v2.datasets.kind.${ds.kind}`, { defaultValue: ds.kind }) },
            { label: t("v2.datasets.colOrigin"), value: t(`v2.datasets.origin.${datasetOrigin(ds)}`) },
            { label: t("v2.datasets.colCount"), value: t("v2.datasets.items", { count: ds.item_count }) },
            // N17-obs424 (D3-c): the version a run records as the one it replayed
            { label: t("v2.datasets.revision"), value: <span data-testid="v2-dataset-revision">{t("v2.datasets.revisionValue", { n: ds.revision ?? 1 })}</span> },
            { label: t("v2.common.createdAt"), value: fmtTime(ds.created_at) },
            { label: t("v2.common.creator"), value: creatorLabel(ds) },
            { label: t("v2.datasets.description"), value: ds.description || "—" },
            { label: t("v2.datasets.colGt"), value: ds.has_ground_truth ? t("v2.common.yes") : t("v2.common.no") },
            {
              label: t("v2.datasets.split"),
              value: ds.split_counts ? t("v2.datasets.splitCounts", { tuning: ds.split_counts.tuning, held: ds.split_counts.held_out }) : "—",
            },
            { label: t("v2.datasets.agent"), value: ds.agent_id ? <span className="mono">{ds.agent_id}</span> : t("v2.datasets.agentNone") },
          ]}
        />
      </Card>
      <RewriteNotice rewrites={rewrites} />
      <div data-testid="v2-dataset-split-hint">
        <Alert>{t("v2.datasets.splitExplain")}</Alert>
      </div>
      <Card title={t("v2.datasets.records")} sub={t("v2.datasets.items", { count: ds.item_count })}>
        <Table
          columns={[
            {
              key: "pick",
              title: (
                <input
                  type="checkbox"
                  aria-label={t("v2.datasets.pickAll")}
                  checked={rows.length > 0 && picked.size === rows.length}
                  disabled={ds.kind === "simulated"}
                  onChange={(e) => setPicked(e.target.checked ? new Set(rows.map((_, i) => i)) : new Set())}
                  data-testid="v2-dataset-pick-all"
                />
              ),
              width: 36,
              render: (r: ScenarioRow) => (
                <input
                  type="checkbox"
                  aria-label={t("v2.datasets.pickRow", { n: r.i + 1 })}
                  checked={picked.has(r.i)}
                  disabled={ds.kind === "simulated"}
                  onChange={() => toggle(r.i)}
                  data-testid="v2-dataset-pick"
                />
              ),
            },
            {
              key: "scenario",
              title: t("v2.datasets.colScenario"),
              render: (r: ScenarioRow) => (
                <>
                  <span className="mono">{r.view.scenarioId}</span>
                  <span className="sub clip" title={r.view.turns[0]?.input}>{r.view.turns[0]?.input}</span>
                </>
              ),
            },
            {
              key: "turns",
              title: t("v2.datasets.turns"),
              render: (r: ScenarioRow) => <TurnsCell turns={r.view.turns} />,
            },
            {
              key: "assertions",
              title: t("v2.datasets.assertions"),
              render: (r: ScenarioRow) =>
                r.view.assertions.length ? (
                  <span title={r.view.assertions.join("\n")} data-testid="v2-dataset-assertions">{t("v2.datasets.assertionCount", { count: r.view.assertions.length })}</span>
                ) : (
                  <span className="v2-muted">—</span>
                ),
            },
            {
              key: "trajectory",
              title: t("v2.datasets.trajectory"),
              render: (r: ScenarioRow) =>
                r.view.trajectory == null ? (
                  <span className="v2-muted">—</span>
                ) : r.view.trajectory.length === 0 ? (
                  <Tag tone="outline">{t("v2.datasets.trajectoryNone")}</Tag>
                ) : (
                  <span className="v2-row" style={{ gap: 4, flexWrap: "wrap" }}>
                    {r.view.trajectory.map((name, k) => (
                      <Tag key={k} tone="blue">
                        <span className="mono">{name}</span>
                      </Tag>
                    ))}
                  </span>
                ),
            },
            {
              key: "split",
              title: t("v2.datasets.split"),
              render: (r: ScenarioRow) => {
                const split = ds.item_splits?.[r.i];
                return split ? <Tag tone={split === "held_out" ? "orange" : undefined}>{t(`v2.datasets.splitName.${split}`)}</Tag> : <span className="v2-muted">—</span>;
              },
            },
          ]}
          rows={paged.slice}
          rowKey={(r) => String(r.i)}
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      <Modal
        open={copyName !== null}
        title={t("v2.datasets.copyTitle", { count: picked.size })}
        onClose={() => setCopyName(null)}
        testId="v2-dataset-copy-dialog"
        footer={
          <>
            <Button onClick={() => setCopyName(null)}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={copying || !copyName?.trim()} onClick={() => void copy()} testId="v2-dataset-copy-confirm">
              {t("v2.datasets.copyConfirm")}
            </Button>
          </>
        }
      >
        <div className="v2-form">
          <Alert>{t("v2.datasets.copyHint")}</Alert>
          <Field label={t("v2.datasets.colName")} required>
            <input className="v2-input" value={copyName ?? ""} maxLength={64} onChange={(e) => setCopyName(e.target.value)} data-testid="v2-dataset-copy-name" />
          </Field>
        </div>
      </Modal>
    </>
  );
}

// ─── create / edit ─────────────────────────────────────────────────────────
export function DatasetEditor({ id }: { id: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const list = useLoad(() => api.v2Datasets(), "datasets");
  const ds = id ? (list.data?.datasets.find((d) => d.id === id) ?? null) : null;
  const [seeded, setSeeded] = useState(id === null);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [agentId, setAgentId] = useState("");
  const [drafts, setDrafts] = useState<ScenarioDraft[]>(() => toDrafts(null));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const agents = useLoad(() => api.listAgents(), "agents");

  if (!seeded && ds) {
    setSeeded(true);
    setName(ds.name);
    setDescription(ds.description);
    setAgentId(ds.agent_id ?? "");
    setDrafts(toDrafts(ds));
  }

  if (id && list.loading) return <Spin />;
  if (id && !ds) return <Alert tone="error">{list.error ?? t("v2.datasets.notFound")}</Alert>;
  const kind = ds?.kind ?? "predefined";
  const scenarios = kind !== "legacy"; // a legacy prompt dataset keeps one turn per item
  const agentName = (agents.data?.agents ?? []).find((a) => a.id === agentId)?.name;

  const setDraft = (i: number, patch: Partial<ScenarioDraft>) => setDrafts(drafts.map((d, j) => (j === i ? { ...d, ...patch } : d)));
  const setTurn = (i: number, k: number, patch: { input?: string; expected?: string }) =>
    setDraft(i, { turns: drafts[i].turns.map((turn, m) => (m === k ? { ...turn, ...patch } : turn)) });

  const save = async () => {
    const items = fromDrafts(drafts, kind);
    if (!name.trim()) return setError(t("v2.datasets.errName"));
    if (items.length === 0) return setError(t("v2.datasets.errItems"));
    if (items.length > 200) return setError(t("v2.datasets.errTooMany"));
    setSaving(true);
    setError(null);
    try {
      // N17-obs407 / obs424: the linked agent's tool names gate (and canonicalise) the
      // trajectories — the detail page lists what was rewritten
      const out = id
        ? await api.v2UpdateDataset(id, { name: name.trim(), description, items, agent_id: agentId })
        : await api.v2CreateDataset({ name: name.trim(), description, items, ...(agentId ? { agent_id: agentId } : {}) });
      toast("success", t(id ? "v2.datasets.saved" : "v2.datasets.created"));
      setParams({ tab: "datasets", view: "dataset", id: out.id }, { state: { rewrites: out.tool_name_rewrites ?? [] } satisfies RewriteState });
    } catch (err) {
      setError(datasetErrorText(t, err));
    } finally {
      setSaving(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={id ? t("v2.datasets.editTitle") : t("v2.datasets.newTitle")}
        onBack={() => setParams(id ? { tab: "datasets", view: "dataset", id } : { tab: "datasets" })}
        end={
          <Button kind="primary" disabled={saving} onClick={() => void save()} testId="v2-dataset-save">
            {id ? t("v2.common.save") : t("v2.common.create")}
          </Button>
        }
      />
      {error && (
        <div data-testid="v2-dataset-error">
          <Alert tone="error">{error}</Alert>
        </div>
      )}
      <Card title={t("v2.datasets.basic")}>
        <div className="v2-form cols-2">
          <Field label={t("v2.datasets.colName")} required>
            <input className="v2-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} data-testid="v2-dataset-name" />
          </Field>
          <Field label={t("v2.datasets.colKind")} hint={t("v2.datasets.kindHint")}>
            <input className="v2-input" disabled value={t(`v2.datasets.kind.${kind}`, { defaultValue: kind })} />
          </Field>
          <Field label={t("v2.datasets.agent")} hint={t("v2.datasets.agentHint")}>
            <Select
              value={agentId}
              onChange={setAgentId}
              placeholder={t("v2.datasets.agentNone")}
              options={(agents.data?.agents ?? []).filter((a) => a.status === "active").map((a) => ({ value: a.id, label: `${a.name} · ${a.method}` }))}
              testId="v2-dataset-agent"
            />
          </Field>
          {ds && (
            <Field label={t("v2.datasets.revision")} hint={t("v2.datasets.revisionHint")}>
              <input className="v2-input" disabled value={t("v2.datasets.revisionValue", { n: ds.revision ?? 1 })} />
            </Field>
          )}
          <Field label={t("v2.datasets.description")} full>
            <textarea className="v2-textarea" rows={2} value={description} maxLength={1000} onChange={(e) => setDescription(e.target.value)} />
          </Field>
        </div>
      </Card>
      <Card
        title={t("v2.datasets.records")}
        sub={t(scenarios ? "v2.datasets.scenariosSub" : "v2.datasets.recordsSub")}
        end={
          <Button size="sm" disabled={drafts.length >= 200} onClick={() => setDrafts([...drafts, emptyScenario(drafts.length + 1)])} testId="v2-dataset-add-scenario">
            <Plus size={13} aria-hidden="true" />
            {t("v2.datasets.addRow")}
          </Button>
        }
      >
        <div className="v2-stack">
          {drafts.map((d, i) => (
            <div key={i} className="v2-card-inset" style={{ border: "1px solid var(--v2-line)", borderRadius: 8, padding: 12 }} data-testid={`v2-dataset-scenario-${i}`}>
              <div className="v2-row" style={{ justifyContent: "space-between", marginBottom: 8 }}>
                <span className="v2-muted">{t("v2.datasets.scenarioN", { n: i + 1 })}</span>
                <Button size="sm" title={t("v2.common.delete")} disabled={drafts.length <= 1} onClick={() => setDrafts(drafts.filter((_, j) => j !== i))} testId={`v2-dataset-scenario-delete-${i}`}>
                  <Trash2 size={13} aria-hidden="true" />
                </Button>
              </div>
              <div className="v2-form cols-2">
                {scenarios && (
                  <Field label={t("v2.datasets.scenarioId")} required>
                    <input className="v2-input mono" value={d.scenarioId} maxLength={128} onChange={(e) => setDraft(i, { scenarioId: e.target.value })} data-testid={`v2-dataset-scenario-id-${i}`} />
                  </Field>
                )}
                <Field label={t("v2.datasets.split")} hint={t("v2.datasets.splitHint")}>
                  <Select
                    value={d.split}
                    onChange={(v) => setDraft(i, { split: v as SplitChoice })}
                    testId={`v2-dataset-split-${i}`}
                    options={[
                      { value: "", label: t("v2.datasets.splitDefault") },
                      { value: "tuning", label: t("v2.datasets.splitName.tuning") },
                      { value: "held_out", label: t("v2.datasets.splitName.held_out") },
                    ]}
                  />
                </Field>
              </div>
              <div className="v2-stack" style={{ marginTop: 8 }}>
                {d.turns.map((turn, k) => (
                  <div key={k} className="v2-row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
                    <span className="v2-muted" style={{ width: 56, paddingTop: 6 }}>
                      {t("v2.datasets.turnN", { n: k + 1 })}
                    </span>
                    <textarea
                      className="v2-textarea"
                      style={{ minHeight: 56 }}
                      rows={2}
                      placeholder={t("v2.datasets.inputPlaceholder")}
                      value={turn.input}
                      onChange={(e) => setTurn(i, k, { input: e.target.value })}
                      aria-label={t("v2.datasets.turnInputLabel", { scenario: i + 1, n: k + 1 })}
                      data-testid={`v2-dataset-input-${i}-${k}`}
                    />
                    <textarea
                      className="v2-textarea"
                      style={{ minHeight: 56 }}
                      rows={2}
                      placeholder={t("v2.datasets.expectedPlaceholder")}
                      value={turn.expected}
                      onChange={(e) => setTurn(i, k, { expected: e.target.value })}
                      aria-label={t("v2.datasets.turnExpectedLabel", { scenario: i + 1, n: k + 1 })}
                      data-testid={`v2-dataset-expected-${i}-${k}`}
                    />
                    {scenarios && (
                      <Button size="sm" title={t("v2.datasets.removeTurn")} disabled={d.turns.length <= 1} onClick={() => setDraft(i, { turns: d.turns.filter((_, m) => m !== k) })} testId={`v2-dataset-turn-delete-${i}-${k}`}>
                        <Trash2 size={13} aria-hidden="true" />
                      </Button>
                    )}
                  </div>
                ))}
                {scenarios && (
                  <div>
                    <Button size="sm" onClick={() => setDraft(i, { turns: [...d.turns, { input: "", expected: "", original: null }] })} testId={`v2-dataset-add-turn-${i}`}>
                      <Plus size={13} aria-hidden="true" />
                      {t("v2.datasets.addTurn")}
                    </Button>
                  </div>
                )}
              </div>
              {scenarios && (
                <div className="v2-form cols-2" style={{ marginTop: 8 }}>
                  <Field label={t("v2.datasets.assertions")} hint={t("v2.datasets.assertionsHint")}>
                    <textarea className="v2-textarea" rows={2} value={d.assertions} onChange={(e) => setDraft(i, { assertions: e.target.value })} data-testid={`v2-dataset-assertions-${i}`} />
                  </Field>
                  <Field
                    label={t("v2.datasets.trajectory")}
                    hint={
                      d.trajectoryMode === "list"
                        ? agentId
                          ? t("v2.datasets.trajectoryChecked", { agent: agentName ?? agentId })
                          : t("v2.datasets.trajectoryUnchecked")
                        : undefined
                    }
                  >
                    <Select
                      value={d.trajectoryMode}
                      onChange={(v) => setDraft(i, { trajectoryMode: v as TrajectoryMode })}
                      testId={`v2-dataset-trajectory-mode-${i}`}
                      options={[
                        { value: "none", label: t("v2.datasets.trajectoryModeNone") },
                        { value: "list", label: t("v2.datasets.trajectoryModeList") },
                      ]}
                    />
                    {d.trajectoryMode === "list" && (
                      <input
                        className="v2-input mono"
                        style={{ marginTop: 6 }}
                        placeholder={t("v2.datasets.trajectoryPlaceholder")}
                        value={d.trajectory}
                        onChange={(e) => setDraft(i, { trajectory: e.target.value })}
                        data-testid={`v2-dataset-trajectory-${i}`}
                      />
                    )}
                  </Field>
                </div>
              )}
            </div>
          ))}
        </div>
      </Card>
    </>
  );
}
