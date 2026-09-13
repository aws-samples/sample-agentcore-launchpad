import type { CSSProperties } from "react";
import { Eye, Network, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Btn, Chip, ConfirmDialog, LoadError, Panel, useToast, ViewHead } from "../components";
import type { ChipTone } from "../components";
import { useAuth } from "../auth/auth-context";
import { api, ApiError, errorMessage, getJson } from "../lib/api";
import type {
  AgentInfo,
  DiscoverableRegistryRecord,
  LiveAgentCard,
  RegistryRecordSystem,
} from "../lib/api";
import { A2ADemoView } from "./registry/A2ADemoView";
import { EditView } from "./registry/EditView";
import { RegisterView } from "./registry/RegisterView";

type RecordType = "A2A" | "MCP" | "AGENT_SKILLS";

export interface RegistryRecord {
  record_id: string;
  name: string;
  description: string;
  type: RecordType;
  status: string;
  version: string | null;
  descriptors?: Record<string, unknown>;
  updated_at: string | null;
  /** SE-043: server-owned; set exactly when the workspace ledger maps this record to
   *  a system preset's Skill. Content edits are refused server-side for everyone,
   *  lifecycle actions for members — the UI mirrors that, the backend enforces it. */
  system?: RegistryRecordSystem | null;
}

interface SkillSourceMeta {
  kind: string;
  url?: string;
  ref?: string;
  /** Full commit SHA the files came from; absent on pre-pinning records. */
  commit?: string;
  subdir?: string;
  imported_at?: string;
}

/** Parse the AGENT_SKILLS skillDefinition JSON; returns file list + source when present.
 *  Twin copy in registry/EditView.tsx — keep both in sync if the shape changes. */
function parseSkillDefinition(
  record: RegistryRecord,
): { files: string[]; source: SkillSourceMeta | null } | null {
  if (record.type !== "AGENT_SKILLS") return null;
  const skills = record.descriptors?.agentSkills as
    | { skillDefinition?: { inlineContent?: string } }
    | undefined;
  const raw = skills?.skillDefinition?.inlineContent;
  if (!raw) return null;
  try {
    const parsed = JSON.parse(raw) as { files?: unknown; source?: unknown };
    const files = Array.isArray(parsed.files)
      ? parsed.files.filter((f): f is string => typeof f === "string")
      : [];
    const source =
      parsed.source && typeof parsed.source === "object"
        ? (parsed.source as SkillSourceMeta)
        : null;
    if (files.length === 0 && !source) return null;
    return { files, source };
  } catch {
    return null;
  }
}

const SOURCE_CHIP_TONE: Record<string, ChipTone> = {
  inline: "muted",
  zip: "aqua",
  git: "amber",
  url: "blue",
};

const TABS: { key: RecordType; labelKey: string }[] = [
  { key: "A2A", labelKey: "registry.tabs.agents" },
  { key: "MCP", labelKey: "registry.tabs.tools" },
  { key: "AGENT_SKILLS", labelKey: "registry.tabs.skills" },
];

const STATUS_CHIP: Record<string, { tone: ChipTone; icon: string; labelKey: string }> = {
  DRAFT: { tone: "muted", icon: "○", labelKey: "registry.states.draft" },
  PENDING_APPROVAL: { tone: "warn", icon: "◍", labelKey: "registry.states.submitted" },
  APPROVED: { tone: "good", icon: "●", labelKey: "registry.states.published" },
  REJECTED: { tone: "crit", icon: "✕", labelKey: "registry.states.rejected" },
  DEPRECATED: { tone: "muted", icon: "✕", labelKey: "registry.states.disabled" },
};

// Parsed A2A AgentCard from the record descriptor — the drawer renders it as
// a first-class panel (transport, endpoint, skills) instead of raw JSON only.
interface AgentCardData {
  url?: string;
  description?: string;
  version?: string;
  skills?: { id?: string; name?: string; description?: string; tags?: string[] }[];
  capabilities?: { streaming?: boolean };
  metadata?: Record<string, string>;
}

function parseAgentCard(record: RegistryRecord): AgentCardData | null {
  if (record.type !== "A2A") return null;
  const a2a = record.descriptors?.a2a as
    | { agentCard?: { inlineContent?: string } }
    | undefined;
  const raw = a2a?.agentCard?.inlineContent;
  if (!raw) return null;
  try {
    return JSON.parse(raw) as AgentCardData;
  } catch {
    return null;
  }
}

function descriptorExcerpt(record: RegistryRecord): string {
  const d = record.descriptors ?? {};
  try {
    const raw = JSON.stringify(d);
    const parsed = JSON.parse(raw, (key, value) => {
      if (key === "inlineContent" && typeof value === "string") {
        try {
          return JSON.parse(value);
        } catch {
          return value.length > 400 ? value.slice(0, 400) + "…" : value;
        }
      }
      return value;
    });
    return JSON.stringify(parsed, null, 2).slice(0, 1800);
  } catch {
    return JSON.stringify(d, null, 2).slice(0, 1800);
  }
}

