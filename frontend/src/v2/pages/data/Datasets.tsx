import { Plus, Trash2 } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api, errorMessage, type V2Dataset } from "../../../lib/api";
import { fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
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
  Pager,
  SearchInput,
  Spin,
  Table,
  Tag,
} from "../../ui";

type Item = Record<string, unknown>;

/** "trace" when the items came from observed trajectories, else "manual". */
function datasetOrigin(ds: V2Dataset): "trace" | "manual" {
  return ds.items.some((i) => (i.metadata as { source?: string } | undefined)?.source === "trace") ? "trace" : "manual";
}

interface Row {
  input: string;
  expected: string;
  /** turns beyond the first (kept untouched when editing a multi-turn scenario) */
  extraTurns: number;
  original: Item | null;
}

function toRows(ds: V2Dataset | null): Row[] {
  if (!ds) return [{ input: "", expected: "", extraTurns: 0, original: null }];
  return ds.items.map((item) => {
    if (Array.isArray(item.turns)) {
      const turns = item.turns as { input?: unknown; expected_response?: unknown }[];
      const first = turns[0] ?? {};
      return {
        input: String(first.input ?? ""),
        expected: String(first.expected_response ?? ""),
        extraTurns: Math.max(0, turns.length - 1),
        original: item,
      };
    }
    if ("actor_profile" in item) {
      return { input: String(item.input ?? ""), expected: "", extraTurns: 0, original: item };
    }
    return { input: String(item.prompt ?? ""), expected: String(item.expected ?? ""), extraTurns: 0, original: item };
  });
}

