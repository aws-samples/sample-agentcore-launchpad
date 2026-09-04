import type { CSSProperties } from "react";
import { Network, TriangleAlert } from "lucide-react";
import { useCallback, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useNavigate, useSearchParams } from "react-router-dom";

import { Btn, Chip, ConfirmDialog, LoadError, Panel, useToast, ViewHead } from "../components";
import type { ChipTone } from "../components";
import { ApiError, errorMessage, getJson } from "../lib/api";
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

  const load = useCallback(async () => {
    try {
      const body = await getJson<{ records: RegistryRecord[] }>("/api/registry/records");
      setUnavailable(null);
      setLoadError(null);
      setRecords(body.records);
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
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

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

  return (
    <section>
      <ViewHead
        kicker={t("registry.kicker")}
        title={t("registry.title")}
        meta={t("registry.metaLive")}
      />

      <Panel brk pad={false} style={{ "--i": 0, marginBottom: 14 } as CSSProperties}>
        <div className="phead" style={{ borderBottom: 0, paddingBottom: 0 }}>
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
          <div className="end flowchips">
            <Chip tone="muted" icon="○">{t("registry.states.draft")}</Chip>
            <i>→</i>
            <Chip tone="warn" icon="◍">{t("registry.states.submitted")}</Chip>
            <i>→</i>
            <Chip tone="good" icon="●">{t("registry.states.published")}</Chip>
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
              <span className="cnt">{counts(key)}</span>
            </button>
          ))}
          {searching && <span className="tab active">{t("registry.searchResults")}</span>}
          <div style={{ marginLeft: "auto", alignSelf: "center", display: "flex", gap: 8 }}>
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
          </div>
        </div>

        <div className="reg-grid" style={{ padding: 14 }}>
          <div>
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
                      <td className="pri">{record.name}</td>
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
          </div>

          <Panel
            className="drawer"
            title={selected?.name ?? "—"}
            end={
              selected &&
              (() => {
                const chip = STATUS_CHIP[selected.status] ?? STATUS_CHIP.DRAFT;
                return <Chip tone={chip.tone} icon={chip.icon}>{t(chip.labelKey)}</Chip>;
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
                    </div>
                  );
                })()}
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
                  {selected.type !== "A2A" && selected.status !== "DEPRECATED" && (
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
                  {selected.status === "DRAFT" && (
                    <Btn disabled={busy} onClick={() => void action(selected, "submit")}>
                      {t("registry.drawer.submit")}
                    </Btn>
                  )}
                  {(selected.status === "PENDING_APPROVAL" ||
                    selected.status === "REJECTED") && (
                    <Btn disabled={busy} onClick={() => void action(selected, "approve")}>
                      {t("registry.drawer.approve")}
                    </Btn>
                  )}
                  {selected.status === "PENDING_APPROVAL" && (
                    <Btn disabled={busy} onClick={() => void action(selected, "reject")}>
                      {t("registry.drawer.reject")}
                    </Btn>
                  )}
                  {selected.status === "APPROVED" && (
                    <Btn disabled={busy} onClick={() => setConfirmDisable(selected)}>
                      {t("registry.drawer.disable")}
                    </Btn>
                  )}
                  <Btn
                    disabled={busy}
                    style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
                    onClick={() => setConfirmDelete(selected)}
                  >
                    {t("registry.drawer.delete")}
                  </Btn>
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