export function Registry() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { isAdmin } = useAuth();
  // "?view=register" renders a standalone sub-page instead of the list — it is
  // linkable and the browser back button returns to the list (like Evaluation).
  const [searchParams, setSearchParams] = useSearchParams();
  const view = searchParams.get("view");
  const [records, setRecords] = useState<RegistryRecord[] | null>(null);
  const [unavailable, setUnavailable] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [tab, setTab] = useState<RecordType>("A2A");
  const [selected, setSelected] = useState<RegistryRecord | null>(null);
  const [query, setQuery] = useState("");
  const [searching, setSearching] = useState(false);
  const [busy, setBusy] = useState(false);
  const [confirmDisable, setConfirmDisable] = useState<RegistryRecord | null>(null);
  const [confirmDelete, setConfirmDelete] = useState<RegistryRecord | null>(null);
  const [reimporting, setReimporting] = useState(false);
  const [reimportError, setReimportError] = useState<string | null>(null);
  // Ledger agents keyed by the registry record they own — decides whether an
  // A2A record can serve a LIVE CARD. `null` = the list could not be loaded;
  // the button then stays enabled and the backend's refusal is shown instead.
  const [agentsByRecord, setAgentsByRecord] = useState<Map<string, AgentInfo> | null>(null);
  const [liveCard, setLiveCard] = useState<{
    recordId: string;
    loading: boolean;
    data: LiveAgentCard | null;
    error: string | null;
  } | null>(null);
  // Consumer view (?view=discoverable): what the data plane discloses
  // (ListDiscoverableRegistryRecords). `null` = not fetched in this page session;
  // the publisher list marks a record "not discoverable" only once the set is
  // known, never on a guess from its status.
  const [discoverable, setDiscoverable] = useState<DiscoverableRegistryRecord[] | null>(null);
  const [discoverableError, setDiscoverableError] = useState<string | null>(null);
  const discoverableFetched = useRef(false);

  const loadDiscoverable = useCallback(async () => {
    try {
      const body = await api.registryDiscoverable();
      discoverableFetched.current = true;
      setDiscoverable(body.records);
      setDiscoverableError(null);
    } catch (err) {
      if (err instanceof ApiError && err.code === "registry.unavailable") {
        setUnavailable(err.message || t("registry.unavailableBody"));
        return;
      }
      setDiscoverableError(errorMessage(err));
    }
  }, [t]);

  const load = useCallback(async () => {
    try {
      const body = await getJson<{ records: RegistryRecord[] }>("/api/registry/records");
      setUnavailable(null);
      setLoadError(null);
      setRecords(body.records);
      // a lifecycle action may have changed what consumers see — keep the diff honest
      if (discoverableFetched.current) void loadDiscoverable();
      try {
        const { agents } = await api.listAgents();
        const byRecord = new Map<string, AgentInfo>();
        for (const agent of agents) {
          if (agent.registry_record_id && agent.status !== "deleted") {
            byRecord.set(agent.registry_record_id, agent);
          }
        }
        setAgentsByRecord(byRecord);
      } catch {
        setAgentsByRecord(null);
      }
    } catch (err) {
      // 503 registry.unavailable = the account has no Registry — a distinct
      // full-page state. Anything else (backend down, 5xx) is a failed load:
      // the table says so instead of claiming there are no records.
      if (err instanceof ApiError && err.code === "registry.unavailable") {
        setUnavailable(err.message || t("registry.unavailableBody"));
        setRecords((prev) => prev ?? []);
        return;
      }
      setLoadError(errorMessage(err));
    }
  }, [t, loadDiscoverable]);

  useEffect(() => {
    void load();
  }, [load]);

  // `?record=<id>` on the list view (the System presets panel links here): select
  // that record once the list is known and show its tab. `?view=edit&record=` keeps
  // its own meaning below.
  const deepLinked = useRef<string | null>(null);
  useEffect(() => {
    const wanted = searchParams.get("record");
    if (view !== null || !wanted || !records || deepLinked.current === wanted) return;
    const hit = records.find((r) => r.record_id === wanted);
    if (!hit) return;
    deepLinked.current = wanted;
    setTab(hit.type);
    void select(hit);
  }, [records, searchParams, view]);

  // Entering the consumer view always reads the data plane afresh.
  useEffect(() => {
    if (view === "discoverable") void loadDiscoverable();
  }, [view, loadDiscoverable]);

  // A record's live card belongs to that record only — selecting another one
  // drops it (the read is on demand, never repeated on open).
  useEffect(() => {
    setLiveCard(null);
  }, [selected?.record_id]);

  const readLiveCard = async (record: RegistryRecord) => {
    setLiveCard({ recordId: record.record_id, loading: true, data: null, error: null });
    try {
      const data = await api.registryLiveAgentCard(record.record_id);
      setLiveCard({ recordId: record.record_id, loading: false, data, error: null });
    } catch (err) {
      setLiveCard({
        recordId: record.record_id,
        loading: false,
        data: null,
        error: errorMessage(err),
      });
    }
  };

  /** Why LIVE CARD is disabled for this record, or null when it can be read. */
  const liveCardDisabledReason = (record: RegistryRecord): string | null => {
    if (record.type !== "A2A") return t("registry.drawer.liveCardNotLaunchpad");
    if (agentsByRecord === null) return null; // unknown → let the backend decide
    const agent = agentsByRecord.get(record.record_id);
    if (!agent) return t("registry.drawer.liveCardNotLaunchpad");
    if (agent.spec.protocol !== "a2a") return t("registry.drawer.liveCardNotA2A");
    if (agent.status !== "active" || !agent.arn) {
      return t("registry.drawer.liveCardNotReady", { status: agent.status });
    }
    return null;
  };

  const runSearch = async () => {
    if (!query.trim()) {
      setSearching(false);
      void load();
      return;
    }
    setSearching(true);
    const res = await fetch(`/api/registry/records/search?q=${encodeURIComponent(query)}`);
    if (res.ok) {
      const body = (await res.json()) as { records: RegistryRecord[] };
      setRecords(body.records);
    }
  };

  const select = async (record: RegistryRecord) => {
    setReimportError(null);
    setSelected(record);
    try {
      const res = await fetch(`/api/registry/records/${record.record_id}`);
      if (res.ok) setSelected((await res.json()) as RegistryRecord);
    } catch {
      /* keep the summary row */
    }
  };

  const action = async (record: RegistryRecord, act: string) => {
    setBusy(true);
    try {
      const res = await fetch(`/api/registry/records/${record.record_id}/action`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ action: act }),
      });
      if (res.ok) {
        const updated = (await res.json()) as RegistryRecord;
        setSelected(updated);
        void load();
      } else {
        const env = (await res.json().catch(() => ({}))) as { message?: string };
        toast(t("common.actionFailed", { msg: env.message ?? `HTTP ${res.status}` }));
      }
    } catch (err) {
      toast(t("common.actionFailed", { msg: String(err) }));
    } finally {
      setBusy(false);
    }
  };

  // re-run the ingestion pipeline from a git/url skill record's stored source:
  // re-acquire → re-upload S3 → bump recordVersion. Refreshes the drawer + list.
  const reimport = async (record: RegistryRecord) => {
    setReimporting(true);
    setReimportError(null);
    try {
      const res = await fetch(`/api/registry/records/${record.record_id}/reimport`, {
        method: "POST",
      });
      const body = (await res.json().catch(() => ({}))) as RegistryRecord & { message?: string };
      if (!res.ok) {
        setReimportError(t("registry.drawer.reimportFailed", { msg: body.message ?? `HTTP ${res.status}` }));
        return;
      }
      setSelected(body);
      toast(t("registry.drawer.reimportOk", { name: body.name }));
      void load();
    } catch (err) {
      setReimportError(t("registry.drawer.reimportFailed", { msg: String(err) }));
    } finally {
      setReimporting(false);
    }
  };

  // Success tail for the register sub-page (inline/MCP POST + zip/git/url
  // import share it): return to the list, toast, reload, and select the record.
  const handleRegistered = useCallback(
    async (record: RegistryRecord | null, name: string) => {
      setSearchParams({}, { replace: true });
      toast(t("registry.register.done", { name }));
      setSearching(false);
      if (record) setTab(record.type);
      await load();
      if (record) setSelected(record);
    },
    [load, setSearchParams, t, toast],
  );

  // Success tail for the edit sub-page: EditView already toasted, so just return
  // to the list, reload, and re-select the (updated) record.
  const handleEdited = useCallback(
    async (record: RegistryRecord) => {
      setSearchParams({}, { replace: true });
      setSearching(false);
      setTab(record.type);
      await load();
      setSelected(record);
    },
    [load, setSearchParams],
  );

  const deleteRecord = async (record: RegistryRecord) => {
    setBusy(true);
    try {
      const res = await fetch(`/api/registry/records/${record.record_id}`, {
        method: "DELETE",
      });
      if (res.ok) {
        toast(t("registry.deleted", { name: record.name }));
        setSelected(null);
        void load();
      } else {
        const env = (await res.json().catch(() => ({}))) as { message?: string };
        toast(t("common.actionFailed", { msg: env.message ?? `HTTP ${res.status}` }));
      }
    } catch (err) {
      toast(t("common.actionFailed", { msg: String(err) }));
    } finally {
      setBusy(false);
    }
  };

  /** A system-managed Skill's lifecycle (submit/approve/reject/disable) is an
   *  administrator's call; every other record keeps its member-operable lifecycle.
   *  The server refuses regardless — this only hides what would be refused. */
  const lifecycleAllowed = (record: RegistryRecord) => !record.system || isAdmin;

  const openInWizard = (record: RegistryRecord) => {
    if (record.type === "MCP") {
      navigate(`/create?gateway=${encodeURIComponent(record.name)}`);
      return;
    }
    if (record.type === "AGENT_SKILLS") {
      let path = record.name;
      try {
        const skills = record.descriptors?.agentSkills as
          | { skillDefinition?: { inlineContent?: string } }
          | undefined;
        const definition = JSON.parse(skills?.skillDefinition?.inlineContent ?? "{}") as {
          path?: string;
        };
        if (definition.path) path = definition.path;
      } catch {
        /* fall back to the record name */
      }
      navigate(`/create?skill=${encodeURIComponent(path)}`);
    }
  };

  if (unavailable) {
    return (
      <section>
        <ViewHead
          kicker={t("registry.kicker")}
          title={t("registry.title")}
          meta={t("registry.metaUnavailable")}
        />
        <Panel brk>
          <div className="gov-state gov-state-error" data-testid="registry-unavailable">
            <TriangleAlert size={20} aria-hidden="true" />
            <strong>{t("registry.unavailableTitle")}</strong>
            <span>{unavailable}</span>
          </div>
        </Panel>
      </section>
    );
  }

  // ── Register sub-page (?view=register) ────────────────────────────────────
  if (view === "register") {
    return (
      <RegisterView
        initialType={tab === "AGENT_SKILLS" ? "AGENT_SKILLS" : "MCP"}
        onBack={() => setSearchParams({}, { replace: true })}
        onDone={(record, name) => void handleRegistered(record, name)}
      />
    );
  }

  // ── A2A routing demo sub-page (?view=a2a-demo) ────────────────────────────
  if (view === "a2a-demo") {
    return <A2ADemoView onBack={() => setSearchParams({}, { replace: true })} />;
  }

  // ── Edit sub-page (?view=edit&record=<id>) ────────────────────────────────
  if (view === "edit") {
    return (
      <EditView
        recordId={searchParams.get("record") ?? ""}
        onBack={() => setSearchParams({}, { replace: true })}
        onDone={(record) => void handleEdited(record)}
      />
    );
  }

  const loading = records === null;
  const skillMeta = selected ? parseSkillDefinition(selected) : null;
  const loaded = records ?? [];
  // rows that were loaded once stay visible through a later failed poll
  const failed = loadError !== null && loaded.length === 0;
  const visible = searching ? loaded : loaded.filter((r) => r.type === tab);
  const counts = (type: RecordType) => loaded.filter((r) => r.type === type).length;

  // ── Consumer view (?view=discoverable) — same page, same drawer: the table
  // swaps to the data-plane list and the publisher rows learn the diff.
  const consumerView = view === "discoverable";
  const discoverableIds =
    discoverable === null ? null : new Set(discoverable.map((r) => r.record_id));
  const isHidden = (record: RegistryRecord) =>
    discoverableIds !== null && !discoverableIds.has(record.record_id);
  const hiddenCount = loaded.filter(isHidden).length;
  const consumerRows = (discoverable ?? []).filter((r) => r.type === tab);
  const consumerCounts = (type: RecordType) =>
    (discoverable ?? []).filter((r) => r.type === type).length;
  const consumerLoading = discoverable === null && discoverableError === null;
  const consumerFailed = discoverableError !== null && discoverable === null;
  const hiddenChip = (
    <span title={t("registry.consumer.notDiscoverableHint")} style={{ marginLeft: 6 }}>
      <Chip tone="amber" icon="⊘">{t("registry.consumer.notDiscoverable")}</Chip>
    </span>
  );

  return (
    <section>
      <ViewHead
        kicker={t(consumerView ? "registry.consumer.kicker" : "registry.kicker")}
        title={t(consumerView ? "registry.consumer.title" : "registry.title")}
        meta={t(consumerView ? "registry.consumer.meta" : "registry.metaLive")}
      />

      <Panel brk pad={false} style={{ "--i": 0, marginBottom: 14 } as CSSProperties}>
        <div className="phead" style={{ borderBottom: 0, paddingBottom: 0 }}>
          {consumerView ? (
            <div className="search mono" style={{ gap: 9 }} data-testid="consumer-view-label">
              <Eye size={14} aria-hidden="true" />
              <span style={{ color: "var(--line-2)" }}>{t("registry.consumer.apiLabel")}</span>
            </div>
          ) : (
          <div className="search" style={{ gap: 9 }}>
            ⌕
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void runSearch()}
              placeholder={t("registry.searchPlaceholder")}
              style={{
                background: "transparent",
                border: 0,
                outline: "none",
                color: "var(--ink)",
                flex: 1,
                font: "inherit",
              }}
            />
            <span style={{ color: "var(--line-2)" }}>
              SearchDiscoverableRegistryRecords
            </span>
          </div>
          )}
          <div className="end flowchips">
            {consumerView && discoverable !== null ? (
              <>
                <Chip tone="good" icon="●">
                  {t("registry.consumer.discoverableCount", { count: discoverable.length })}
                </Chip>
                {!loading && (
                  <Chip tone={hiddenCount > 0 ? "amber" : "muted"} icon="⊘">
                    {t("registry.consumer.hiddenCount", { count: hiddenCount })}
                  </Chip>
                )}
              </>
            ) : (
              <>
                <Chip tone="muted" icon="○">{t("registry.states.draft")}</Chip>
                <i>→</i>
                <Chip tone="warn" icon="◍">{t("registry.states.submitted")}</Chip>
                <i>→</i>
                <Chip tone="good" icon="●">{t("registry.states.published")}</Chip>
              </>
            )}
          </div>
        </div>
        <div className="tabs" style={{ padding: "0 16px" }}>
          {TABS.map(({ key, labelKey }) => (
            <button
              key={key}
              type="button"
              className={`tab${!searching && tab === key ? " active" : ""}`}
              onClick={() => {
                setSearching(false);
                setQuery("");
                setTab(key);
              }}
            >
              {t(labelKey)}
              <span className="cnt">{consumerView ? consumerCounts(key) : counts(key)}</span>
            </button>
          ))}
          {searching && <span className="tab active">{t("registry.searchResults")}</span>}
          <div className="tabs-actions">
            {consumerView ? (
              <Btn
                onClick={() => setSearchParams({}, { replace: true })}
                data-testid="publisher-list-btn"
              >
                ← {t("registry.consumer.back")}
              </Btn>
            ) : (
              <>
                <Btn
                  onClick={() => {
                    setSearching(false);
                    setQuery("");
                    setSearchParams({ view: "discoverable" });
                  }}
                  data-testid="consumer-view-btn"
                >
                  <Eye size={14} aria-hidden="true" />
                  {t("registry.consumer.entry")}
                </Btn>
                <Btn onClick={() => navigate("/governance")}>
                  <Network size={14} aria-hidden="true" />
                  {t("registry.importGateway")}
                </Btn>
                <Btn
                  onClick={() => setSearchParams({ view: "a2a-demo" })}
                  data-testid="a2a-demo-btn"
                >
                  ⇄ {t("registry.a2aDemo.entry")}
                </Btn>
                <Btn
                  primary
                  onClick={() => setSearchParams({ view: "register" })}
                  data-testid="register-btn"
                >
                  + {t("registry.register.cta")}
                </Btn>
              </>
            )}
          </div>
        </div>

        <div className="reg-grid" style={{ padding: 14 }}>
          <div className="table-scroll">
            {consumerView ? (
            <table data-testid="consumer-view-table">
              <thead>
                <tr>
                  <th>{t("registry.consumer.cols.name")}</th>
                  <th>{t("registry.consumer.cols.displayName")}</th>
                  <th>{t("registry.consumer.cols.type")}</th>
                  <th>{t("registry.consumer.cols.descriptorTypes")}</th>
                  <th>{t("registry.consumer.cols.status")}</th>
                  <th>{t("registry.consumer.cols.version")}</th>
                  <th>{t("registry.consumer.cols.updated")}</th>
                </tr>
              </thead>
              <tbody>
                {consumerRows.map((record) => {
                  const chip = STATUS_CHIP[record.status] ?? STATUS_CHIP.APPROVED;
                  return (
                    <tr
                      key={record.record_id}
                      onClick={() => void select(record)}
                      style={{
                        cursor: "pointer",
                        background:
                          selected?.record_id === record.record_id
                            ? "rgba(255,176,0,.045)"
                            : undefined,
                      }}
                    >
                      <td className="pri">{record.name}</td>
                      <td>{record.display_name ?? "—"}</td>
                      <td>
                        {record.type === "A2A" ? (
                          <Chip tone="amber" icon="◇">A2A</Chip>
                        ) : record.type === "MCP" ? (
                          <Chip tone="aqua" icon="⇄">MCP</Chip>
                        ) : (
                          <Chip tone="muted" icon="❖">{record.type}</Chip>
                        )}
                      </td>
                      <td className="mono">
                        {record.descriptor_types.length > 0
                          ? record.descriptor_types.join(", ")
                          : "—"}
                      </td>
                      <td>
                        <Chip tone={chip.tone} icon={chip.icon}>{t(chip.labelKey)}</Chip>
                      </td>
                      <td className="mono">{record.version ?? "—"}</td>
                      <td className="mono">{record.updated_at ?? "—"}</td>
                    </tr>
                  );
                })}
                {consumerLoading && !consumerFailed && (
                  <tr>
                    <td colSpan={7} className="loading-line">
                      {t("common.loading")}
                    </td>
                  </tr>
                )}
                {consumerFailed && (
                  <tr>
                    <td colSpan={7}>
                      <LoadError
                        message={discoverableError}
                        onRetry={() => void loadDiscoverable()}
                        inline
                        data-testid="consumer-view-load-error"
                      />
                    </td>
                  </tr>
                )}
                {discoverable !== null && consumerRows.length === 0 && (
                  <tr>
                    <td
                      colSpan={7}
                      className="dim mono"
                      style={{ textAlign: "center" }}
                      data-testid="consumer-view-empty"
                    >
                      {discoverable.length === 0 && loaded.length > 0
                        ? t("registry.consumer.emptyExplained", { count: loaded.length })
                        : discoverable.length === 0
                          ? t("registry.empty")
                          : t("registry.consumer.emptyTab")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            ) : (
            <table>
              <thead>
                <tr>
                  <th>{t("registry.cols.name")}</th>
                  <th>{t("registry.cols.type")}</th>
                  <th>{t("registry.cols.version")}</th>
                  <th>{t("registry.cols.state")}</th>
                </tr>
              </thead>
              <tbody>
                {visible.map((record) => {
                  const chip = STATUS_CHIP[record.status] ?? STATUS_CHIP.DRAFT;
                  return (
                    <tr
                      key={record.record_id}
                      onClick={() => void select(record)}
                      style={{
                        cursor: "pointer",
                        background:
                          selected?.record_id === record.record_id
                            ? "rgba(255,176,0,.045)"
                            : undefined,
                      }}
                    >
                      <td className="pri">
                        {record.name}
                        {record.system && (
                          <>
                            {" "}
                            <Chip tone="blue" icon="◈" data-testid="system-skill-chip">
                              {t("registry.drawer.system.chip")}
                            </Chip>
                          </>
                        )}
                      </td>
                      <td>
                        {record.type === "A2A" ? (
                          <Chip tone="amber" icon="◇">A2A</Chip>
                        ) : record.type === "MCP" ? (
                          <Chip tone="aqua" icon="⇄">GATEWAY · MCP</Chip>
                        ) : (
                          <Chip tone="muted" icon="❖">AGENT_SKILLS</Chip>
                        )}
                      </td>
                      <td className="mono">{record.version ?? "—"}</td>
                      <td>
                        <Chip tone={chip.tone} icon={chip.icon}>{t(chip.labelKey)}</Chip>
                        {isHidden(record) && hiddenChip}
                      </td>
                    </tr>
                  );
                })}
                {loading && !failed && (
                  <tr>
                    <td colSpan={4} className="loading-line">
                      {t("common.loading")}
                    </td>
                  </tr>
                )}
                {failed && (
                  <tr>
                    <td colSpan={4}>
                      <LoadError
                        message={loadError}
                        onRetry={() => void load()}
                        inline
                        data-testid="registry-load-error"
                      />
                    </td>
                  </tr>
                )}
                {!loading && !failed && visible.length === 0 && (
                  <tr>
                    <td colSpan={4} className="dim mono" style={{ textAlign: "center" }}>
                      {t("registry.empty")}
                    </td>
                  </tr>
                )}
              </tbody>
            </table>
            )}
          </div>

          <Panel
            className="drawer"
            title={selected?.name ?? "—"}
            end={
              selected &&
              (() => {
                const chip = STATUS_CHIP[selected.status] ?? STATUS_CHIP.DRAFT;
                return (
                  <>
                    {selected.system && (
                      <Chip tone="blue" icon="◈" data-testid="drawer-system-chip">
                        {t("registry.drawer.system.chip")}
                      </Chip>
                    )}
                    <Chip tone={chip.tone} icon={chip.icon}>{t(chip.labelKey)}</Chip>
                    {isHidden(selected) && hiddenChip}
                  </>
                );
              })()
            }
            pad={false}
            style={{ borderColor: "var(--line-2)" }}
          >
            {selected && (
              <>
                <div className="sect">
                  <div className="kv">
                    <span className="k">{t("registry.drawer.type")}</span>
                    <span className="v">{selected.type}</span>
                  </div>
                  <div className="kv">
                    <span className="k">{t("registry.drawer.version")}</span>
                    <span className="v">{selected.version ?? "—"}</span>
                  </div>
                  <div className="kv">
                    <span className="k">{t("registry.drawer.recordId")}</span>
                    <span className="v">{selected.record_id}</span>
                  </div>
                  <div className="kv">
                    <span className="k">{t("registry.drawer.updated")}</span>
                    <span className="v">{selected.updated_at?.slice(0, 19) ?? "—"}</span>
                  </div>
                </div>
                {(() => {
                  const card = parseAgentCard(selected);
                  if (!card) return null;
                  const transport = card.metadata?.["launchpad.transport"];
                  const skills = card.skills ?? [];
                  return (
                    <div className="sect" data-testid="agent-card-panel">
                      <h4>{t("registry.drawer.agentCard")}</h4>
                      <div className="kv">
                        <span className="k">{t("registry.drawer.cardTransport")}</span>
                        <span className="v">
                          <Chip tone={transport === "a2a-jsonrpc" ? "good" : "muted"}>
                            {transport ?? "—"}
                          </Chip>
                        </span>
                      </div>
                      {card.url && (
                        <div className="kv">
                          <span className="k">{t("registry.drawer.cardUrl")}</span>
                          <span
                            className="v"
                            style={{ display: "flex", gap: 6, alignItems: "center" }}
                          >
                            <span style={{ flex: 1, wordBreak: "break-all", fontSize: 10 }}>
                              {card.url}
                            </span>
                            <Btn
                              data-testid="card-url-copy"
                              title={t("registry.drawer.cardUrlCopy")}
                              onClick={() => {
                                void navigator.clipboard.writeText(card.url ?? "");
                                toast(t("registry.drawer.cardUrlCopied"));
                              }}
                            >
                              ⧉
                            </Btn>
                          </span>
                        </div>
                      )}
                      <div className="kv">
                        <span className="k">{t("registry.drawer.cardStreaming")}</span>
                        <span className="v">{card.capabilities?.streaming ? "✓" : "—"}</span>
                      </div>
                      {skills.length > 0 && (
                        <>
                          <h4 style={{ marginTop: 10 }}>
                            {t("registry.drawer.cardSkills", { n: skills.length })}
                          </h4>
                          {skills.map((s) => (
                            <div key={s.id ?? s.name} style={{ marginBottom: 7 }}>
                              <span className="selchip on" style={{ marginRight: 5 }}>
                                {s.name ?? s.id}
                              </span>
                              {(s.tags ?? []).map((tg) => (
                                <span
                                  key={tg}
                                  className="selchip"
                                  style={{ marginRight: 4, opacity: 0.65 }}
                                >
                                  {tg}
                                </span>
                              ))}
                              {s.description && (
                                <div className="dim" style={{ fontSize: 10.5, marginTop: 2 }}>
                                  {s.description}
                                </div>
                              )}
                            </div>
                          ))}
                        </>
                      )}
                      {(() => {
                        const reason = liveCardDisabledReason(selected);
                        const live =
                          liveCard && liveCard.recordId === selected.record_id ? liveCard : null;
                        const diff = live?.data?.diff;
                        return (
                          <div style={{ marginTop: 10 }} data-testid="live-agent-card">
                            <div style={{ display: "flex", gap: 8, alignItems: "center" }}>
                              <Btn
                                data-testid="live-card-btn"
                                disabled={Boolean(reason) || Boolean(live?.loading)}
                                disabledReason={reason ?? undefined}
                                title={reason ? undefined : t("registry.drawer.liveCardHint")}
                                onClick={() => void readLiveCard(selected)}
                              >
                                {live?.loading
                                  ? t("registry.drawer.liveCardReading")
                                  : t("registry.drawer.liveCard")}
                              </Btn>
                              {live?.data && (
                                <Chip
                                  tone={
                                    live.data.status_code !== null &&
                                    live.data.status_code >= 200 &&
                                    live.data.status_code < 300
                                      ? "good"
                                      : "warn"
                                  }
                                  data-testid="live-card-status"
                                >
                                  {t("registry.drawer.liveCardStatus", {
                                    code: live.data.status_code ?? "—",
                                  })}
                                </Chip>
                              )}
                              {live?.data && diff && (
                                <Chip tone={diff.identical ? "good" : "amber"}>
                                  {diff.identical
                                    ? t("registry.drawer.liveCardIdentical")
                                    : t("registry.drawer.liveCardDrift")}
                                </Chip>
                              )}
                            </div>
                            {live?.error && (
                              <div
                                className="mono"
                                role="alert"
                                data-testid="live-card-error"
                                style={{ color: "var(--crit)", fontSize: 10.5, marginTop: 6 }}
                              >
                                {t("registry.drawer.liveCardFailed", { msg: live.error })}
                              </div>
                            )}
                            {live?.data && diff && !diff.identical && (
                              <ul
                                className="mono"
                                data-testid="live-card-diff"
                                style={{ fontSize: 10.5, margin: "6px 0 0", paddingLeft: 16 }}
                              >
                                {diff.fields.map((f) => (
                                  <li key={f.field}>
                                    {t("registry.drawer.liveCardField", {
                                      field: f.field,
                                      record: String(f.record ?? "—"),
                                      live: String(f.live ?? "—"),
                                    })}
                                  </li>
                                ))}
                                {diff.skills_only_in_live.length > 0 && (
                                  <li>
                                    {t("registry.drawer.liveCardSkillsOnlyLive", {
                                      ids: diff.skills_only_in_live.join(", "),
                                    })}
                                  </li>
                                )}
                                {diff.skills_only_in_record.length > 0 && (
                                  <li>
                                    {t("registry.drawer.liveCardSkillsOnlyRecord", {
                                      ids: diff.skills_only_in_record.join(", "),
                                    })}
                                  </li>
                                )}
                              </ul>
                            )}
                            {live?.data && (
                              <details style={{ marginTop: 6 }} data-testid="live-card-json">
                                <summary className="dim mono" style={{ fontSize: 10.5 }}>
                                  {t("registry.drawer.liveCardJson")}
                                </summary>
                                <div
                                  className="code"
                                  style={{ maxHeight: 260, overflowY: "auto", marginTop: 4 }}
                                >
                                  {JSON.stringify(live.data.card, null, 2)}
                                </div>
                              </details>
                            )}
                          </div>
                        );
                      })()}
                    </div>
                  );
                })()}
                {selected.system && (
                  <div className="sect" data-testid="system-skill">
                    <h4>{t("registry.drawer.system.title")}</h4>
                    <div className="kv">
                      <span className="k">{t("registry.drawer.system.preset")}</span>
                      <span className="v">{selected.system.label}</span>
                    </div>
                    <div className="kv">
                      <span className="k">{t("registry.drawer.system.version")}</span>
                      <span className="v mono" data-testid="system-skill-version">
                        {selected.system.skill_version ?? "—"}
                        {selected.system.release_digest ? ` · ${selected.system.release_digest}` : ""}
                      </span>
                    </div>
                    <div className="kv">
                      <span className="k">{t("registry.drawer.system.path")}</span>
                      <span className="v mono" data-testid="system-skill-path">
                        {selected.system.path ?? "—"}
                      </span>
                    </div>
                    <div className="note" style={{ marginTop: 8 }} data-testid="system-skill-note">
                      <span className="i">◈</span>
                      <span>
                        {t(
                          isAdmin
                            ? "registry.drawer.system.noteAdmin"
                            : "registry.drawer.system.noteMember",
                        )}
                      </span>
                    </div>
                  </div>
                )}
                {skillMeta && (
                  <div className="sect" data-testid="skill-bundle">
                    {skillMeta.source && (
                      <div className="kv">
                        <span className="k">{t("registry.drawer.source")}</span>
                        <span className="v">
                          <Chip tone={SOURCE_CHIP_TONE[skillMeta.source.kind] ?? "muted"}>
                            {skillMeta.source.kind}
                          </Chip>
                        </span>
                      </div>
                    )}
                    {skillMeta.source?.url && (
                      <div className="kv">
                        <span className="k">{t("registry.drawer.sourceUrl")}</span>
                        <span className="v">{skillMeta.source.url}</span>
                      </div>
                    )}
                    {skillMeta.source?.kind === "git" && (
                      <div className="kv">
                        <span className="k">{t("registry.drawer.sourceCommit")}</span>
                        {/* `ref` is what was asked for and may be a branch; the
                            commit is the revision this record actually carries.
                            Records imported before commits were recorded show a
                            hint instead of a blank. */}
                        <span className="v" data-testid="skill-source-commit">
                          {skillMeta.source.commit ?? t("registry.drawer.commitUnknown")}
                        </span>
                      </div>
                    )}
                    {skillMeta.files.length > 0 && (
                      <>
                        <h4 style={{ marginTop: 10 }}>
                          {t("registry.drawer.files", { n: skillMeta.files.length })}
                        </h4>
                        <div
                          className="code"
                          style={{ maxHeight: 160, overflowY: "auto" }}
                          data-testid="skill-files"
                        >
                          {skillMeta.files.join("\n")}
                        </div>
                      </>
                    )}
                  </div>
                )}
                <div className="sect">
                  <h4>{t("registry.drawer.descriptor")}</h4>
                  <div className="code" style={{ maxHeight: 260, overflowY: "auto" }}>
                    {descriptorExcerpt(selected)}
                  </div>
                </div>
                <div className="sect" style={{ display: "flex", gap: 9, borderBottom: 0, flexWrap: "wrap" }}>
                  {selected.type !== "A2A" && (
                    <Btn
                      primary
                      style={{ flex: 1, justifyContent: "center" }}
                      disabled={selected.status !== "APPROVED"}
                      title={
                        selected.status !== "APPROVED"
                          ? t("registry.drawer.useNeedsApproved")
                          : undefined
                      }
                      data-testid="use-in-wizard-btn"
                      onClick={() => openInWizard(selected)}
                    >
                      {t("registry.drawer.useInNewAgent")}
                    </Btn>
                  )}
                  {selected.type !== "A2A" && selected.status !== "DEPRECATED" && !selected.system && (
                    <Btn
                      onClick={() =>
                        setSearchParams({ view: "edit", record: selected.record_id })
                      }
                      data-testid="edit-btn"
                    >
                      {t("registry.drawer.edit")}
                    </Btn>
                  )}
                  {/* Any status is evaluable — a fresh DRAFT skill is exactly
                      what an author wants to score before publishing it. */}
                  {selected.type === "AGENT_SKILLS" && (
                    <Btn
                      data-testid="evaluate-in-skill-lab-btn"
                      onClick={() =>
                        navigate(
                          `/skill-lab?view=eval&job=new&record=${encodeURIComponent(selected.record_id)}`,
                        )
                      }
                    >
                      {t("registry.drawer.evaluateInSkillLab")}
                    </Btn>
                  )}
                  {selected.type === "AGENT_SKILLS" &&
                    !selected.system &&
                    (skillMeta?.source?.kind === "git" || skillMeta?.source?.kind === "url") &&
                    selected.status !== "DEPRECATED" && (
                      <Btn
                        disabled={busy || reimporting}
                        onClick={() => void reimport(selected)}
                        data-testid="reimport-btn"
                      >
                        {reimporting
                          ? t("registry.drawer.reimporting")
                          : t("registry.drawer.reimport")}
                      </Btn>
                    )}
                  {lifecycleAllowed(selected) && selected.status === "DRAFT" && (
                    <Btn
                      disabled={busy}
                      onClick={() => void action(selected, "submit")}
                      data-testid="submit-btn"
                    >
                      {t("registry.drawer.submit")}
                    </Btn>
                  )}
                  {lifecycleAllowed(selected) &&
                    (selected.status === "PENDING_APPROVAL" ||
                      selected.status === "REJECTED") && (
                      <Btn
                        disabled={busy}
                        onClick={() => void action(selected, "approve")}
                        data-testid="approve-btn"
                      >
                        {t("registry.drawer.approve")}
                      </Btn>
                    )}
                  {lifecycleAllowed(selected) && selected.status === "PENDING_APPROVAL" && (
                    <Btn
                      disabled={busy}
                      onClick={() => void action(selected, "reject")}
                      data-testid="reject-btn"
                    >
                      {t("registry.drawer.reject")}
                    </Btn>
                  )}
                  {lifecycleAllowed(selected) && selected.status === "APPROVED" && (
                    <Btn
                      disabled={busy}
                      onClick={() => setConfirmDisable(selected)}
                      data-testid="disable-btn"
                    >
                      {t("registry.drawer.disable")}
                    </Btn>
                  )}
                  {!selected.system && (
                    <Btn
                      disabled={busy}
                      style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
                      onClick={() => setConfirmDelete(selected)}
                      data-testid="delete-btn"
                    >
                      {t("registry.drawer.delete")}
                    </Btn>
                  )}
                  {selected.system && !isAdmin && (
                    <span className="dim mono" style={{ fontSize: 11 }} data-testid="system-skill-readonly">
                      {t("registry.drawer.system.memberReadOnly")}
                    </span>
                  )}
                </div>
                {reimportError && (
                  <div className="sect" style={{ borderBottom: 0, paddingTop: 0 }}>
                    <div
                      className="note"
                      style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
                      data-testid="reimport-error"
                    >
                      <span className="i">!</span>
                      <span>{reimportError}</span>
                    </div>
                  </div>
                )}
              </>
            )}
          </Panel>
        </div>
      </Panel>

      <ConfirmDialog
        open={confirmDisable !== null}
        title={t("registry.confirmDisable.title")}
        body={t("registry.confirmDisable.body", { name: confirmDisable?.name ?? "" })}
        confirmLabel={t("registry.drawer.disable")}
        onConfirm={() => {
          if (confirmDisable) void action(confirmDisable, "disable");
          setConfirmDisable(null);
        }}
        onCancel={() => setConfirmDisable(null)}
      />
      <ConfirmDialog
        open={confirmDelete !== null}
        title={t("registry.confirmDelete.title")}
        body={t("registry.confirmDelete.body", { name: confirmDelete?.name ?? "" })}
        confirmLabel={t("registry.drawer.delete")}
        onConfirm={() => {
          if (confirmDelete) void deleteRecord(confirmDelete);
          setConfirmDelete(null);
        }}
        onCancel={() => setConfirmDelete(null)}
      />
    </section>
  );
}