function fromRows(rows: Row[], kind: string): Item[] {
  return rows
    .filter((r) => r.input.trim())
    .map((r, i) => {
      if (kind === "legacy") {
        const item: Item = { ...(r.original ?? {}), prompt: r.input };
        if (r.expected.trim()) item.expected = r.expected;
        else delete item.expected;
        return item;
      }
      const original = r.original ?? {};
      const prevTurns = (Array.isArray(original.turns) ? original.turns : []) as Item[];
      const first: Item = { ...(prevTurns[0] ?? {}), input: r.input };
      if (r.expected.trim()) first.expected_response = r.expected;
      else delete first.expected_response;
      return {
        ...original,
        scenario_id: String(original.scenario_id ?? `case-${Date.now().toString(36)}-${i + 1}`),
        turns: [first, ...prevTurns.slice(1)],
      };
    });
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
  const [busy, setBusy] = useState(false);

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
      toast("error", errorMessage(err));
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
    { key: "created", title: t("v2.common.createdAt"), className: "nowrap", render: (ds) => fmtTime(ds.created_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (ds) => (
        <div className="v2-actions">
          <LinkButton onClick={() => setParams({ tab: "datasets", view: "dataset", id: ds.id })}>{t("v2.common.view")}</LinkButton>
          <LinkButton disabled={ds.kind === "simulated"} title={ds.kind === "simulated" ? t("v2.datasets.simulatedHint") : undefined} onClick={() => setParams({ tab: "datasets", view: "dataset-edit", id: ds.id })}>
            {t("v2.common.edit")}
          </LinkButton>
          <LinkButton danger onClick={() => setDeleting(ds)}>
            {t("v2.common.delete")}
          </LinkButton>
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
    </Card>
  );
}

// ─── detail (read-only) ────────────────────────────────────────────────────
export function DatasetDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const [, setParams] = useSearchParams();
  const { data, loading, error } = useLoad(() => api.v2Datasets(), "datasets");
  const ds = data?.datasets.find((d) => d.id === id) ?? null;
  const rows = useMemo(() => toRows(ds), [ds]);
  const paged = usePaged(rows, 20);
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
            <Button disabled={ds.kind === "simulated"} onClick={() => navigate(`/v2/eval/tasks?view=new&dataset=${ds.id}`)}>
              {t("v2.datasets.useInTask")}
            </Button>
            <Button kind="primary" disabled={ds.kind === "simulated"} onClick={() => setParams({ tab: "datasets", view: "dataset-edit", id })}>
              {t("v2.common.edit")}
            </Button>
          </>
        }
      />
      <Card title={t("v2.datasets.basic")}>
        <Descriptions
          items={[
            { label: t("v2.datasets.colName"), value: ds.name },
            { label: "ID", value: <span className="mono">{ds.id}</span> },
            { label: t("v2.datasets.colKind"), value: t(`v2.datasets.kind.${ds.kind}`, { defaultValue: ds.kind }) },
            { label: t("v2.datasets.colOrigin"), value: t(`v2.datasets.origin.${datasetOrigin(ds)}`) },
            { label: t("v2.datasets.colCount"), value: t("v2.datasets.items", { count: ds.item_count }) },
            { label: t("v2.common.createdAt"), value: fmtTime(ds.created_at) },
            { label: t("v2.datasets.description"), value: ds.description || "—" },
            { label: t("v2.datasets.colGt"), value: ds.has_ground_truth ? t("v2.common.yes") : t("v2.common.no") },
          ]}
        />
      </Card>
      <Card title={t("v2.datasets.records")} sub={t("v2.datasets.items", { count: ds.item_count })}>
        <Table
          columns={[
            { key: "n", title: "#", width: 48, render: (r: Row) => rows.indexOf(r) + 1 },
            { key: "input", title: "Input", render: (r: Row) => <span className="clip" title={r.input}>{r.input}</span> },
            {
              key: "expected",
              title: t("v2.datasets.expected"),
              render: (r: Row) => (r.expected ? <span className="clip" title={r.expected}>{r.expected}</span> : <span className="v2-muted">—</span>),
            },
            {
              key: "turns",
              title: t("v2.datasets.turns"),
              render: (r: Row) => (r.extraTurns ? t("v2.datasets.turnCount", { count: r.extraTurns + 1 }) : t("v2.datasets.singleTurn")),
            },
          ]}
          rows={paged.slice}
          rowKey={(r) => String(rows.indexOf(r))}
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
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
  const [rows, setRows] = useState<Row[]>(() => toRows(null));
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);

  if (!seeded && ds) {
    setSeeded(true);
    setName(ds.name);
    setDescription(ds.description);
    setRows(toRows(ds));
  }

  if (id && list.loading) return <Spin />;
  if (id && !ds) return <Alert tone="error">{list.error ?? t("v2.datasets.notFound")}</Alert>;
  const kind = ds?.kind ?? "predefined";

  const setRow = (i: number, patch: Partial<Row>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...patch } : r)));

  const save = async () => {
    const items = fromRows(rows, kind);
    if (!name.trim()) return setError(t("v2.datasets.errName"));
    if (items.length === 0) return setError(t("v2.datasets.errItems"));
    if (items.length > 200) return setError(t("v2.datasets.errTooMany"));
    setSaving(true);
    setError(null);
    try {
      if (id) {
        await api.v2UpdateDataset(id, { name: name.trim(), description, items });
        toast("success", t("v2.datasets.saved"));
        setParams({ tab: "datasets", view: "dataset", id });
      } else {
        const created = await api.v2CreateDataset({ name: name.trim(), description, items });
        toast("success", t("v2.datasets.created"));
        setParams({ tab: "datasets", view: "dataset", id: created.id });
      }
    } catch (err) {
      setError(errorMessage(err));
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
      {error && <Alert tone="error">{error}</Alert>}
      <Card title={t("v2.datasets.basic")}>
        <div className="v2-form cols-2">
          <Field label={t("v2.datasets.colName")} required>
            <input className="v2-input" value={name} maxLength={64} onChange={(e) => setName(e.target.value)} data-testid="v2-dataset-name" />
          </Field>
          <Field label={t("v2.datasets.colKind")} hint={t("v2.datasets.kindHint")}>
            <input className="v2-input" disabled value={t(`v2.datasets.kind.${kind}`, { defaultValue: kind })} />
          </Field>
          <Field label={t("v2.datasets.description")} full>
            <textarea className="v2-textarea" rows={2} value={description} maxLength={1000} onChange={(e) => setDescription(e.target.value)} />
          </Field>
        </div>
      </Card>
      <Card
        title={t("v2.datasets.records")}
        sub={t("v2.datasets.recordsSub")}
        end={
          <Button size="sm" disabled={rows.length >= 200} onClick={() => setRows([...rows, { input: "", expected: "", extraTurns: 0, original: null }])}>
            <Plus size={13} aria-hidden="true" />
            {t("v2.datasets.addRow")}
          </Button>
        }
      >
        <div className="v2-stack">
          {rows.map((r, i) => (
            <div key={i} className="v2-row" style={{ alignItems: "flex-start", flexWrap: "nowrap" }}>
              <span className="v2-muted" style={{ width: 28, paddingTop: 6 }}>
                {i + 1}
              </span>
              <textarea
                className="v2-textarea"
                style={{ minHeight: 56 }}
                rows={2}
                placeholder={t("v2.datasets.inputPlaceholder")}
                value={r.input}
                onChange={(e) => setRow(i, { input: e.target.value })}
                aria-label={`Input ${i + 1}`}
                data-testid={`v2-dataset-input-${i}`}
              />
              <textarea
                className="v2-textarea"
                style={{ minHeight: 56 }}
                rows={2}
                placeholder={t("v2.datasets.expectedPlaceholder")}
                value={r.expected}
                onChange={(e) => setRow(i, { expected: e.target.value })}
                aria-label={`${t("v2.datasets.expected")} ${i + 1}`}
              />
              {r.extraTurns > 0 && <Tag tone="blue">{t("v2.datasets.turnCount", { count: r.extraTurns + 1 })}</Tag>}
              <Button size="sm" title={t("v2.common.delete")} disabled={rows.length <= 1} onClick={() => setRows(rows.filter((_, j) => j !== i))}>
                <Trash2 size={13} aria-hidden="true" />
              </Button>
            </div>
          ))}
        </div>
      </Card>
    </>
  );
}
