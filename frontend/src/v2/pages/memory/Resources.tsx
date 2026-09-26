import { Plus } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import {
  api,
  errorMessage,
  type MemoryResourceCreateInput,
  type MemoryResourceDetail,
  type MemoryResourceRow,
  type MemoryResourceStrategy,
  type MemoryResourceUpdateInput,
} from "../../../lib/api";
import {
  CREATE_EXPIRY_MIN,
  DEFAULT_EXPIRY_DAYS,
  DEFAULT_MEMORY_STRATEGIES,
  EDIT_EXPIRY_MIN,
  EXPIRY_MAX,
  expiryValid,
  MEMORY_NAME_RE,
  MEMORY_STRATEGY_KEYS,
  MEMORY_STRATEGY_NAMESPACES,
  type MemoryStrategyKey,
} from "../../../lib/memory";
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
  OptionCard,
  Pager,
  SearchInput,
  Spin,
  Table,
  Tag,
} from "../../ui";
import { isTransient, POLL_MS, shortId, statusTone } from "./common";

/** Why a row cannot be deleted, or null when it can. */
function deleteBlock(t: (k: string) => string, row: MemoryResourceRow | null | undefined): string | null {
  if (!row) return null;
  if (row.is_default) return t("v2.memory.res.defaultProtected");
  if (row.agents.length > 0) return t("memoryPage.resources.inUseHint");
  if ((row.status ?? "").toUpperCase() === "DELETING") return t("v2.memory.res.deleting");
  return null;
}

function UsedBy({ row }: { row: MemoryResourceRow | null | undefined }) {
  const { t } = useTranslation();
  if (!row) return <>—</>;
  if (row.is_default) return <span className="v2-muted">{t("memoryPage.resources.sharedDefault")}</span>;
  if (row.agents.length === 0) return <span className="v2-muted">{t("v2.memory.res.unused")}</span>;
  return (
    <span className="v2-row" style={{ gap: 6 }}>
      {row.agents.map((a) => (
        <Link key={a.id} className="v2-link" to={`/v2/agents?view=detail&id=${encodeURIComponent(a.id)}`}>
          {a.name}
        </Link>
      ))}
    </span>
  );
}

