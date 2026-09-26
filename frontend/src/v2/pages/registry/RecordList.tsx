import { Network, Plus, Search, Shuffle } from "lucide-react";
import { type FormEvent, useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { api, type DiscoverableRegistryRecord, errorMessage } from "../../../lib/api";
import { REGISTRY_STATUSES, REGISTRY_TYPES } from "../../../lib/registry";
import { fmtTime } from "../../format";
import { useLoad, usePaged, useV2Toast } from "../../hooks";
import {
  Alert,
  Button,
  Card,
  type Column,
  Confirm,
  FilterSelect,
  Kpi,
  LinkButton,
  PageHeader,
  Pager,
  SearchInput,
  SubTabs,
  Table,
  Tag,
} from "../../ui";
import { isRegistryUnavailable, type RegistryRecord, statusLabel, typeLabel } from "./common";
import { StatusTag, SystemTag, TypeTag } from "./tags";

type Tab = "records" | "discoverable";

interface ListData {
  records: RegistryRecord[];
  /** `null` = the data-plane list could not be read; never guess from status */
  discoverable: DiscoverableRegistryRecord[] | null;
  unavailable: string | null;
}

/** Publisher records + (best-effort) the consumer-discoverable set in one load. */
function useRegistryData(unavailableFallback: string) {
  return useLoad<ListData>(async () => {
    const [records, discoverable] = await Promise.allSettled([
      api.registryRecords(),
      api.registryDiscoverable(),
    ]);
    if (records.status === "rejected") {
      if (isRegistryUnavailable(records.reason)) {
        return {
          records: [],
          discoverable: null,
          unavailable: records.reason.message || unavailableFallback,
        };
      }
      throw records.reason;
    }
    return {
      records: records.value.records,
      discoverable: discoverable.status === "fulfilled" ? discoverable.value.records : null,
      unavailable: null,
    };
  }, "registry-records");
}

function RegistryHeader({ tab }: { tab: Tab }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  return (
    <PageHeader
      title={t("nav.registry")}
      desc={t(tab === "records" ? "v2.registry.desc" : "v2.registry.consumerDesc")}
      tabs={
        <SubTabs
          value={tab}
          onChange={(next) => setParams(next === "records" ? {} : { view: "discoverable" })}
          tabs={[
            { value: "records", label: t("v2.registry.tab.records") },
            { value: "discoverable", label: t("v2.registry.tab.discoverable") },
          ]}
        />
      }
    />
  );
}

function Unavailable({ tab, message }: { tab: Tab; message: string }) {
  const { t } = useTranslation();
  return (
    <>
      <RegistryHeader tab={tab} />
      <Card testId="v2-registry-unavailable">
        <Alert tone="error">
          <b>{t("v2.registry.unavailableTitle")}</b>
          <div>{message}</div>
        </Alert>
      </Card>
    </>
  );
}

function NotDiscoverableTag() {
  const { t } = useTranslation();
  return (
    <Tag tone="orange" title={t("registry.consumer.notDiscoverableHint")}>
      {t("v2.registry.notDiscoverable")}
    </Tag>
  );
}

// ─── publisher list ────────────────────────────────────────────────────────
export function RecordList() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useV2Toast();
  const [params, setParams] = useSearchParams();
  const { data, loading, error, reload } = useRegistryData(t("registry.unavailableBody"));
  const typeParam = params.get("type") ?? "";
  const type = (REGISTRY_TYPES as string[]).includes(typeParam) ? typeParam : "";
  const [status, setStatus] = useState("");
  const [q, setQ] = useState("");
  const [searchHits, setSearchHits] = useState<RegistryRecord[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [deleting, setDeleting] = useState<RegistryRecord | null>(null);
  const [busy, setBusy] = useState(false);

  const records = useMemo(() => data?.records ?? [], [data]);
  const discoverableIds = data?.discoverable ? new Set(data.discoverable.map((r) => r.record_id)) : null;
  const isHidden = (r: RegistryRecord) => discoverableIds !== null && !discoverableIds.has(r.record_id);

  const rows = useMemo(() => {
    const needle = searchHits ? "" : q.trim().toLowerCase();
    return (searchHits ?? records).filter((r) => {
      if (type && r.type !== type) return false;
      if (status && r.status !== status) return false;
      if (!needle) return true;
      return `${r.name} ${r.record_id} ${r.description ?? ""}`.toLowerCase().includes(needle);
    });
  }, [records, searchHits, type, status, q]);
  const paged = usePaged(rows, 12);

  const setType = (next: string) => {
    const p = new URLSearchParams(params);
    if (next) p.set("type", next);
    else p.delete("type");
    setParams(p, { replace: true });
  };

  // SearchDiscoverableRegistryRecords — the semantic search the data plane runs;
  // typing alone only filters the loaded list.
  const runSearch = async (e?: FormEvent) => {
    e?.preventDefault();
    if (!q.trim()) {
      setSearchHits(null);
      return;
    }
    setSearching(true);
    try {
      const body = await api.registrySearch(q.trim());
      setSearchHits(body.records);
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setSearching(false);
    }
  };

  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await api.registryDelete(deleting.record_id);
      toast("success", t("registry.deleted", { name: deleting.name }));
      setDeleting(null);
      setSearchHits(null);
      reload();
    } catch (err) {
      toast("error", t("common.actionFailed", { msg: errorMessage(err) }));
    } finally {
      setBusy(false);
    }
  };

  if (data?.unavailable) return <Unavailable tab="records" message={data.unavailable} />;

  const open = (r: RegistryRecord) => setParams({ view: "detail", id: r.record_id });
  const count = (tp: string) => records.filter((r) => r.type === tp).length;
  const pending = records.filter((r) => r.status === "PENDING_APPROVAL").length;
  const hiddenCount = records.filter(isHidden).length;

  const columns: Column<RegistryRecord>[] = [
    {
      key: "name",
      title: t("v2.registry.col.name"),
      render: (r) => (
        <>
          <span className="v2-row" style={{ gap: 6 }} title={r.description || undefined}>
            <LinkButton onClick={() => open(r)} testId={`v2-registry-open-${r.record_id}`}>
              {r.name}
            </LinkButton>
            {r.system && <SystemTag />}
          </span>
          <span className="sub mono">ID: {r.record_id}</span>
        </>
      ),
    },
    { key: "type", title: t("v2.registry.col.type"), render: (r) => <TypeTag type={r.type} /> },
    { key: "version", title: t("v2.registry.col.version"), render: (r) => <span className="mono">{r.version ?? "—"}</span> },
    {
      key: "status",
      title: t("v2.registry.col.status"),
      render: (r) => (
        <span className="v2-tags v2-registry-status">
          <StatusTag status={r.status} />
          {isHidden(r) && <NotDiscoverableTag />}
        </span>
      ),
    },
    { key: "updated", title: t("v2.registry.col.updated"), render: (r) => <span className="nowrap">{fmtTime(r.updated_at)}</span> },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (r) => (
        <div className="v2-actions">
          <LinkButton onClick={() => open(r)}>{t("v2.common.view")}</LinkButton>
          {r.type !== "A2A" && r.status !== "DEPRECATED" && !r.system && (
            <LinkButton onClick={() => setParams({ view: "edit", id: r.record_id })}>{t("v2.common.edit")}</LinkButton>
          )}
          {!r.system && (
            <LinkButton danger onClick={() => setDeleting(r)} testId={`v2-registry-delete-${r.record_id}`}>
              {t("v2.common.delete")}
            </LinkButton>
          )}
        </div>
      ),
    },
  ];

  return (
    <>
      <RegistryHeader tab="records" />
      <div className="v2-kpis">
        <Kpi label={typeLabel(t, "A2A")} value={loading && !data ? "—" : count("A2A")} sub={t("v2.registry.kpi.a2aSub")} />
        <Kpi label={typeLabel(t, "MCP")} value={loading && !data ? "—" : count("MCP")} sub={t("v2.registry.kpi.mcpSub")} />
        <Kpi
          label={typeLabel(t, "AGENT_SKILLS")}
          value={loading && !data ? "—" : count("AGENT_SKILLS")}
          sub={t("v2.registry.kpi.skillSub")}
        />
        <Kpi
          label={t("v2.registry.kpi.pending")}
          value={loading && !data ? "—" : pending}
          sub={
            discoverableIds === null
              ? t("v2.registry.kpi.discoverableUnknown")
              : t("v2.registry.kpi.hidden", { count: hiddenCount })
          }
          testId="v2-registry-kpi-pending"
        />
      </div>
      <Card>
        <div className="v2-toolbar">
          <Button onClick={() => { setSearchHits(null); reload(); }}>{t("v2.common.refresh")}</Button>
          <Button kind="primary" onClick={() => setParams(type && type !== "A2A" ? { view: "register", type } : { view: "register" })} testId="v2-registry-register">
            <Plus size={14} aria-hidden="true" />
            {t("v2.registry.register")}
          </Button>
          <Button onClick={() => navigate("/v2/governance")} title={t("v2.registry.importGatewayHint")}>
            <Network size={14} aria-hidden="true" />
            {t("v2.registry.importGateway")}
          </Button>
          <Button onClick={() => setParams({ view: "a2a-demo" })} testId="v2-registry-a2a-demo">
            <Shuffle size={14} aria-hidden="true" />
            {t("v2.registry.a2aDemo")}
          </Button>
          <FilterSelect
            label={t("v2.registry.col.type")}
            value={type}
            allLabel={t("v2.common.all")}
            onChange={setType}
            options={REGISTRY_TYPES.map((tp) => ({ value: tp, label: `${typeLabel(t, tp)} (${count(tp)})` }))}
            testId="v2-registry-type"
          />
          <FilterSelect
            label={t("v2.registry.col.status")}
            value={status}
            allLabel={t("v2.common.all")}
            onChange={setStatus}
            options={REGISTRY_STATUSES.map((s) => ({ value: s, label: statusLabel(t, s) }))}
          />
          <div className="end">
            <form className="v2-row" onSubmit={(e) => void runSearch(e)}>
              <SearchInput
                value={q}
                onChange={(v) => {
                  setQ(v);
                  if (!v.trim()) setSearchHits(null);
                }}
                placeholder={t("v2.registry.search")}
                testId="v2-registry-search"
              />
              <Button type="submit" disabled={searching || !q.trim()} title={t("v2.registry.semanticHint")}>
                <Search size={14} aria-hidden="true" />
                {searching ? t("v2.registry.searching") : t("v2.registry.semantic")}
              </Button>
            </form>
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        {searchHits && (
          <Alert
            action={
              <LinkButton onClick={() => { setSearchHits(null); setQ(""); }}>{t("v2.registry.clearSearch")}</LinkButton>
            }
          >
            {t("v2.registry.searchResults", { count: searchHits.length, q })}
          </Alert>
        )}
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(r) => r.record_id}
          loading={loading}
          error={data ? null : error}
          onRetry={reload}
          empty={t("v2.registry.empty")}
          testId="v2-registry-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
        {error && data && <Alert tone="warn">{error}</Alert>}
      </Card>
      <Confirm
        open={deleting !== null}
        title={t("v2.registry.deleteTitle")}
        body={t("registry.confirmDelete.body", { name: deleting?.name ?? "" })}
        confirmLabel={t("v2.common.delete")}
        danger
        busy={busy}
        onConfirm={() => void remove()}
        onClose={() => setDeleting(null)}
      />
    </>
  );
}