function useDelete(onDone: () => void) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [pending, setPending] = useState<MemoryResourceRow | null>(null);
  const [busy, setBusy] = useState(false);
  const run = async () => {
    if (!pending?.id) return;
    setBusy(true);
    try {
      await api.memoryResourceDelete(pending.id);
      toast("success", t("memoryPage.resources.deleted", { id: pending.id }));
      setPending(null);
      onDone();
    } catch (err) {
      toast("error", t("memoryPage.resources.deleteFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  };
  const dialog = (
    <Confirm
      open={pending !== null}
      title={t("memoryPage.resources.deleteTitle")}
      body={t("memoryPage.resources.deleteBody", { id: pending?.id ?? "" })}
      confirmLabel={t("v2.common.delete")}
      danger
      busy={busy}
      onConfirm={() => void run()}
      onClose={() => setPending(null)}
    />
  );
  return { ask: setPending, busy, dialog };
}

/**
 * Memory resources in this workspace's account/region. The bootstrap memory is
 * the delete-protected default; others become selectable per agent in the
 * Create wizard (`spec.memory.memory_id`). A memory still referenced by agents
 * cannot be deleted.
 */
export function ResourceList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const [tick, setTick] = useState(0);
  const { data, loading, error, reload } = useLoad(() => api.memoryResources(), `memory-resources:${tick}`);
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const del = useDelete(reload);

  const all = useMemo(() => data?.items ?? [], [data]);
  const statuses = useMemo(() => [...new Set(all.map((r) => r.status ?? "").filter(Boolean))].sort(), [all]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return all.filter((r) => {
      if (status && r.status !== status) return false;
      return !needle || `${r.name ?? ""} ${r.id ?? ""}`.toLowerCase().includes(needle);
    });
  }, [all, status, q]);
  const paged = usePaged(rows, 12);

  // CREATING / DELETING settle on their own: follow them
  const transient = all.some((r) => isTransient(r.status));
  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [transient]);

  const open = (row: MemoryResourceRow) => row.id && setParams({ view: "resource", id: row.id });

  const columns: Column<MemoryResourceRow>[] = [
    {
      key: "name",
      title: t("v2.memory.colNameId"),
      render: (row) => (
        <>
          <span className="v2-row" style={{ flexWrap: "nowrap" }}>
            <LinkButton onClick={() => open(row)} testId={`v2-memory-res-${row.id ?? ""}`}>
              <span className="ellipsis" style={{ maxWidth: 240 }} title={row.name ?? ""}>
                {row.name ?? "—"}
              </span>
            </LinkButton>
            {row.is_default && <Tag tone="orange">{t("v2.memory.res.default")}</Tag>}
          </span>
          <span className="sub mono" title={row.arn ?? ""}>
            ID: {row.id ?? "—"}
          </span>
        </>
      ),
    },
    {
      key: "status",
      title: t("v2.memory.colStatus"),
      render: (row) => (
        <Tag tone={statusTone(row.status)} dot>
          {row.status ?? "—"}
        </Tag>
      ),
    },
    { key: "agents", title: t("v2.memory.colUsedBy"), render: (row) => <UsedBy row={row} /> },
    { key: "created", title: t("v2.memory.colCreated"), className: "nowrap", render: (row) => fmtTime(row.created_at) },
    { key: "updated", title: t("v2.memory.colUpdated"), className: "nowrap", render: (row) => fmtTime(row.updated_at) },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) => {
        const block = deleteBlock(t, row);
        return (
          <div className="v2-actions">
            <LinkButton onClick={() => open(row)}>{t("v2.common.view")}</LinkButton>
            <LinkButton
              disabled={!row.id || isTransient(row.status)}
              onClick={() => row.id && setParams({ view: "resource-edit", id: row.id })}
              testId="v2-memory-res-edit"
            >
              {t("v2.common.edit")}
            </LinkButton>
            {/* the bootstrap memory is delete-protected: no delete at all */}
            {!row.is_default && (
              <LinkButton danger disabled={!row.id || block !== null || del.busy} title={block ?? undefined} onClick={() => del.ask(row)} testId="v2-memory-res-delete">
                {t("v2.common.delete")}
              </LinkButton>
            )}
          </div>
        );
      },
    },
  ];

  return (
    <>
      <Alert tone="info">{t("memoryPage.resources.note")}</Alert>
      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload}>{t("v2.common.refresh")}</Button>
          <Button kind="primary" onClick={() => setParams({ view: "resource-new" })} testId="v2-memory-res-new">
            <Plus size={14} aria-hidden="true" />
            {t("v2.memory.res.new")}
          </Button>
          <FilterSelect
            label={t("v2.memory.colStatus")}
            value={status}
            allLabel={t("v2.common.all")}
            onChange={setStatus}
            options={statuses.map((s) => ({ value: s, label: s }))}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.memory.res.search")} />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(row) => row.id ?? row.arn ?? ""}
          loading={loading}
          error={error}
          onRetry={reload}
          empty={all.length ? t("v2.memory.res.noMatch") : t("memoryPage.resources.empty")}
          testId="v2-memory-res-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
      {del.dialog}
    </>
  );
}