// ─── consumer view (?view=discoverable) ────────────────────────────────────
export function DiscoverableList() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const { data, loading, error, reload } = useRegistryData(t("registry.unavailableBody"));
  const [type, setType] = useState("");
  const [q, setQ] = useState("");

  const all = data?.discoverable ?? null;
  const records = useMemo(() => data?.records ?? [], [data]);
  const rows = useMemo(() => {
    const needle = q.trim().toLowerCase();
    return (all ?? []).filter((r) => {
      if (type && r.type !== type) return false;
      if (!needle) return true;
      return `${r.name} ${r.display_name ?? ""} ${r.record_id}`.toLowerCase().includes(needle);
    });
  }, [all, type, q]);
  const paged = usePaged(rows, 12);

  if (data?.unavailable) return <Unavailable tab="discoverable" message={data.unavailable} />;

  const ids = all ? new Set(all.map((r) => r.record_id)) : null;
  const hiddenCount = ids ? records.filter((r) => !ids.has(r.record_id)).length : null;
  const readFailed = data !== null && all === null;
  const empty =
    all && all.length === 0 && records.length > 0
      ? t("registry.consumer.emptyExplained", { count: records.length })
      : all && all.length === 0
        ? t("v2.registry.empty")
        : t("registry.consumer.emptyTab");

  const columns: Column<DiscoverableRegistryRecord>[] = [
    {
      key: "name",
      title: t("v2.registry.col.name"),
      render: (r) => (
        <>
          <LinkButton onClick={() => setParams({ view: "detail", id: r.record_id })}>{r.name}</LinkButton>
          <span className="sub mono">ID: {r.record_id}</span>
        </>
      ),
    },
    { key: "display", title: t("v2.registry.col.displayName"), render: (r) => r.display_name ?? "—" },
    { key: "type", title: t("v2.registry.col.type"), render: (r) => <TypeTag type={r.type} /> },
    {
      key: "desc",
      title: t("v2.registry.col.descriptorTypes"),
      render: (r) => <span className="mono">{r.descriptor_types.length ? r.descriptor_types.join(", ") : "—"}</span>,
    },
    { key: "status", title: t("v2.registry.col.status"), render: (r) => <StatusTag status={r.status} /> },
    { key: "version", title: t("v2.registry.col.version"), render: (r) => <span className="mono">{r.version ?? "—"}</span> },
    { key: "updated", title: t("v2.registry.col.updated"), render: (r) => <span className="nowrap">{fmtTime(r.updated_at)}</span> },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (r) => (
        <div className="v2-actions">
          <LinkButton onClick={() => setParams({ view: "detail", id: r.record_id })}>{t("v2.common.view")}</LinkButton>
        </div>
      ),
    },
  ];

  return (
    <>
      <RegistryHeader tab="discoverable" />
      <div className="v2-kpis">
        <Kpi label={t("v2.registry.kpi.discoverable")} value={all ? all.length : "—"} sub="ListDiscoverableRegistryRecords" testId="v2-registry-kpi-discoverable" />
        <Kpi
          label={t("v2.registry.kpi.notDiscoverable")}
          value={hiddenCount ?? "—"}
          sub={t("v2.registry.kpi.notDiscoverableSub")}
          tone={hiddenCount ? "bad" : undefined}
        />
        <Kpi label={t("v2.registry.kpi.published")} value={data ? records.length : "—"} sub={t("v2.registry.kpi.publishedSub")} />
      </div>
      <Card>
        <div className="v2-toolbar">
          <Button onClick={reload}>{t("v2.common.refresh")}</Button>
          <FilterSelect
            label={t("v2.registry.col.type")}
            value={type}
            allLabel={t("v2.common.all")}
            onChange={setType}
            options={REGISTRY_TYPES.map((tp) => ({
              value: tp,
              label: `${typeLabel(t, tp)} (${(all ?? []).filter((r) => r.type === tp).length})`,
            }))}
          />
          <div className="end">
            <SearchInput value={q} onChange={setQ} placeholder={t("v2.registry.search")} />
            <span className="v2-count">{t("v2.common.total", { count: rows.length })}</span>
          </div>
        </div>
        {readFailed && <Alert tone="error">{t("v2.registry.discoverableFailed")}</Alert>}
        <Table
          columns={columns}
          rows={paged.slice}
          rowKey={(r) => r.record_id}
          loading={loading}
          error={data ? null : error}
          onRetry={reload}
          empty={empty}
          testId="v2-registry-consumer-table"
        />
        <Pager page={paged.page} pages={paged.pages} total={paged.total} onPage={paged.setPage} />
      </Card>
    </>
  );
}