function StrategyTable({ strategies }: { strategies: MemoryResourceStrategy[] }) {
  const { t } = useTranslation();
  const columns: Column<MemoryResourceStrategy>[] = [
    {
      key: "name",
      title: t("v2.memory.colStrategy"),
      render: (s) => (
        <>
          <b>{s.name ?? "—"}</b>
          <span className="sub mono">ID: {s.strategy_id ?? "—"}</span>
        </>
      ),
    },
    { key: "type", title: t("v2.memory.colType"), render: (s) => <Tag tone="outline">{s.type ?? "—"}</Tag> },
    {
      key: "status",
      title: t("v2.memory.colStatus"),
      render: (s) => (
        <Tag tone={statusTone(s.status)} dot>
          {s.status ?? "—"}
        </Tag>
      ),
    },
    {
      key: "ns",
      title: t("v2.memory.colNamespace"),
      render: (s) => (
        <div className="v2-stack" style={{ gap: 2 }}>
          {s.namespaces.map((ns) => (
            <span key={ns} className="mono">
              {ns}
            </span>
          ))}
        </div>
      ),
    },
  ];
  return <Table columns={columns} rows={strategies} rowKey={(s) => s.strategy_id ?? s.name ?? ""} empty={t("memoryPage.overview.noStrategies")} />;
}

/** One memory resource: configuration, strategies, namespace keys and who uses it. */
export function ResourceDetail({ id }: { id: string }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const [tick, setTick] = useState(0);
  const detail = useLoad(() => api.memoryResource(id), `memory-resource:${id}:${tick}`);
  // `agents` exists on the list rows only (the detail read is GetMemory)
  const list = useLoad(() => api.memoryResources(), `memory-resources:${tick}`);
  const back = () => setParams({ tab: "resources" });
  const del = useDelete(back);

  const mem = detail.data;
  const row = list.data?.items.find((r) => r.id === id) ?? null;
  const transient = isTransient(mem?.status);
  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(() => setTick((n) => n + 1), POLL_MS);
    return () => window.clearInterval(timer);
  }, [transient]);

  if (!mem) {
    return (
      <>
        <FlowHeader title={id} onBack={back} />
        {detail.error ? (
          <Alert tone="error" action={<LinkButton onClick={detail.reload}>{t("v2.common.retry")}</LinkButton>}>
            {detail.error}
          </Alert>
        ) : (
          <Spin />
        )}
      </>
    );
  }

  const block = row ? deleteBlock(t, row) : mem.is_default ? t("v2.memory.res.defaultProtected") : t("v2.memory.res.agentsUnknown");

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {mem.name ?? id}
            <Tag tone={statusTone(mem.status)} dot>
              {mem.status ?? "—"}
            </Tag>
            {mem.is_default && <Tag tone="orange">{t("v2.memory.res.default")}</Tag>}
          </span>
        }
        onBack={back}
        end={
          <>
            <Button onClick={() => setTick((n) => n + 1)}>{t("v2.common.refresh")}</Button>
            <Button disabled={transient} onClick={() => setParams({ view: "resource-edit", id })} testId="v2-memory-res-edit">
              {t("v2.common.edit")}
            </Button>
            {!mem.is_default && (
              <Button
                kind="danger"
                disabled={block !== null || del.busy || !row}
                title={block ?? undefined}
                onClick={() => row && del.ask(row)}
                testId="v2-memory-res-delete"
              >
                {t("v2.common.delete")}
              </Button>
            )}
          </>
        }
      />
      {mem.failure_reason && <Alert tone="error">{mem.failure_reason}</Alert>}
      {transient && <Alert tone="info">{t("v2.memory.res.transient", { status: mem.status })}</Alert>}
      {mem.is_default && <Alert tone="info">{t("memoryPage.resources.saveBodyDefault")}</Alert>}
      <Card title={t("v2.memory.res.summary")}>
        <Descriptions
          items={[
            { label: t("memoryPage.overview.id"), value: <span className="mono">{mem.id ?? id}</span> },
            {
              label: t("memoryPage.overview.arn"),
              value: (
                <span className="mono" title={mem.arn ?? ""}>
                  {shortId(mem.arn, 22)}
                </span>
              ),
            },
            { label: t("memoryPage.overview.description"), value: mem.description || "—" },
            {
              label: t("memoryPage.overview.expiry"),
              value: mem.event_expiry_days != null ? t("memoryPage.overview.daysValue", { count: mem.event_expiry_days }) : "—",
            },
            {
              label: t("memoryPage.overview.executionRole"),
              value: (
                <span className="mono" title={mem.execution_role_arn ?? ""}>
                  {mem.execution_role_arn ? shortId(mem.execution_role_arn, 18) : "—"}
                </span>
              ),
            },
            { label: t("v2.memory.colUsedBy"), value: <UsedBy row={row} /> },
            { label: t("memoryPage.overview.createdAt"), value: fmtTime(mem.created_at) },
            { label: t("memoryPage.overview.updatedAt"), value: fmtTime(mem.updated_at) },
          ]}
        />
      </Card>
      <Card title={t("memoryPage.overview.strategiesTitle")} sub={t("memoryPage.overview.strategiesSub")}>
        <StrategyTable strategies={mem.strategies} />
      </Card>
      {mem.namespace_keys.length > 0 && (
        <Card title={t("v2.memory.res.nsKeys")}>
          <Table
            columns={[
              { key: "key", title: t("v2.memory.res.nsKey"), render: (k) => <span className="mono">{k.key ?? "—"}</span> },
              {
                key: "values",
                title: t("v2.memory.res.nsValues"),
                render: (k) => <span className="mono">{k.allowed_values?.join(", ") || "—"}</span>,
              },
              { key: "regex", title: t("v2.memory.res.nsRegex"), render: (k) => <span className="mono">{k.regex_pattern || "—"}</span> },
            ]}
            rows={mem.namespace_keys}
            rowKey={(k) => k.key ?? ""}
          />
        </Card>
      )}
      {del.dialog}
    </>
  );
}

/**
 * Create (`id` null) or edit a memory resource. Editing covers the description
 * and the short-term event expiry only (UpdateMemory) — strategies, namespace
 * variables and the execution role are fixed at creation, so they show read-only.
 * Only the fields that actually changed go on the wire.
 */
export function ResourceEditor({ id }: { id: string | null }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const toast = useV2Toast();
  const editing = id !== null;
  const existing = useLoad<MemoryResourceDetail | null>(
    () => (id ? api.memoryResource(id) : Promise.resolve(null)),
    `memory-resource-edit:${id ?? ""}`,
  );
  const list = useLoad(() => (id ? api.memoryResources() : Promise.resolve(null)), `memory-resources-edit:${id ?? ""}`);
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [expiryDays, setExpiryDays] = useState(DEFAULT_EXPIRY_DAYS);
  const [strategies, setStrategies] = useState<MemoryStrategyKey[]>(DEFAULT_MEMORY_STRATEGIES);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmSave, setConfirmSave] = useState(false);

  const detail = existing.data;
  useEffect(() => {
    if (!detail) return;
    setDescription(detail.description ?? "");
    setExpiryDays(detail.event_expiry_days ?? DEFAULT_EXPIRY_DAYS);
  }, [detail]);
  const row = list.data?.items.find((r) => r.id === id) ?? null;

  const back = () => (id ? setParams({ view: "resource", id }) : setParams({ tab: "resources" }));

  // create validation
  const nameErr = !editing && (touched || name) && !MEMORY_NAME_RE.test(name) ? t("memoryPage.resources.nameInvalid") : null;
  const createExpiryOk = expiryValid(expiryDays, CREATE_EXPIRY_MIN);

  // edit diff — validity judged per changed field so a memory created with a
  // 3-day expiry can still have its description edited
  const trimmed = description.trim();
  const descChanged = detail != null && trimmed !== (detail.description ?? "");
  const expiryChanged = detail != null && expiryDays !== detail.event_expiry_days;
  const saveReason: string | null = !editing
    ? null
    : detail == null
      ? t("memoryPage.resources.editLoading")
      : descChanged && trimmed.length === 0
        ? t("memoryPage.resources.editDescriptionRequired")
        : expiryChanged && !expiryValid(expiryDays, EDIT_EXPIRY_MIN)
          ? t("memoryPage.resources.editExpiryInvalid")
          : !descChanged && !expiryChanged
            ? t("memoryPage.resources.editNothingChanged")
            : null;

  const expiryErr = editing
    ? expiryChanged && !expiryValid(expiryDays, EDIT_EXPIRY_MIN)
      ? t("memoryPage.resources.editExpiryInvalid")
      : null
    : !createExpiryOk
      ? t("v2.memory.res.expiryRange", { min: CREATE_EXPIRY_MIN, max: EXPIRY_MAX })
      : null;

  const toggle = (key: MemoryStrategyKey) =>
    // canonical order, whatever the click order — it is the order sent to CreateMemory
    setStrategies((prev) => MEMORY_STRATEGY_KEYS.filter((k) => (k === key ? !prev.includes(k) : prev.includes(k))));

  const create = async () => {
    setTouched(true);
    if (!MEMORY_NAME_RE.test(name) || !createExpiryOk) return;
    const input: MemoryResourceCreateInput = {
      name,
      description: trimmed,
      event_expiry_days: expiryDays,
      strategies: MEMORY_STRATEGY_KEYS.filter((k) => strategies.includes(k)),
    };
    setBusy(true);
    try {
      const created = await api.memoryResourceCreate(input);
      toast("success", t("memoryPage.resources.created", { id: created.id ?? name }));
      if (created.id) setParams({ view: "resource", id: created.id });
      else setParams({ tab: "resources" });
    } catch (err) {
      toast("error", t("memoryPage.resources.createFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  };

  const save = async () => {
    if (!id || saveReason) return;
    const input: MemoryResourceUpdateInput = {};
    if (descChanged) input.description = trimmed;
    if (expiryChanged) input.event_expiry_days = expiryDays;
    setBusy(true);
    try {
      await api.memoryResourceUpdate(id, input);
      toast("success", t("memoryPage.resources.updated", { id }));
      setConfirmSave(false);
      setParams({ view: "resource", id });
    } catch (err) {
      toast("error", t("memoryPage.resources.updateFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  };

  // a shorter expiry window reaches every agent writing to the memory, so the
  // confirm names them instead of blocking the edit
  const saveBody = [
    t("memoryPage.resources.saveBody", { id: id ?? "" }),
    expiryChanged ? t("memoryPage.resources.saveBodyExpiry", { days: expiryDays }) : null,
    detail?.is_default ? t("memoryPage.resources.saveBodyDefault") : null,
    row && row.agents.length > 0
      ? t("memoryPage.resources.saveBodyAgents", { count: row.agents.length, names: row.agents.map((a) => a.name).join(", ") })
      : null,
  ]
    .filter(Boolean)
    .join(" ");

  if (editing && existing.error) {
    return (
      <>
        <FlowHeader title={t("v2.memory.res.editTitle")} onBack={back} />
        <Alert tone="error" action={<LinkButton onClick={existing.reload}>{t("v2.common.retry")}</LinkButton>}>
          {t("memoryPage.resources.editLoadFailed", { id, msg: existing.error })}
        </Alert>
      </>
    );
  }
  if (editing && !detail) {
    return (
      <>
        <FlowHeader title={t("v2.memory.res.editTitle")} onBack={back} />
        <Spin label={t("memoryPage.resources.editLoading")} />
      </>
    );
  }

  return (
    <>
      <FlowHeader
        title={editing ? t("memoryPage.resources.editTitle", { name: detail?.name ?? id ?? "" }) : t("v2.memory.res.new")}
        onBack={back}
        end={
          <>
            {/* field errors already explain themselves; "nothing changed" has no field */}
            {editing && !descChanged && !expiryChanged && <span className="v2-muted v2-memory-reason">{t("memoryPage.resources.editNothingChanged")}</span>}
            <Button onClick={back}>{t("v2.common.cancel")}</Button>
            {editing ? (
              <Button
                kind="primary"
                disabled={saveReason !== null || busy}
                title={saveReason ?? undefined}
                onClick={() => setConfirmSave(true)}
                testId="v2-memory-res-save"
              >
                {busy ? t("memoryPage.resources.saving") : t("v2.common.save")}
              </Button>
            ) : (
              <Button kind="primary" disabled={busy} onClick={() => void create()} testId="v2-memory-res-create">
                {busy ? t("memoryPage.resources.creating") : t("v2.common.create")}
              </Button>
            )}
          </>
        }
      />
      {editing && <Alert tone="info">{t("memoryPage.resources.editSub")}</Alert>}
      <Card title={t("v2.memory.res.basic")}>
        <div className="v2-form cols-2">
          {editing ? (
            <Field label={t("v2.memory.res.name")}>
              <input className="v2-input mono" value={detail?.name ?? id ?? ""} disabled aria-label={t("v2.memory.res.name")} />
            </Field>
          ) : (
            <Field label={t("v2.memory.res.name")} required error={nameErr} hint={t("memoryPage.resources.nameInvalid")}>
              <input
                className="v2-input mono"
                value={name}
                placeholder="team_notes"
                onChange={(e) => setName(e.target.value)}
                onBlur={() => setTouched(true)}
                aria-label={t("v2.memory.res.name")}
                data-testid="v2-memory-res-name"
              />
            </Field>
          )}
          <Field
            label={t("v2.memory.res.expiry")}
            required
            error={expiryErr}
            hint={editing ? t("memoryPage.resources.editExpiryHint") : t("memoryPage.resources.expiryHint")}
          >
            <input
              className="v2-input mono"
              type="number"
              min={editing ? EDIT_EXPIRY_MIN : CREATE_EXPIRY_MIN}
              max={EXPIRY_MAX}
              value={expiryDays}
              disabled={busy}
              onChange={(e) => setExpiryDays(Number(e.target.value))}
              aria-label={t("v2.memory.res.expiry")}
              data-testid="v2-memory-res-expiry"
            />
          </Field>
          <Field
            label={t("memoryPage.overview.description")}
            full
            error={editing && descChanged && trimmed.length === 0 ? t("memoryPage.resources.editDescriptionRequired") : null}
          >
            <textarea
              className="v2-textarea"
              value={description}
              disabled={busy}
              placeholder={t("memoryPage.resources.descriptionPlaceholder")}
              onChange={(e) => setDescription(e.target.value)}
              aria-label={t("memoryPage.overview.description")}
              data-testid="v2-memory-res-desc"
            />
          </Field>
        </div>
      </Card>
      <Card title={t("memoryPage.overview.strategiesTitle")} sub={editing ? t("v2.memory.res.fixedAtCreate") : t("v2.memory.res.strategiesSub")}>
        {editing ? (
          <StrategyTable strategies={detail?.strategies ?? []} />
        ) : (
          <>
            <div className="v2-options">
              {MEMORY_STRATEGY_KEYS.map((key) => (
                <OptionCard
                  key={key}
                  title={t(`v2.memory.strategy.${key}`)}
                  desc={MEMORY_STRATEGY_NAMESPACES[key]}
                  on={strategies.includes(key)}
                  onClick={() => toggle(key)}
                  testId={`v2-memory-strategy-${key}`}
                />
              ))}
            </div>
            <p className="v2-muted v2-memory-hint">{t("memoryPage.resources.strategiesHint")}</p>
            {strategies.length === 0 && <Alert tone="warn">{t("memoryPage.resources.nsPreviewNoStrategies")}</Alert>}
          </>
        )}
      </Card>
      <Confirm
        open={confirmSave}
        title={t("memoryPage.resources.saveTitle", { name: detail?.name ?? id ?? "" })}
        body={saveBody}
        confirmLabel={t("v2.common.save")}
        busy={busy}
        onConfirm={() => void save()}
        onClose={() => setConfirmSave(false)}
      />
    </>
  );
}
