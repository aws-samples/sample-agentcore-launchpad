import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import { ArrowLeft, Download, RefreshCw, Search } from "lucide-react";

import { useAuth } from "../auth/auth-context";
import {
  Btn,
  Chip,
  ConfirmDialog,
  DEFAULT_PAGE_SIZE,
  LaunchSequence,
  MethodChip,
  methodLabel,
  Pager,
  Panel,
  useToast,
  ViewHead,
} from "../components";
import type {
  AgentInfo,
  AgentSdk,
  DeploymentInfo,
  HarnessDiscoveryCandidate,
  InspectedSkill,
  JobInfo,
  RuntimeDiscoveryCandidate,
  RuntimeImportResult,
  Toolkit,
} from "../lib/api";
import { api, ApiError } from "../lib/api";
import type { ModelSource } from "../lib/models";
import {
  CLAUDE_SDK_MODEL_SOURCE,
  CUSTOM_MODEL_OPTION,
  DEFAULT_MODEL_SOURCE,
  defaultModelFor,
  isCustomModelId,
  modelOptionsFor,
} from "../lib/models";

const BUILTIN_TOOLS = ["code-interpreter", "browser"] as const;

// Platform toolkits selectable for the Strands ZIP method. `tools` lists the tool
// names the backend will emit — kept here only so the chips can show the resulting
// tool surface without a round-trip; the backend registry
// (backend/app/templates/toolkits/) stays the source of truth for what is emitted.
// `prompt` is the wizard-offered default system prompt: deliberately generic, so an
// agent built from it has prompt-fixable defects a config-bundle A/B can repair.
const TOOLKITS: {
  name: Toolkit;
  tools: string[];
  prompt: string;
}[] = [
  {
    name: "hr_assistant",
    tools: [
      "get_pto_balance",
      "submit_pto_request",
      "lookup_hr_policy",
      "get_benefits_summary",
      "get_pay_stub",
    ],
    prompt: `You are a helpful HR Assistant for Acme Corp.

You help employees with:
- Checking PTO (paid time off) balances
- Submitting PTO requests
- Looking up HR policies (PTO, remote work, parental leave, code of conduct)
- Understanding employee benefits (health, dental, vision, 401k, life insurance)
- Retrieving pay stub information

Always use the available tools to answer questions accurately. Do not make up
policy details, benefit amounts, or pay information — look them up.
Be concise, professional, and friendly.`,
  },
];

const TOOLKIT_PROMPTS = TOOLKITS.map((kit) => kit.prompt);
// AgentCore mount-path contract: exactly one level under /mnt
const MOUNT_RE = /^\/mnt\/[a-zA-Z0-9._-]+$/;
const DEFAULT_SESSION_MOUNT = "/mnt/workspace";

const splitIds = (s: string) => s.split(/[\s,]+/).filter(Boolean);
const skillNameFromPath = (path: string) => path.replace(/\/+$/, "").split("/").pop() ?? path;

type Step = 1 | 2 | 3;

interface LaunchState {
  agentId: string;
  jobId: string;
}

type Method = "harness" | "zip_runtime" | "container";

// Which source a method starts on. The invariant: a method only defaults to
// mantle once its execution path can actually execute a Mantle model. The
// harness needs only bedrockModelConfig.apiFormat; the zip/Strands template now
// renders an OpenAIResponsesModel with bedrock_mantle_config (IAM auth, no API
// key) when the source is mantle, so it joined it. The container method stays on
// Claude — the Claude Agent SDK cannot drive anything else.
const MODEL_SOURCE_BY_METHOD: Record<Method, ModelSource> = {
  harness: DEFAULT_MODEL_SOURCE,
  container: CLAUDE_SDK_MODEL_SOURCE,
  zip_runtime: DEFAULT_MODEL_SOURCE,
};

// A2A zip agents render from a different template that has no Mantle branch, so
// they stay on the Converse path regardless of the method default.
const A2A_MODEL_SOURCE: ModelSource = "bedrock";

// The single member of the "Other Agent SDK" category (the container method).
// Selected by default and, for now, the only selectable value.
const DEFAULT_AGENT_SDK: AgentSdk = "claude_agent_sdk";

const sourceForMethod = (m: Method): ModelSource => MODEL_SOURCE_BY_METHOD[m];

// Spec fields we read back when loading an existing agent into the wizard.
interface StoredSpec {
  model_id?: string;
  model_source?: ModelSource;
  agent_sdk?: AgentSdk;
  system_prompt?: string;
  tools?: {
    type: string;
    name: string;
    config?: { url?: string; record_id?: string; gateway_id?: string };
  }[];
  toolkits?: Toolkit[];
  skills?: string[];
  knowledge_bases?: KbRef[];
  memory?: { long_term?: boolean };
  protocol?: "http" | "a2a";
  a2a_skills?: { id?: string; name?: string; description?: string; tags?: string[] }[];
  env?: Record<string, string>;
  filesystem?: {
    session_storage?: { mount_path?: string } | null;
    s3_files?: { access_point_arn?: string; mount_path?: string }[];
    efs?: { access_point_arn?: string; mount_path?: string }[];
  };
  network?: { subnets?: string[]; security_groups?: string[] };
}

interface MountRow {
  arn: string;
  path: string;
}

// agent-card skill editor row; tags edit as a comma-separated string
interface A2aSkillRow {
  name: string;
  description: string;
  tags: string;
}

const DISCOVERY_STATUS_TONE: Record<string, "good" | "warn" | "crit" | "muted"> = {
  READY: "good",
  CREATING: "warn",
  UPDATING: "warn",
  CREATE_FAILED: "crit",
  UPDATE_FAILED: "crit",
};

// Same rule for both discovered kinds: eligible, and not owned by a Launchpad
// agent (a previous import of the same resource may be refreshed).
const canSelectCandidate = (candidate: {
  importable: boolean;
  managed_agent_id: string | null;
  managed_agent_method: AgentInfo["method"] | null;
}) =>
  candidate.importable &&
  (!candidate.managed_agent_id || candidate.managed_agent_method === "discovered_runtime");

// One selection set spans both kinds, so keys carry their kind.
const runtimeKey = (runtimeId: string) => `rt:${runtimeId}`;
const harnessKey = (harnessId: string) => `hn:${harnessId}`;
const idsOfKind = (keys: Set<string>, prefix: string) =>
  [...keys].filter((key) => key.startsWith(prefix)).map((key) => key.slice(prefix.length));

// One merged table row. A managed Harness materializes a hidden backing Runtime
// named `harness_<harnessName>` (artifact_type "harness" in the scan) — the two
// are the same agent, so the pair folds into a single harness-kind row carrying
// both ids. Runtimes flagged harness-managed but unmatched (harness scan failed)
// stay visible as plain runtime rows; they are never importable anyway.
type DiscoveryRow =
  | {
      kind: "harness";
      key: string;
      harness: HarnessDiscoveryCandidate;
      backing: RuntimeDiscoveryCandidate | null;
    }
  | { kind: "runtime"; key: string; runtime: RuntimeDiscoveryCandidate };

const rowName = (row: DiscoveryRow) =>
  row.kind === "harness" ? row.harness.name : row.runtime.name;

const mergeDiscoveryRows = (
  runtimes: RuntimeDiscoveryCandidate[],
  harnesses: HarnessDiscoveryCandidate[],
): DiscoveryRow[] => {
  const backingByName = new Map(
    runtimes
      .filter((runtime) => runtime.artifact_type === "harness")
      .map((runtime) => [runtime.name, runtime]),
  );
  const consumed = new Set<string>();
  const rows: DiscoveryRow[] = harnesses.map((harness) => {
    const backing = backingByName.get(`harness_${harness.name}`) ?? null;
    if (backing) consumed.add(backing.runtime_id);
    return { kind: "harness", key: harnessKey(harness.harness_id), harness, backing };
  });
  for (const runtime of runtimes) {
    if (!consumed.has(runtime.runtime_id)) {
      rows.push({ kind: "runtime", key: runtimeKey(runtime.runtime_id), runtime });
    }
  }
  return rows.sort((a, b) => rowName(a).localeCompare(rowName(b)));
};

export function CreateAgent() {
  const [params] = useSearchParams();
  // Members reach the whole module: the list, details and the discovery scan
  // are reads. Each mutating action gates itself on the caller's granted
  // agent-management permissions (default granted, revocable per user in the
  // Users console — mirrors route_policy's perm:agents.*).
  return params.get("view") === "discover" ? <RuntimeDiscovery /> : <CreateAgentWizard />;
}

function RuntimeDiscovery() {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const toast = useToast();
  const { can } = useAuth();
  const canImport = can("agents.import");
  const [region, setRegion] = useState("");
  const [runtimes, setRuntimes] = useState<RuntimeDiscoveryCandidate[]>([]);
  const [harnesses, setHarnesses] = useState<HarnessDiscoveryCandidate[]>([]);
  const [harnessScanError, setHarnessScanError] = useState<string | null>(null);
  const [selected, setSelected] = useState<Set<string>>(() => new Set());
  const [loading, setLoading] = useState(true);
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [importResult, setImportResult] = useState<RuntimeImportResult | null>(null);
  const reasonText = (code?: string | null, fallback?: string | null) =>
    t(`create.discovery.reasons.${code ?? "unknown"}`, {
      defaultValue: fallback ?? code ?? "",
    });

  const load = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const result = await api.discoverRuntimes();
      setRegion(result.region);
      setRuntimes(result.runtimes);
      setHarnesses(result.harnesses);
      setHarnessScanError(result.harness_scan_error);
      setSelected((current) => {
        const available = new Set([
          ...result.runtimes
            .filter(canSelectCandidate)
            .map((runtime) => runtimeKey(runtime.runtime_id)),
          ...result.harnesses
            .filter(canSelectCandidate)
            .map((harness) => harnessKey(harness.harness_id)),
        ]);
        return new Set([...current].filter((key) => available.has(key)));
      });
    } catch (err) {
      setError(err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err));
    } finally {
      setLoading(false);
    }
  }, [t]);

  useEffect(() => {
    void load();
  }, [load]);

  const [kindFilter, setKindFilter] = useState<"all" | DiscoveryRow["kind"]>("all");
  const [protocolFilter, setProtocolFilter] = useState("all");
  const [artifactFilter, setArtifactFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  useEffect(() => {
    setPage(1); // filters change the result set — restart from page 1
  }, [kindFilter, protocolFilter, artifactFilter, query]);

  const allRows = useMemo(() => mergeDiscoveryRows(runtimes, harnesses), [runtimes, harnesses]);
  // Protocol/artifact only exist on runtime rows (harness scans carry neither),
  // so those filters implicitly narrow to runtimes when set to a concrete value.
  const protocols = useMemo(
    () =>
      [
        ...new Set(allRows.flatMap((row) => (row.kind === "runtime" ? [row.runtime.protocol] : []))),
      ].sort(),
    [allRows],
  );
  const artifacts = useMemo(
    () =>
      [
        ...new Set(
          allRows.flatMap((row) => (row.kind === "runtime" ? [row.runtime.artifact_type] : [])),
        ),
      ].sort(),
    [allRows],
  );
  const rows = allRows.filter((row) => {
    if (kindFilter !== "all" && row.kind !== kindFilter) return false;
    if (
      protocolFilter !== "all" &&
      (row.kind !== "runtime" || row.runtime.protocol !== protocolFilter)
    ) {
      return false;
    }
    if (
      artifactFilter !== "all" &&
      (row.kind !== "runtime" || row.runtime.artifact_type !== artifactFilter)
    ) {
      return false;
    }
    const q = query.trim().toLowerCase();
    if (q) {
      const haystack =
        row.kind === "harness"
          ? [
              row.harness.name,
              row.harness.harness_id,
              row.harness.description,
              row.backing?.runtime_id,
              row.backing?.description,
            ]
          : [row.runtime.name, row.runtime.runtime_id, row.runtime.description];
      if (!haystack.some((value) => value?.toLowerCase().includes(q))) return false;
    }
    return true;
  });
  const currentPage = Math.min(page, Math.max(1, Math.ceil(rows.length / pageSize)));
  const pageRows = rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  // Selection follows the current filters: header box and toolbar button span
  // every eligible FILTERED row (all pages); rows hidden by a filter keep their
  // selection so narrowing the view never silently drops picks.
  const selectableKeys = rows
    .filter((row) => canSelectCandidate(row.kind === "harness" ? row.harness : row.runtime))
    .map((row) => row.key);
  const allSelected =
    selectableKeys.length > 0 && selectableKeys.every((key) => selected.has(key));

  const toggle = (key: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  };

  const toggleAll = () => {
    setSelected((current) => {
      const next = new Set(current);
      for (const key of selectableKeys) {
        if (allSelected) next.delete(key);
        else next.add(key);
      }
      return next;
    });
  };

  const importSelected = async () => {
    if (!selected.size) return;
    setImporting(true);
    setError(null);
    try {
      const result = await api.importRuntimes(
        idsOfKind(selected, "rt:"),
        idsOfKind(selected, "hn:"),
      );
      setImportResult(result);
      toast(
        t("create.discovery.importSummary", {
          imported: result.imported.length,
          updated: result.updated.length,
          managed: result.already_managed.length,
          failed: result.failed.length,
        }),
        result.failed.length ? "warn" : "good",
      );
      setSelected(new Set());
      await load();
    } catch (err) {
      setError(err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err));
    } finally {
      setImporting(false);
    }
  };

  return (
    <section>
      <ViewHead
        kicker={t("create.discovery.kicker")}
        title={t("create.discovery.title")}
        meta={region ? t("create.discovery.region", { region }) : undefined}
      />
      <div className="discovery-toolbar">
        <Btn onClick={() => navigate("/create")}>
          <ArrowLeft size={14} aria-hidden="true" />
          {t("create.discovery.back")}
        </Btn>
        <div className="discovery-toolbar-actions">
          <Btn onClick={() => void load()} disabled={loading || importing}>
            <RefreshCw size={14} aria-hidden="true" />
            {t("create.discovery.refresh")}
          </Btn>
          <Btn onClick={toggleAll} disabled={!canImport || !selectableKeys.length || importing}>
            {t(allSelected ? "create.discovery.clearSelection" : "create.discovery.selectEligible")}
          </Btn>
          <span title={canImport ? undefined : t("create.permissionRequired")}>
            <Btn
              primary
              onClick={() => void importSelected()}
              disabled={!canImport || !selected.size || importing}
            >
              <Download size={14} aria-hidden="true" />
              {importing
                ? t("create.discovery.importing")
                : t("create.discovery.importSelected", { count: selected.size })}
            </Btn>
          </span>
        </div>
      </div>

      {error && (
        <div className="note discovery-error">
          <span className="i">[!]</span>
          <span>{error}</span>
        </div>
      )}
      {importResult && importResult.failed.length > 0 && (
        <div className="note discovery-error">
          <span className="i">[!]</span>
          <span>
            {importResult.failed
              .map(
                (item) =>
                  `${item.runtime_id ?? item.harness_id}: ${reasonText(
                    item.reason_code,
                    item.reason,
                  )}`,
              )
              .join(" · ")}
          </span>
        </div>
      )}
      {harnessScanError && (
        <div className="note discovery-error">
          <span className="i">[!]</span>
          <span>
            {t("create.discovery.harnessScanFailed")} {harnessScanError}
          </span>
        </div>
      )}

      <Panel
        title={t("create.discovery.results")}
        sub={t("create.discovery.count", { count: rows.length })}
        pad={false}
        className="discovery-results"
      >
        <div className="filters">
          <select
            className="fsel"
            value={kindFilter}
            onChange={(e) => setKindFilter(e.target.value as "all" | DiscoveryRow["kind"])}
            aria-label={t("create.discovery.columns.type")}
          >
            <option value="all">{t("create.discovery.filterKindAll")}</option>
            <option value="harness">{t("create.discovery.kindHarness")}</option>
            <option value="runtime">{t("create.discovery.kindRuntime")}</option>
          </select>
          <select
            className="fsel"
            value={protocolFilter}
            onChange={(e) => setProtocolFilter(e.target.value)}
            aria-label={t("create.discovery.columns.protocol")}
          >
            <option value="all">{t("create.discovery.filterProtocolAll")}</option>
            {protocols.map((protocol) => (
              <option key={protocol} value={protocol}>
                {protocol}
              </option>
            ))}
          </select>
          <select
            className="fsel"
            value={artifactFilter}
            onChange={(e) => setArtifactFilter(e.target.value)}
            aria-label={t("create.discovery.columns.artifact")}
          >
            <option value="all">{t("create.discovery.filterArtifactAll")}</option>
            {artifacts.map((artifact) => (
              <option key={artifact} value={artifact}>
                {artifact.toUpperCase()}
              </option>
            ))}
          </select>
          <input
            className="fsearch"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={t("create.discovery.searchPlaceholder")}
          />
        </div>
        <table className="discovery-table">
          <thead>
            <tr>
              <th className="discovery-check">
                <input
                  type="checkbox"
                  checked={allSelected}
                  disabled={!canImport || !selectableKeys.length}
                  onChange={toggleAll}
                  aria-label={t("create.discovery.selectEligible")}
                />
              </th>
              <th>{t("create.discovery.columns.resource")}</th>
              <th>{t("create.discovery.columns.type")}</th>
              <th>{t("create.discovery.columns.protocol")}</th>
              <th>{t("create.discovery.columns.artifact")}</th>
              <th>{t("create.discovery.columns.status")}</th>
              <th>{t("create.discovery.columns.auth")}</th>
              <th>{t("create.discovery.columns.version")}</th>
              <th>{t("create.discovery.columns.eligibility")}</th>
            </tr>
          </thead>
          <tbody>
            {pageRows.map((row) =>
              row.kind === "harness" ? (
                <HarnessRow
                  key={row.key}
                  row={row}
                  selected={selected.has(row.key)}
                  disabled={!canImport || !canSelectCandidate(row.harness) || importing}
                  onToggle={() => toggle(row.key)}
                  reasonText={reasonText}
                />
              ) : (
                <RuntimeRow
                  key={row.key}
                  runtime={row.runtime}
                  selected={selected.has(row.key)}
                  disabled={!canImport || !canSelectCandidate(row.runtime) || importing}
                  onToggle={() => toggle(row.key)}
                  reasonText={reasonText}
                />
              ),
            )}
            {!loading && rows.length === 0 && (
              <tr>
                <td colSpan={9} className="empty">
                  {t(allRows.length ? "create.discovery.noMatch" : "create.discovery.empty")}
                </td>
              </tr>
            )}
            {loading && (
              <tr>
                <td colSpan={9} className="loading-line">
                  {t("create.discovery.scanning")}
                </td>
              </tr>
            )}
          </tbody>
        </table>
        <Pager
          total={rows.length}
          page={currentPage}
          size={pageSize}
          onPage={setPage}
          onSize={(size) => {
            setPageSize(size);
            setPage(1);
          }}
        />
      </Panel>
    </section>
  );
}

// A harness with its backing runtime folded in: one agent, two AWS ids. Status,
// version and eligibility come from the harness — it is the invokable resource;
// the backing runtime only contributes its id (and description, which harness
// summaries never carry).
function HarnessRow({
  row,
  selected,
  disabled,
  onToggle,
  reasonText,
}: {
  row: Extract<DiscoveryRow, { kind: "harness" }>;
  selected: boolean;
  disabled: boolean;
  onToggle: () => void;
  reasonText: (code?: string | null, fallback?: string | null) => string;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const { harness, backing } = row;
  const externallyManaged = harness.managed_agent_method === "discovered_runtime";
  const description = harness.description || backing?.description;
  return (
    <tr>
      <td className="discovery-check">
        <input
          type="checkbox"
          checked={selected}
          disabled={disabled}
          onChange={onToggle}
          aria-label={t("create.discovery.selectHarness", { name: harness.name })}
        />
      </td>
      <td>
        <div className="runtime-name" title={harness.harness_arn}>
          <b>{harness.name}</b>
          <span>{harness.harness_id}</span>
          {backing && (
            <span title={backing.runtime_arn}>
              {t("create.discovery.backingRuntime", { id: backing.runtime_id })}
            </span>
          )}
          {description && <small>{description}</small>}
        </div>
      </td>
      <td>
        <Chip tone="aqua">{t("create.discovery.kindHarness")}</Chip>
      </td>
      <td className="mono">—</td>
      <td className="mono">—</td>
      <td>
        <Chip tone={DISCOVERY_STATUS_TONE[harness.aws_status] ?? "muted"}>
          {harness.aws_status}
        </Chip>
      </td>
      <td className="mono">—</td>
      <td className="mono">{harness.version || "—"}</td>
      <td className="runtime-reason">
        {externallyManaged ? (
          <>
            <Chip tone="aqua">{t("create.discovery.alreadyImported")}</Chip>{" "}
            <span>{t("create.discovery.reimportHint")}</span>
          </>
        ) : harness.managed_agent_id ? (
          <button type="button" className="rowact" onClick={() => navigate("/create")}>
            {t("create.discovery.alreadyManaged", {
              name: harness.managed_agent_name ?? harness.name,
            })}
          </button>
        ) : !harness.importable ? (
          <span>{reasonText(harness.reason_code, harness.reason)}</span>
        ) : (
          <span className="discovery-ready">{t("create.discovery.harnessReadyToImport")}</span>
        )}
      </td>
    </tr>
  );
}

function RuntimeRow({
  runtime,
  selected,
  disabled,
  onToggle,
  reasonText,
}: {
  runtime: RuntimeDiscoveryCandidate;
  selected: boolean;
  disabled: boolean;
  onToggle: () => void;
  reasonText: (code?: string | null, fallback?: string | null) => string;
}) {
  const { t } = useTranslation();
  const navigate = useNavigate();
  const externallyManaged = runtime.managed_agent_method === "discovered_runtime";
  return (
    <tr>
      <td className="discovery-check">
        <input
          type="checkbox"
          checked={selected}
          disabled={disabled}
          onChange={onToggle}
          aria-label={t("create.discovery.selectRuntime", { name: runtime.name })}
        />
      </td>
      <td>
        <div className="runtime-name" title={runtime.runtime_arn}>
          <b>{runtime.name}</b>
          <span>{runtime.runtime_id}</span>
          {runtime.description && <small>{runtime.description}</small>}
        </div>
      </td>
      <td>
        <Chip tone="blue">{t("create.discovery.kindRuntime")}</Chip>
      </td>
      <td>
        <Chip tone={runtime.protocol === "HTTP" ? "blue" : "aqua"}>{runtime.protocol}</Chip>
      </td>
      <td>
        <Chip tone="muted">{runtime.artifact_type.toUpperCase()}</Chip>
      </td>
      <td>
        <Chip tone={DISCOVERY_STATUS_TONE[runtime.aws_status] ?? "muted"}>
          {runtime.aws_status}
        </Chip>
      </td>
      <td className="mono">
        {runtime.authorizer_type === "custom_jwt"
          ? t("create.discovery.customJwt")
          : runtime.authorizer_type.toUpperCase()}
      </td>
      <td className="mono">{runtime.version || "—"}</td>
      <td className="runtime-reason">
        {externallyManaged ? (
          <>
            <Chip tone="aqua">{t("create.discovery.alreadyImported")}</Chip>{" "}
            <span>{t("create.discovery.reimportHint")}</span>
          </>
        ) : runtime.managed_agent_id ? (
          <button type="button" className="rowact" onClick={() => navigate("/create")}>
            {t("create.discovery.alreadyManaged", {
              name: runtime.managed_agent_name ?? runtime.name,
            })}
          </button>
        ) : !runtime.importable ? (
          <span>{reasonText(runtime.reason_code, runtime.reason)}</span>
        ) : !runtime.invoke_capability.eligible ? (
          <span>
            {t("create.discovery.inventoryOnly")}:{" "}
            {reasonText(
              runtime.invoke_capability.reason_code,
              runtime.invoke_capability.reason,
            )}
          </span>
        ) : (
          <span className="discovery-ready">{t("create.discovery.readyToImport")}</span>
        )}
      </td>
    </tr>
  );
}

// the two demo tools every zip template ships — seed the skills editor
const A2A_SKILL_SEEDS: A2aSkillRow[] = [
  { name: "calculator", description: "Evaluate a basic arithmetic expression", tags: "math" },
  { name: "current time", description: "Report the current UTC date and time", tags: "time" },
];

// backend A2ASkill.id pattern is ^[a-z][a-z0-9_-]{0,63}$ — leading letter required
const skillSlug = (name: string) =>
  name
    .toLowerCase()
    .replace(/[^a-z0-9_-]+/g, "-")
    .replace(/^[^a-z]+/, "")
    .replace(/-+$/, "")
    .slice(0, 64) || "skill";

// rows edit by name; ids must be unique in the spec → suffix repeats (faq, faq-2, …)
const skillIds = (rows: A2aSkillRow[]): string[] => {
  const used = new Set<string>();
  return rows.map((row) => {
    const base = skillSlug(row.name);
    let id = base;
    for (let n = 2; used.has(id); n += 1) id = `${base.slice(0, 60)}-${n}`;
    used.add(id);
    return id;
  });
};

// APPROVED registry records the wizard offers for mounting.
interface AttachableMcp {
  name: string;
  description: string;
  url: string;
  gateway: boolean;
  record_id: string;
  gateway_id: string | null;
  gateway_arn: string | null;
  attachable: boolean;
  attachability_reason: string | null;
  auth_type: "aws_iam" | "none" | "oauth" | null;
}
interface AttachableSkill {
  name: string;
  description: string;
  path: string;
}
// A managed KB offered by the catalog (only ACTIVE + MANAGED are selectable).
interface AttachableKb {
  kb_id: string;
  name: string;
  description?: string;
  status?: string;
  type?: string;
}
// The redundant KB reference stored in the agent spec (name/description carried
// so the wizard can still render a chip if the KB later leaves the catalog).
interface KbRef {
  kb_id: string;
  name: string;
  description: string;
}

function CreateAgentWizard() {
  const { t } = useTranslation();
  const toast = useToast();
  const navigate = useNavigate();
  const { can } = useAuth();
  const canDeploy = can("agents.deploy");
  const [params] = useSearchParams();
  const prefillGateway = params.get("gateway");
  const prefillSkill = params.get("skill");
  const [step, setStep] = useState<Step>(prefillGateway || prefillSkill ? 2 : 1);
  const [method, setMethod] = useState<Method>("harness");
  const [skills, setSkills] = useState<string[]>(prefillSkill ? [prefillSkill] : []);
  const [name, setName] = useState("");
  const [modelId, setModelId] = useState(defaultModelFor(DEFAULT_MODEL_SOURCE));
  const [modelSource, setModelSource] = useState<ModelSource>(DEFAULT_MODEL_SOURCE);
  // true ⇒ the model dropdown sits on "Custom model ID…" and the free-text input shows
  const [customModel, setCustomModel] = useState(false);
  // container method only — the "Other Agent SDK" second-level choice
  const [agentSdk, setAgentSdk] = useState<AgentSdk>(DEFAULT_AGENT_SDK);
  const [systemPrompt, setSystemPrompt] = useState("");
  const [tools, setTools] = useState<string[]>([]);
  // Platform toolkits (zip_runtime only) — local @tool functions the backend
  // inlines into the generated agent, replacing the template's own two tools.
  const [toolkits, setToolkits] = useState<Toolkit[]>([]);
  const [gatewayTargets, setGatewayTargets] = useState<AttachableMcp[]>([]);
  const [remoteMcp, setRemoteMcp] = useState<AttachableMcp[]>([]);
  const [skillCatalog, setSkillCatalog] = useState<AttachableSkill[]>([]);
  const [selectedGateway, setSelectedGateway] = useState<string[]>(
    prefillGateway ? [prefillGateway] : [],
  );
  const [storedGatewayConfig, setStoredGatewayConfig] = useState<
    Record<string, { record_id: string; gateway_id: string }>
  >({});
  const [selectedMcp, setSelectedMcp] = useState<string[]>([]);
  const [kbCatalog, setKbCatalog] = useState<AttachableKb[]>([]);
  const [selectedKbs, setSelectedKbs] = useState<string[]>([]);
  // KB refs carried in the loaded spec — name fallback for KBs no longer in the catalog.
  const [specKbs, setSpecKbs] = useState<KbRef[]>([]);
  // KBs shown read-only on the step-3 detail view (viewed agent or just-published).
  const [detailKbs, setDetailKbs] = useState<KbRef[]>([]);
  const [detailConversion, setDetailConversion] = useState<{
    source: string;
    notes: Record<string, string>;
  } | null>(null);
  const [longTerm, setLongTerm] = useState(true);
  const [mcpServers, setMcpServers] = useState("");
  // custom skill sources attached without a registry record (name shown on the chip)
  const [customSkills, setCustomSkills] = useState<{ name: string; path: string }[]>([]);
  const [pendingSkills, setPendingSkills] = useState<{
    stagingId: string;
    skills: InspectedSkill[];
    picked: number[];
  } | null>(null);
  const [gitOpen, setGitOpen] = useState(false);
  const [gitUrl, setGitUrl] = useState("");
  const [srcBusy, setSrcBusy] = useState(false);
  const skillFileRef = useRef<HTMLInputElement>(null);
  // AgentCore Runtime filesystem configuration (container method only)
  const [sessionFs, setSessionFs] = useState(true);
  const [sessionMount, setSessionMount] = useState(DEFAULT_SESSION_MOUNT);
  const [s3Mounts, setS3Mounts] = useState<MountRow[]>([]);
  const [efsMounts, setEfsMounts] = useState<MountRow[]>([]);
  const [vpcSubnets, setVpcSubnets] = useState("");
  const [vpcSgs, setVpcSgs] = useState("");
  // zip runtime service protocol: standard HTTP invocations vs a real A2A
  // JSON-RPC server (serverProtocol=A2A) with configurable agent-card skills
  const [protocol, setProtocol] = useState<"http" | "a2a">("http");
  const [a2aSkills, setA2aSkills] = useState<A2aSkillRow[]>([]);
  // when set, the wizard edits an existing agent and the launch button re-publishes it
  const [editing, setEditing] = useState<{ id: string; name: string; method: Method } | null>(null);
  const [detailsMode, setDetailsMode] = useState(false);

  useEffect(() => {
    // Mountable assets come from the registry catalog: only APPROVED records
    // are offered, so the registry lifecycle gates availability.
    fetch("/api/registry/attachables")
      .then((res) => (res.ok ? res.json() : { mcp_servers: [], skills: [] }))
      .then((d: { mcp_servers: AttachableMcp[]; skills: AttachableSkill[] }) => {
        setGatewayTargets(d.mcp_servers.filter((m) => m.gateway));
        setRemoteMcp(d.mcp_servers.filter((m) => !m.gateway));
        setSkillCatalog(d.skills);
      })
      .catch(() => {
        /* registry not bootstrapped — chips stay hidden */
      });
    // Managed KB catalog — failures are tolerated: an empty catalog just leaves
    // the Knowledge section empty and never blocks the wizard.
    fetch("/api/knowledge-bases")
      .then((res) => (res.ok ? res.json() : { items: [] }))
      .then((d: { items: AttachableKb[] }) => setKbCatalog(d.items ?? []))
      .catch(() => {
        /* KB catalog unavailable — section stays empty */
      });
  }, []);

  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const reloadAgents = useCallback(() => {
    void api
      .listAgents()
      .then((res) => setAgents(res.agents))
      .catch(() => {
        /* list is best-effort — a fetch blip shouldn't blank the page */
      });
  }, []);
  useEffect(() => reloadAgents(), [reloadAgents]);

  // All three wizard methods can mount KBs — harness through launchpad-kb-gw,
  // zip_runtime/container through a generated kb_search tool. Only the Studio
  // canvas (its own page) has no retrieval contract, so no reset here.

  const [submitError, setSubmitError] = useState<string | null>(null);
  const [launch, setLaunch] = useState<LaunchState | null>(null);
  const [deployment, setDeployment] = useState<DeploymentInfo | null>(null);
  const [job, setJob] = useState<JobInfo | null>(null);
  const [agentStatus, setAgentStatus] = useState<string>("deploying");
  const [confirm, setConfirm] = useState<
    | { kind: "republish" }
    | { kind: "delete"; id: string; name: string; external: boolean }
    | { kind: "convert"; id: string; name: string }
    | null
  >(null);

  const failureToasted = useRef(false);
  const poll = useCallback(async () => {
    if (!launch) return;
    try {
      const agent = await api.getAgent(launch.agentId);
      setDeployment(agent.deployments?.[0] ?? null);
      setJob(await api.getJob(launch.jobId));
      if (agent.status === "failed" && !failureToasted.current) {
        failureToasted.current = true;
        const failedStage = (agent.deployments?.[0]?.stages ?? []).find(
          (s) => s.status === "failed",
        );
        toast(
          t("create.launchFailedToast", {
            stage: failedStage?.name ?? "deploy",
            msg: (failedStage?.detail ?? "").slice(0, 120),
          }),
        );
      }
      setAgentStatus(agent.status);
    } catch {
      /* transient poll errors are retried on the next tick */
    }
  }, [launch, t, toast]);

  useEffect(() => {
    if (!launch) return;
    void poll(); // always load once (covers read-only "details" of a finished deploy)
    if (agentStatus === "active" || agentStatus === "failed") return;
    const timer = setInterval(() => void poll(), 2000);
    return () => clearInterval(timer);
  }, [launch, agentStatus, poll]);


  // A2A zip agents render from strands_a2a_agent, which has no Mantle branch —
  // the Model source control is hidden and pinned to A2A_MODEL_SOURCE for them.
  const isA2a = method === "zip_runtime" && protocol === "a2a";

  // Switching source re-seeds the model to that source's catalog default.
  const applyModelSource = (source: ModelSource) => {
    setModelSource(source);
    setModelId(defaultModelFor(source));
    setCustomModel(false);
  };

const deployLock = !canDeploy
    ? ({ opacity: 0.45, pointerEvents: "none" } as CSSProperties)
    : undefined;
  
  const pickMethod = (next: Method) => {
    if (next === method) return;
    setMethod(next);
    // protocol survives a method switch, so re-entering zip_runtime with A2A
    // still selected must land back on the pinned source, not the default.
    applyModelSource(
      next === "zip_runtime" && protocol === "a2a" ? A2A_MODEL_SOURCE : sourceForMethod(next),
    );
  };

  const resetForm = () => {
    setEditing(null);
    setDetailsMode(false);
    setName("");
    applyModelSource(sourceForMethod(method));
    setAgentSdk(DEFAULT_AGENT_SDK);
    setSystemPrompt("");
    setTools([]);
    setToolkits([]);
    setSelectedGateway([]);
    setStoredGatewayConfig({});
    setSelectedMcp([]);
    setSelectedKbs([]);
    setSpecKbs([]);
    setDetailKbs([]);
    setSkills([]);
    setLongTerm(true);
    setMcpServers("");
    setCustomSkills([]);
    setPendingSkills(null);
    setGitOpen(false);
    setGitUrl("");
    setSessionFs(true);
    setSessionMount(DEFAULT_SESSION_MOUNT);
    setS3Mounts([]);
    setEfsMounts([]);
    setVpcSubnets("");
    setVpcSgs("");
    setProtocol("http");
    setA2aSkills([]);
    setSubmitError(null);
  };

  const byoMounts = s3Mounts.length > 0 || efsMounts.length > 0;

  // Tool names the selected toolkits contribute. Non-empty ⇒ they replace the
  // template's own calculator/current_utc_time, matching what the backend emits.
  const toolkitToolNames = TOOLKITS.filter((kit) => toolkits.includes(kit.name)).flatMap(
    (kit) => kit.tools,
  );

  // Shared by the harness and zip_runtime tool blocks — a gateway attachment is
  // the same selection for both; only who performs the token exchange differs.
  const gatewayChips = () =>
    gatewayTargets.map((target) => (
      <button
        key={target.record_id}
        type="button"
        data-testid={`gateway-${target.name}`}
        className={`selchip${selectedGateway.includes(target.name) ? " on" : ""}`}
        disabled={!target.attachable}
        style={{ cursor: target.attachable ? "pointer" : "not-allowed" }}
        title={target.attachability_reason ?? target.description}
        onClick={() => {
          if (!target.attachable) return;
          setSelectedGateway((prev) =>
            prev.includes(target.name)
              ? prev.filter((x) => x !== target.name)
              : [...prev, target.name],
          );
        }}
      >
        {target.name} · gateway{" "}
        {target.attachable ? (selectedGateway.includes(target.name) ? "✓" : "+") : "—"}
      </button>
    ));

  const toggleToolkit = (kit: (typeof TOOLKITS)[number]) => {
    const on = toolkits.includes(kit.name);
    setToolkits((prev) => (on ? prev.filter((x) => x !== kit.name) : [...prev, kit.name]));
    if (on) return;
    // Offer the toolkit's default prompt, but never clobber the user's own text —
    // only an empty box or another toolkit's untouched default is replaced.
    setSystemPrompt((prev) =>
      !prev.trim() || TOOLKIT_PROMPTS.includes(prev) ? kit.prompt : prev,
    );
  };

  // Resolve a KB id to its name/description, preferring the live catalog and
  // falling back to the loaded spec so out-of-catalog KBs keep their label.
  const kbInfo = (id: string): KbRef => {
    const cat = kbCatalog.find((k) => k.kb_id === id);
    if (cat) return { kb_id: id, name: cat.name, description: cat.description ?? "" };
    const stored = specKbs.find((k) => k.kb_id === id);
    return { kb_id: id, name: stored?.name ?? id, description: stored?.description ?? "" };
  };

  // Only ACTIVE managed KBs are selectable; the catalog may already exclude
  // non-managed KBs, so the type guard is defensive.
  const activeKbs = kbCatalog.filter(
    (k) => k.status === "ACTIVE" && (k.type == null || k.type === "MANAGED"),
  );

  // Gateway attachments as ToolRefs. Shared by the harness and zip_runtime
  // branches below: the harness service performs the token exchange declaratively,
  // a generated runtime does it in code, but the spec shape is the same one.
  const gatewayToolRefs = () =>
    selectedGateway.map((n) => {
      const server = gatewayTargets.find((item) => item.name === n);
      const config =
        server?.record_id && server.gateway_id
          ? { record_id: server.record_id, gateway_id: server.gateway_id }
          : storedGatewayConfig[n];
      return { type: "gateway", name: n, ...(config ? { config } : {}) };
    });

  const buildSpec = () => ({
    name,
    method,
    model_id: modelId.trim(), // a pasted custom id may carry stray whitespace
    model_source: modelSource,
    // container only — the other methods have no SDK choice to express
    ...(method === "container" ? { agent_sdk: agentSdk } : {}),
    system_prompt: systemPrompt,
    tools:
      method === "harness"
        ? [
            ...tools.map((n) => ({ type: "builtin", name: n })),
            ...gatewayToolRefs(),
            ...selectedMcp.flatMap((n) => {
              const server = remoteMcp.find((m) => m.name === n);
              return server ? [{ type: "mcp", name: n, config: { url: server.url } }] : [];
            }),
          ]
        : method === "container"
          ? selectedMcp.flatMap((n) => {
              const server = remoteMcp.find((m) => m.name === n);
              return server ? [{ type: "mcp", name: n, config: { url: server.url } }] : [];
            })
          : // An HTTP zip runtime calls the shared Gateway from generated client
            // code; the A2A template carries no MCP client.
            method === "zip_runtime" && protocol === "http"
            ? gatewayToolRefs()
            : [],
    memory: { short_term: true, long_term: longTerm },
    ...(method === "zip_runtime"
      ? {
          protocol,
          ...(protocol === "a2a"
            ? {
                a2a_skills: (() => {
                  const rows = a2aSkills.filter((s) => s.name.trim());
                  const ids = skillIds(rows);
                  return rows.map((s, i) => ({
                    id: ids[i],
                    name: s.name.trim(),
                    description: s.description.trim(),
                    tags: s.tags.split(",").map((x) => x.trim()).filter(Boolean),
                  }));
                })(),
              }
            : {}),
        }
      : {}),
    // zip_runtime only, and never together with A2A — the backend rejects both.
    ...(method === "zip_runtime" && protocol === "http" && toolkits.length
      ? { toolkits }
      : {}),
    ...(selectedKbs.length ? { knowledge_bases: selectedKbs.map(kbInfo) } : {}),
    ...((method === "harness" || method === "container" || method === "zip_runtime") &&
    skills.length
      ? { skills }
      : {}),
    ...(method === "container" && mcpServers.trim()
      ? { env: { LAUNCHPAD_MCP_SERVERS: mcpServers.trim() } }
      : {}),
    ...(method === "container"
      ? {
          filesystem: {
            session_storage: sessionFs ? { mount_path: sessionMount } : null,
            s3_files: s3Mounts.map((m) => ({ access_point_arn: m.arn, mount_path: m.path })),
            efs: efsMounts.map((m) => ({ access_point_arn: m.arn, mount_path: m.path })),
          },
          ...(byoMounts
            ? { network: { subnets: splitIds(vpcSubnets), security_groups: splitIds(vpcSgs) } }
            : {}),
        }
      : {}),
  });

  const submit = async () => {
    setSubmitError(null);
    try {
      const spec = buildSpec();
      const res = editing
        ? await api.redeployAgent(editing.id, spec)
        : await api.createAgent(spec);
      setDetailKbs((spec as { knowledge_bases?: KbRef[] }).knowledge_bases ?? []);
      failureToasted.current = false;
      setLaunch({ agentId: res.agent.id, jobId: res.job_id });
      setAgentStatus("deploying");
      setDetailsMode(false);
      setStep(3);
      reloadAgents();
    } catch (err) {
      setSubmitError(
        err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err),
      );
    }
  };

  const startEdit = (agent: AgentInfo) => {
    const spec = (agent.spec ?? {}) as StoredSpec;
    setEditing({ id: agent.id, name: agent.name, method: agent.method as Method });
    setDetailsMode(false);
    setMethod(agent.method as Method);
    setName(agent.name);
    // A spec stored before model_source existed is a Converse-API agent, never
    // Mantle; an id in neither catalog rides the "Custom model ID…" branch.
    const storedModel = spec.model_id ?? defaultModelFor("bedrock");
    const storedSource = spec.model_source ?? "bedrock";
    setModelId(storedModel);
    setModelSource(storedSource);
    // Custom unless the id is actually offered for the stored source — covers
    // unknown ids, an id belonging to the other source, and a non-Claude id on a
    // Claude Agent SDK agent.
    setCustomModel(isCustomModelId(storedModel, storedSource, agent.method === "container"));
    // absent on every container spec written before the SDK choice existed
    setAgentSdk(spec.agent_sdk ?? DEFAULT_AGENT_SDK);
    setSystemPrompt(spec.system_prompt ?? "");
    setTools((spec.tools ?? []).filter((x) => x.type === "builtin").map((x) => x.name));
    const gatewayTools = (spec.tools ?? []).filter((x) => x.type === "gateway");
    setSelectedGateway(gatewayTools.map((x) => x.name));
    setStoredGatewayConfig(
      Object.fromEntries(
        gatewayTools.flatMap((tool) =>
          tool.config?.record_id && tool.config.gateway_id
            ? [[
                tool.name,
                {
                  record_id: tool.config.record_id,
                  gateway_id: tool.config.gateway_id,
                },
              ]]
            : [],
        ),
      ),
    );
    setSelectedMcp((spec.tools ?? []).filter((x) => x.type === "mcp").map((x) => x.name));
    // absent on every zip spec written before toolkits existed
    setToolkits((spec.toolkits ?? []).filter((k) => TOOLKITS.some((x) => x.name === k)));
    setSelectedKbs((spec.knowledge_bases ?? []).map((k) => k.kb_id));
    setSpecKbs(spec.knowledge_bases ?? []);
    setSkills(spec.skills ?? []);
    setLongTerm(spec.memory?.long_term ?? true);
    setMcpServers(spec.env?.LAUNCHPAD_MCP_SERVERS ?? "");
    // custom (non-registry) skill paths get their chip name from the path tail
    setCustomSkills(
      (spec.skills ?? [])
        .filter((p) => p.includes("/agent-skills/"))
        .map((p) => ({ name: skillNameFromPath(p), path: p })),
    );
    setPendingSkills(null);
    const fs = spec.filesystem;
    setSessionFs(fs ? fs.session_storage != null : true);
    setSessionMount(fs?.session_storage?.mount_path ?? DEFAULT_SESSION_MOUNT);
    setS3Mounts(
      (fs?.s3_files ?? []).map((m) => ({ arn: m.access_point_arn ?? "", path: m.mount_path ?? "" })),
    );
    setEfsMounts(
      (fs?.efs ?? []).map((m) => ({ arn: m.access_point_arn ?? "", path: m.mount_path ?? "" })),
    );
    setVpcSubnets((spec.network?.subnets ?? []).join(", "));
    setVpcSgs((spec.network?.security_groups ?? []).join(", "));
    setProtocol(spec.protocol ?? "http");
    setA2aSkills(
      (spec.a2a_skills ?? []).map((s) => ({
        name: s.name ?? "",
        description: s.description ?? "",
        tags: (s.tags ?? []).join(", "),
      })),
    );
    setSubmitError(null);
    setStep(2);
  };

  const openDetails = (agent: AgentInfo) => {
    const jobId = agent.deployment?.job_id;
    if (!jobId) return;
    setEditing(null);
    setDetailsMode(true);
    setDetailKbs(((agent.spec ?? {}) as StoredSpec).knowledge_bases ?? []);
    const spec = (agent.spec ?? {}) as Record<string, unknown>;
    const src = spec.source_harness as { agent_name?: string } | undefined;
    setDetailConversion(
      src?.agent_name
        ? { source: src.agent_name,
            notes: (spec.conversion_notes as Record<string, string>) ?? {} }
        : null,
    );
    failureToasted.current = true; // don't re-toast an old failure when merely viewing
    setDeployment(agent.deployment ?? null);
    setJob(null);
    setLaunch({ agentId: agent.id, jobId });
    setAgentStatus(agent.status);
    setStep(3);
  };

  const doDelete = async (id: string) => {
    try {
      await api.deleteAgent(id);
      toast(t("create.list.deleted"));
      reloadAgents();
    } catch (err) {
      toast(err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err));
    }
  };

  const doConvert = async (id: string) => {
    try {
      const res = await api.convertAgent(id);
      toast(t("create.list.convertStarted", { name: res.agent.name }));
      reloadAgents();
    } catch (err) {
      toast(err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err));
    }
  };

  const toggleTool = (tool: string) =>
    setTools((prev) => (prev.includes(tool) ? prev.filter((x) => x !== tool) : [...prev, tool]));

  const toggleKb = (id: string) =>
    setSelectedKbs((prev) => (prev.includes(id) ? prev.filter((x) => x !== id) : [...prev, id]));

  /* ── custom skill sources: inspect (zip/git) → pick → attach ──────────── */

  const apiMsg = (err: unknown) =>
    err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err);

  const attachStaged = useCallback(
    async (stagingId: string, indices: number[]) => {
      const res = await api.attachSkillSources(
        stagingId,
        indices.map((index) => ({ index })),
      );
      const attached = res.skills.filter((s) => s.ok && s.path);
      const failed = res.skills.filter((s) => !s.ok);
      if (attached.length) {
        setSkills((prev) => [...prev, ...attached.map((s) => s.path as string)]);
        setCustomSkills((prev) => [
          ...prev,
          ...attached.map((s) => ({ name: s.name, path: s.path as string })),
        ]);
      }
      for (const item of failed) toast(`${item.name}: ${item.error ?? "attach failed"}`);
      return failed.length === 0;
    },
    [toast],
  );

  const inspectSource = async (input: File | { url: string }) => {
    setSrcBusy(true);
    try {
      const res =
        input instanceof File
          ? await api.inspectSkillZip(input)
          : await api.inspectSkillGit(input.url);
      const valid = res.skills.filter((s) => s.valid);
      if (valid.length === 1 && res.skills.length === 1) {
        // single-skill source (typical zip) — attach straight away
        if (await attachStaged(res.staging_id, [valid[0].index])) {
          setGitOpen(false);
          setGitUrl("");
        }
      } else {
        // monorepo — let the user pick which skills to attach
        setPendingSkills({ stagingId: res.staging_id, skills: res.skills, picked: [] });
      }
    } catch (err) {
      toast(apiMsg(err));
    } finally {
      setSrcBusy(false);
    }
  };

  const attachPicked = async () => {
    if (!pendingSkills || pendingSkills.picked.length === 0) return;
    setSrcBusy(true);
    try {
      if (await attachStaged(pendingSkills.stagingId, pendingSkills.picked)) {
        setPendingSkills(null);
        setGitOpen(false);
        setGitUrl("");
      }
    } catch (err) {
      toast(apiMsg(err));
    } finally {
      setSrcBusy(false);
    }
  };

  /* ── filesystem validation (container) ────────────────────────────────── */

  const fsPaths = [
    ...(sessionFs ? [sessionMount] : []),
    ...s3Mounts.map((m) => m.path),
    ...efsMounts.map((m) => m.path),
  ];
  const fsValid =
    method !== "container" ||
    ((!sessionFs || MOUNT_RE.test(sessionMount)) &&
      [...s3Mounts, ...efsMounts].every((m) => m.arn.trim().length > 0 && MOUNT_RE.test(m.path)) &&
      new Set(fsPaths).size === fsPaths.length &&
      (!byoMounts || (splitIds(vpcSubnets).length > 0 && splitIds(vpcSgs).length > 0)));

  const gatewaySelectionsValid = selectedGateway.every((name) => {
    const live = gatewayTargets.find((gateway) => gateway.name === name);
    if (live) return live.attachable;
    return storedGatewayConfig[name] == null;
  });
  const configValid =
    /^[a-z][a-z0-9-]{2,47}$/.test(name) &&
    systemPrompt.trim().length > 0 &&
    // catalog picks are always non-empty; guards a cleared "Custom model ID…" input
    modelId.trim().length > 0 &&
    fsValid &&
    gatewaySelectionsValid;

  return (
    <section>
      <ViewHead kicker={t("create.kicker")} title={t("create.title")} meta={t("create.meta")} />

      <div className="steps">
        {([1, 2, 3] as const).map((n) => (
          <div key={n} className={`step${step === n ? " now" : step > n ? " done" : ""}`}>
            <span className="n">{step > n ? "✓" : `0${n}`}</span>
            <b>{t(`create.steps.${n}`)}</b>
          </div>
        ))}
      </div>

      {step === 1 && (
        <>
          {!canDeploy && (
            <div className="note" style={{ marginBottom: 14 }}>
              <span className="i">[!]</span>
              <span>{t("create.permissionRequired")}</span>
            </div>
          )}
          <div className="methods">
            <div
              className={`method${method === "harness" ? " sel" : ""}`}
              style={{ "--i": 0, ...deployLock } as CSSProperties}
              onClick={() => pickMethod("harness")}
              data-method="harness"
            >
              <div className="m-badge">{t("create.methods.harness.badge")}</div>
              <div className="m-icon">◇</div>
              <h3>{t("create.methods.harness.title")}</h3>
              <p>{t("create.methods.harness.desc")}</p>
              <div className="m-specs">
                <span>CreateHarness · InvokeHarness</span>
                <span>{t("create.methods.harness.spec2")}</span>
                <span>{t("create.methods.harness.spec3")}</span>
              </div>
            </div>
            <div
              className={`method${method === "zip_runtime" ? " sel" : ""}`}
              style={{ "--i": 1, ...deployLock } as CSSProperties}
              onClick={() => pickMethod("zip_runtime")}
              data-method="zip_runtime"
            >
              <div className="m-badge">{t("create.methods.studio.badge")}</div>
              <div className="m-icon">⬡</div>
              <h3>{t("create.methods.studio.title")}</h3>
              <p>{t("create.methods.studio.desc")}</p>
              <div className="m-specs">
                <span>pip (arm64) → zip → S3 → Runtime</span>
                <span>{t("create.methods.studio.spec2")}</span>
                <span>{t("create.methods.studio.spec3")}</span>
              </div>
              <Link
                className="studio-link"
                to="/create/studio"
                onClick={(e) => e.stopPropagation()}
              >
                {t("create.methods.studio.open")}
              </Link>
            </div>
            <div
              className={`method${method === "container" ? " sel" : ""}`}
              style={{ "--i": 2, ...deployLock } as CSSProperties}
              onClick={() => pickMethod("container")}
              data-method="container"
            >
              <div className="m-badge">{t("create.methods.otherSdk.badge")}</div>
              <div className="m-icon">▣</div>
              <h3>{t("create.methods.otherSdk.title")}</h3>
              <p>{t("create.methods.otherSdk.desc")}</p>
              <div className="m-specs">
                <span>CodeBuild → ECR → Runtime</span>
                <span>{t("create.methods.otherSdk.spec2")}</span>
                <span>{t("create.methods.otherSdk.spec3")}</span>
              </div>
            </div>
            <button
              type="button"
              className="method discovery-method"
              style={{ "--i": 3 } as CSSProperties}
              onClick={() => navigate("/create?view=discover")}
              data-method="discovery"
            >
              <div className="m-badge plain">{t("create.methods.discovery.badge")}</div>
              <div className="m-icon">
                <Search size={20} aria-hidden="true" />
              </div>
              <h3>{t("create.methods.discovery.title")}</h3>
              <p>{t("create.methods.discovery.desc")}</p>
              <div className="m-specs">
                <span>ListAgentRuntimes · GetAgentRuntime</span>
                <span>{t("create.methods.discovery.spec2")}</span>
                <span>{t("create.methods.discovery.spec3")}</span>
              </div>
            </button>
          </div>
          <div style={{ display: "flex", justifyContent: "flex-end" }}>
            <span title={canDeploy ? undefined : t("create.permissionRequired")}>
              <Btn primary disabled={!canDeploy} onClick={() => setStep(2)}>
                {t("create.next")} ▸
              </Btn>
            </span>
          </div>

          <div style={{ height: 18 }} />
          <AgentList
            agents={agents}
            onEdit={(a) =>
              a.method === "studio" ? navigate(`/create/studio?agent=${a.id}`) : startEdit(a)
            }
            onDetails={openDetails}
            onDelete={(a) =>
              setConfirm({
                kind: "delete",
                id: a.id,
                name: a.name,
                external: a.method === "discovered_runtime",
              })
            }
            onConvert={(id, name) => setConfirm({ kind: "convert", id, name })}
          />
        </>
      )}

      {step === 2 && (
        <div className="cfg-grid">
          <Panel
            brk
            title={t(
              method === "harness"
                ? "create.configure.title"
                : method === "container"
                  ? "create.configure.titleContainer"
                  : "create.configure.titleZip",
            )}
            sub={
              name
                ? method === "harness"
                  ? `harnessName: ${name.replace(/-/g, "_")}`
                  : `runtime: ${name.replace(/-/g, "_")}_*`
                : undefined
            }
            style={{ "--i": 0 } as CSSProperties}
          >
            {editing && (
              <div className="note" style={{ borderColor: "var(--amber)", marginBottom: 12 }}>
                <span className="i" style={{ color: "var(--amber)" }}>
                  [⟳]
                </span>
                <span>{t("create.editing", { name: editing.name })}</span>
              </div>
            )}
            <div className="field">
              <label htmlFor="agent-name">{t("create.configure.name")}</label>
              <input
                id="agent-name"
                className="input"
                value={name}
                disabled={!!editing}
                onChange={(e) => setName(e.target.value)}
                placeholder="hr-assistant-v3"
              />
            </div>
            {/* The container method is the "Other Agent SDK" entrance: it picks an
                SDK here instead of a model source. The two blocks are one choice
                seen from either side — the Claude Agent SDK can only drive Claude
                models, so its source is pinned (MODEL_SOURCE_BY_METHOD) and the
                Model source control stays hidden. */}
            {method === "container" && (
              <div className="field">
                <label>{t("create.configure.agentSdk")}</label>
                <div className="selchips">
                  <button
                    type="button"
                    data-testid="agent-sdk-claude"
                    className={`selchip${agentSdk === "claude_agent_sdk" ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    onClick={() => setAgentSdk("claude_agent_sdk")}
                  >
                    {t("create.configure.agentSdkClaude")}{" "}
                    {agentSdk === "claude_agent_sdk" ? "✓" : ""}
                  </button>
                </div>
                <div className="note" style={{ margin: "8px 0 0" }}>
                  <span className="i">[i]</span>
                  <span>{t("create.configure.agentSdkNote")}</span>
                </div>
              </div>
            )}
            {method !== "container" && !isA2a && (
              <div className="field">
                <label>{t("create.configure.modelSource")}</label>
                <div className="selchips">
                  {(["mantle", "bedrock"] as const).map((source) => (
                    <button
                      key={source}
                      type="button"
                      data-testid={`model-source-${source}`}
                      className={`selchip${modelSource === source ? " on" : ""}`}
                      style={{ cursor: "pointer" }}
                      onClick={() => applyModelSource(source)}
                    >
                      {t(
                        source === "mantle"
                          ? "create.configure.modelSourceMantle"
                          : "create.configure.modelSourceBedrock",
                      )}{" "}
                      {modelSource === source ? "✓" : ""}
                    </button>
                  ))}
                </div>
                <div className="note" style={{ margin: "8px 0 0" }}>
                  <span className="i">[i]</span>
                  <span>
                    {t(
                      modelSource === "mantle"
                        ? "create.configure.modelSourceMantleDesc"
                        : "create.configure.modelSourceBedrockDesc",
                    )}
                  </span>
                </div>
              </div>
            )}
            <div className="field">
              <label htmlFor="agent-model-select">{t("create.configure.model")}</label>
              <select
                id="agent-model-select"
                className="input"
                data-testid="model-select"
                value={customModel ? CUSTOM_MODEL_OPTION : modelId}
                onChange={(e) => {
                  const picked = e.target.value;
                  if (picked === CUSTOM_MODEL_OPTION) {
                    setCustomModel(true);
                    return;
                  }
                  setCustomModel(false);
                  setModelId(picked);
                }}
              >
                {modelOptionsFor(modelSource, method === "container").map((option) => (
                  <option
                    key={option.model_id}
                    value={option.model_id}
                    style={{ background: "#141816" }}
                  >
                    {option.label} · {option.model_id}
                  </option>
                ))}
                <option value={CUSTOM_MODEL_OPTION} style={{ background: "#141816" }}>
                  {t("create.configure.modelCustom")}
                </option>
              </select>
              {customModel && (
                <input
                  id="agent-model"
                  className="input mono"
                  style={{ marginTop: 8 }}
                  value={modelId}
                  onChange={(e) => setModelId(e.target.value)}
                  placeholder={t("create.configure.modelCustomPlaceholder")}
                />
              )}
            </div>
            <div className="field">
              <label htmlFor="agent-prompt">{t("create.configure.systemPrompt")}</label>
              <textarea
                id="agent-prompt"
                className="input mono"
                style={{ minHeight: 88, resize: "vertical" }}
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
                placeholder={t("create.configure.systemPromptPlaceholder")}
              />
            </div>
            <div className="field">
              <label>
                {method === "harness"
                  ? t("create.configure.tools")
                  : method === "container"
                    ? t("create.configure.sdkTools")
                    : t("create.configure.templateTools")}
              </label>
              <div className="selchips">
                {method === "harness" ? (
                  <>
                    {BUILTIN_TOOLS.map((tool) => (
                      <button
                        key={tool}
                        type="button"
                        className={`selchip${tools.includes(tool) ? " on" : ""}`}
                        style={{ cursor: "pointer" }}
                        onClick={() => toggleTool(tool)}
                      >
                        {tool} · builtin {tools.includes(tool) ? "✓" : "+"}
                      </button>
                    ))}
                    {gatewayChips()}
                    {remoteMcp.map((server) => (
                      <button
                        key={server.name}
                        type="button"
                        className={`selchip${selectedMcp.includes(server.name) ? " on" : ""}`}
                        style={{ cursor: "pointer" }}
                        title={server.url}
                        onClick={() =>
                          setSelectedMcp((prev) =>
                            prev.includes(server.name)
                              ? prev.filter((x) => x !== server.name)
                              : [...prev, server.name],
                          )
                        }
                      >
                        {server.name} · mcp {selectedMcp.includes(server.name) ? "✓" : "+"}
                      </button>
                    ))}
                  </>
                ) : method === "container" ? (
                  <>
                    <span className="selchip on">Task · subagents ✓</span>
                    {remoteMcp.map((server) => (
                      <button
                        key={server.name}
                        type="button"
                        className={`selchip${selectedMcp.includes(server.name) ? " on" : ""}`}
                        style={{ cursor: "pointer" }}
                        title={server.url}
                        onClick={() =>
                          setSelectedMcp((prev) =>
                            prev.includes(server.name)
                              ? prev.filter((x) => x !== server.name)
                              : [...prev, server.name],
                          )
                        }
                      >
                        {server.name} · mcp {selectedMcp.includes(server.name) ? "✓" : "+"}
                      </button>
                    ))}
                  </>
                ) : toolkitToolNames.length ? (
                  // A toolkit replaces the template's own two tools, so the chips
                  // show the tool surface the deployed agent will actually have.
                  toolkitToolNames.map((name) => (
                    <span key={name} className="selchip on">
                      {name} · toolkit ✓
                    </span>
                  ))
                ) : (
                  <>
                    <span className="selchip on">calculator · template ✓</span>
                    <span className="selchip on">current_utc_time · template ✓</span>
                  </>
                )}
                {/* An HTTP zip runtime reaches the shared Gateway through a
                    generated MCP client; A2A and container still cannot. */}
                {method === "zip_runtime" && !isA2a && gatewayChips()}
                {(method === "container" || isA2a) && (
                  <span className="selchip" style={{ opacity: 0.5 }}>
                    {t("create.configure.gatewayToolsSoon")}
                  </span>
                )}
              </div>
              {method === "zip_runtime" && !isA2a && (
                <>
                  <label style={{ marginTop: 12 }}>{t("create.configure.toolkits")}</label>
                  <div className="selchips">
                    {TOOLKITS.map((kit) => (
                      <button
                        key={kit.name}
                        type="button"
                        data-testid={`toolkit-${kit.name}`}
                        className={`selchip${toolkits.includes(kit.name) ? " on" : ""}`}
                        style={{ cursor: "pointer" }}
                        title={t(`create.configure.toolkitDesc.${kit.name}`)}
                        onClick={() => toggleToolkit(kit)}
                      >
                        {t(`create.configure.toolkitName.${kit.name}`)} · toolkit{" "}
                        {toolkits.includes(kit.name) ? "✓" : "+"}
                      </button>
                    ))}
                  </div>
                  <div className="note" style={{ marginTop: 8 }}>
                    <span className="i">[i]</span>
                    <span>{t("create.configure.toolkitNote")}</span>
                  </div>
                </>
              )}
              {(method === "harness" || (method === "zip_runtime" && !isA2a)) &&
                gatewayTargets.length > 0 && (
                  <div className="note" style={{ marginTop: 8 }}>
                    <span className="i">[i]</span>
                    <span>{t("create.configure.gatewayWholeNote")}</span>
                  </div>
                )}
            </div>
            {method === "zip_runtime" && (
              <div className="field">
                <label>{t("create.configure.protocol")}</label>
                <div className="selchips">
                  <button
                    type="button"
                    data-testid="protocol-http"
                    className={`selchip${protocol === "http" ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    onClick={() => {
                      setProtocol("http");
                      // leaving the A2A pin re-offers the method default
                      if (isA2a) applyModelSource(sourceForMethod(method));
                    }}
                  >
                    {t("create.configure.protocolHttp")} {protocol === "http" ? "✓" : ""}
                  </button>
                  <button
                    type="button"
                    data-testid="protocol-a2a"
                    className={`selchip${protocol === "a2a" ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    onClick={() => {
                      setProtocol("a2a");
                      setA2aSkills((prev) => (prev.length ? prev : A2A_SKILL_SEEDS));
                      // The A2A template has no Mantle branch — see A2A_MODEL_SOURCE.
                      if (modelSource !== A2A_MODEL_SOURCE) applyModelSource(A2A_MODEL_SOURCE);
                    }}
                  >
                    {t("create.configure.protocolA2a")} {protocol === "a2a" ? "✓" : ""}
                  </button>
                </div>
                {protocol === "a2a" && (
                  <>
                    <div className="note" style={{ margin: "8px 0" }}>
                      <span className="i">[i]</span>
                      <span>{t("create.configure.a2aNote")}</span>
                    </div>
                    <label style={{ marginTop: 4 }}>{t("create.configure.a2aSkills")}</label>
                    {a2aSkills.map((row, i) => (
                      <div
                        key={i}
                        style={{ display: "grid", gap: 6, marginBottom: 6,
                                 gridTemplateColumns: "1fr 2fr 1fr auto" }}
                      >
                        <input
                          className="input"
                          data-testid={`a2a-skill-name-${i}`}
                          placeholder={t("create.configure.a2aSkillName")}
                          value={row.name}
                          onChange={(e) =>
                            setA2aSkills((p) =>
                              p.map((r, j) => (j === i ? { ...r, name: e.target.value } : r)))
                          }
                        />
                        <input
                          className="input"
                          placeholder={t("create.configure.a2aSkillDesc")}
                          value={row.description}
                          onChange={(e) =>
                            setA2aSkills((p) =>
                              p.map((r, j) =>
                                j === i ? { ...r, description: e.target.value } : r))
                          }
                        />
                        <input
                          className="input"
                          placeholder={t("create.configure.a2aSkillTags")}
                          value={row.tags}
                          onChange={(e) =>
                            setA2aSkills((p) =>
                              p.map((r, j) => (j === i ? { ...r, tags: e.target.value } : r)))
                          }
                        />
                        <Btn onClick={() => setA2aSkills((p) => p.filter((_, j) => j !== i))}>
                          ✕
                        </Btn>
                      </div>
                    ))}
                    <Btn
                      data-testid="a2a-skill-add"
                      onClick={() =>
                        setA2aSkills((p) => [...p, { name: "", description: "", tags: "" }])
                      }
                    >
                      + {t("create.configure.a2aSkillAdd")}
                    </Btn>
                  </>
                )}
              </div>
            )}
            {method === "container" && (
              <div className="field">
                <label htmlFor="agent-mcp">{t("create.configure.mcpServers")}</label>
                <textarea
                  id="agent-mcp"
                  className="input mono"
                  style={{ minHeight: 56, resize: "vertical" }}
                  value={mcpServers}
                  onChange={(e) => setMcpServers(e.target.value)}
                  placeholder='{"docs": {"command": "uvx", "args": ["mcp-server-docs"]}}'
                />
              </div>
            )}
            {(method === "harness" || method === "container" || method === "zip_runtime") && (
              <div className="field">
                <label>{t("create.configure.skills")}</label>
                <div className="selchips">
                  {skillCatalog.map((skill) => (
                    <button
                      key={skill.path}
                      type="button"
                      className={`selchip${skills.includes(skill.path) ? " on" : ""}`}
                      style={{ cursor: "pointer" }}
                      title={skill.description || skill.path}
                      onClick={() =>
                        setSkills((prev) =>
                          prev.includes(skill.path)
                            ? prev.filter((s) => s !== skill.path)
                            : [...prev, skill.path],
                        )
                      }
                    >
                      {skill.name} · skill {skills.includes(skill.path) ? "✓" : "+"}
                    </button>
                  ))}
                  {skills
                    .filter((path) => !skillCatalog.some((s) => s.path === path))
                    .map((path) => {
                      const custom = customSkills.find((c) => c.path === path);
                      return (
                        <button
                          key={path}
                          type="button"
                          className="selchip on"
                          style={{ cursor: "pointer" }}
                          title={path}
                          onClick={() => {
                            setSkills((prev) => prev.filter((s) => s !== path));
                            setCustomSkills((prev) => prev.filter((c) => c.path !== path));
                          }}
                        >
                          {custom
                            ? `${custom.name} · custom ✕`
                            : `${skillNameFromPath(path)} · registry ✕`}
                        </button>
                      );
                    })}
                  <button
                    type="button"
                    className="selchip"
                    style={{ cursor: "pointer" }}
                    disabled={srcBusy}
                    onClick={() => skillFileRef.current?.click()}
                  >
                    ⬆ {t("create.configure.skillsUploadZip")}
                  </button>
                  <button
                    type="button"
                    className={`selchip${gitOpen ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    disabled={srcBusy}
                    onClick={() => setGitOpen((v) => !v)}
                  >
                    ⇣ {t("create.configure.skillsFromGit")}
                  </button>
                  <input
                    ref={skillFileRef}
                    type="file"
                    accept=".zip"
                    style={{ display: "none" }}
                    onChange={(e) => {
                      const file = e.target.files?.[0];
                      e.target.value = "";
                      if (file) void inspectSource(file);
                    }}
                  />
                </div>
                {gitOpen && (
                  <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                    <input
                      className="input mono"
                      style={{ flex: 1 }}
                      value={gitUrl}
                      onChange={(e) => setGitUrl(e.target.value)}
                      placeholder="https://github.com/org/repo[/subdir][@ref]"
                    />
                    <Btn
                      disabled={srcBusy || !gitUrl.trim().startsWith("https://")}
                      onClick={() => void inspectSource({ url: gitUrl.trim() })}
                    >
                      {srcBusy ? "…" : t("create.configure.skillsGitFetch")}
                    </Btn>
                  </div>
                )}
                {pendingSkills && (
                  <div style={{ marginTop: 8 }}>
                    <label>{t("create.configure.skillsPending")}</label>
                    <div className="selchips">
                      {pendingSkills.skills.map((s) => (
                        <button
                          key={s.index}
                          type="button"
                          className={`selchip${pendingSkills.picked.includes(s.index) ? " on" : ""}`}
                          style={{ cursor: s.valid ? "pointer" : "not-allowed", opacity: s.valid ? 1 : 0.4 }}
                          title={s.valid ? s.description : s.errors.join("; ")}
                          disabled={!s.valid}
                          onClick={() =>
                            setPendingSkills((prev) =>
                              prev && {
                                ...prev,
                                picked: prev.picked.includes(s.index)
                                  ? prev.picked.filter((i) => i !== s.index)
                                  : [...prev.picked, s.index],
                              },
                            )
                          }
                        >
                          {s.name} {pendingSkills.picked.includes(s.index) ? "✓" : "+"}
                        </button>
                      ))}
                      <Btn
                        disabled={srcBusy || pendingSkills.picked.length === 0}
                        onClick={() => void attachPicked()}
                      >
                        {t("create.configure.skillsAttach", { n: pendingSkills.picked.length })}
                      </Btn>
                      <Btn onClick={() => setPendingSkills(null)}>✕</Btn>
                    </div>
                  </div>
                )}
              </div>
            )}
            <div className="field" data-testid="kb-picker">
              <label>{t("create.configure.kbLabel")}</label>
              <div className="selchips">
                {activeKbs.map((kb) => (
                  <button
                    key={kb.kb_id}
                    type="button"
                    className={`selchip${selectedKbs.includes(kb.kb_id) ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    title={kb.description || kb.name}
                    onClick={() => toggleKb(kb.kb_id)}
                  >
                    {kb.name} · kb {selectedKbs.includes(kb.kb_id) ? "✓" : "+"}
                  </button>
                ))}
                {selectedKbs
                  .filter((id) => !activeKbs.some((k) => k.kb_id === id))
                  .map((id) => {
                    const info = kbInfo(id);
                    return (
                      <button
                        key={id}
                        type="button"
                        className="selchip on"
                        style={{ cursor: "pointer" }}
                        title={info.description || info.name}
                        onClick={() => toggleKb(id)}
                      >
                        {info.name} · kb ✓
                      </button>
                    );
                  })}
                {activeKbs.length === 0 && selectedKbs.length === 0 && (
                  <span className="selchip" style={{ opacity: 0.5 }}>
                    {t("create.configure.kbEmpty")}
                  </span>
                )}
              </div>
              <div className="note" style={{ marginTop: 8 }}>
                <span className="i">[i]</span>
                <span>
                  {method === "harness"
                    ? t("create.configure.kbNote")
                    : t("create.configure.kbNoteDirect")}
                </span>
              </div>
            </div>
            {method === "container" && (
              <div className="field" data-testid="fs-config">
                <label>{t("create.configure.filesystem")}</label>
                <div className="selchips">
                  <button
                    type="button"
                    className={`selchip${sessionFs ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    onClick={() => setSessionFs((v) => !v)}
                  >
                    {t("create.configure.fsSession")} {sessionFs ? "✓" : "+"}
                  </button>
                  <button
                    type="button"
                    className="selchip"
                    style={{ cursor: "pointer", opacity: s3Mounts.length >= 2 ? 0.4 : 1 }}
                    disabled={s3Mounts.length >= 2}
                    onClick={() => setS3Mounts((prev) => [...prev, { arn: "", path: "" }])}
                  >
                    {t("create.configure.fsAddS3")}
                  </button>
                  <button
                    type="button"
                    className="selchip"
                    style={{ cursor: "pointer", opacity: efsMounts.length >= 2 ? 0.4 : 1 }}
                    disabled={efsMounts.length >= 2}
                    onClick={() => setEfsMounts((prev) => [...prev, { arn: "", path: "" }])}
                  >
                    {t("create.configure.fsAddEfs")}
                  </button>
                </div>
                {sessionFs && (
                  <input
                    className="input mono"
                    style={{ marginTop: 8 }}
                    value={sessionMount}
                    onChange={(e) => setSessionMount(e.target.value)}
                    placeholder={DEFAULT_SESSION_MOUNT}
                    aria-label={t("create.configure.fsSessionMount")}
                  />
                )}
                {[
                  { kind: "s3" as const, rows: s3Mounts, set: setS3Mounts },
                  { kind: "efs" as const, rows: efsMounts, set: setEfsMounts },
                ].map(({ kind, rows, set }) =>
                  rows.map((row, i) => (
                    <div key={`${kind}-${i}`} style={{ display: "flex", gap: 8, marginTop: 8 }}>
                      <span className="selchip on" style={{ alignSelf: "center" }}>
                        {kind === "s3" ? "S3 FILES" : "EFS"}
                      </span>
                      <input
                        className="input mono"
                        style={{ flex: 2 }}
                        value={row.arn}
                        onChange={(e) =>
                          set((prev) =>
                            prev.map((r, j) => (j === i ? { ...r, arn: e.target.value } : r)),
                          )
                        }
                        placeholder={t(
                          kind === "s3"
                            ? "create.configure.fsS3ArnPlaceholder"
                            : "create.configure.fsEfsArnPlaceholder",
                        )}
                      />
                      <input
                        className="input mono"
                        style={{ flex: 1 }}
                        value={row.path}
                        onChange={(e) =>
                          set((prev) =>
                            prev.map((r, j) => (j === i ? { ...r, path: e.target.value } : r)),
                          )
                        }
                        placeholder="/mnt/data"
                      />
                      <Btn onClick={() => set((prev) => prev.filter((_, j) => j !== i))}>✕</Btn>
                    </div>
                  )),
                )}
                {byoMounts && (
                  <div style={{ marginTop: 8 }}>
                    <label>{t("create.configure.fsVpc")}</label>
                    <div style={{ display: "flex", gap: 8 }}>
                      <input
                        className="input mono"
                        style={{ flex: 1 }}
                        value={vpcSubnets}
                        onChange={(e) => setVpcSubnets(e.target.value)}
                        placeholder="subnet-0abc, subnet-0def"
                        aria-label={t("create.configure.fsSubnets")}
                      />
                      <input
                        className="input mono"
                        style={{ flex: 1 }}
                        value={vpcSgs}
                        onChange={(e) => setVpcSgs(e.target.value)}
                        placeholder="sg-0abc"
                        aria-label={t("create.configure.fsSgs")}
                      />
                    </div>
                  </div>
                )}
                <div className="note" style={{ marginTop: 8 }}>
                  <span className="i">[i]</span>
                  <span>
                    {byoMounts ? t("create.configure.fsNoteByo") : t("create.configure.fsNote")}
                  </span>
                </div>
              </div>
            )}
            <div className="field">
              <label>{t("create.configure.memory")}</label>
              <div className="selchips">
                <span className="selchip on">{t("create.configure.memoryShort")} ✓</span>
                <button
                  type="button"
                  className={`selchip${longTerm ? " on" : ""}`}
                  style={{ cursor: "pointer" }}
                  onClick={() => setLongTerm((v) => !v)}
                >
                  {t("create.configure.memoryLong")} {longTerm ? "✓" : "+"}
                </button>
              </div>
            </div>
            <div className="note">
              <span className="i">[i]</span>
              <span>{t("create.configure.note")}</span>
            </div>
          </Panel>

          <div>
            <Panel
              title={t(editing ? "create.republishPanel.title" : "create.launchPanel.title")}
              sub={t(editing ? "create.republishPanel.sub" : "create.launchPanel.sub")}
            >
              <div className="kv">
                <span className="k">{t("create.launchPanel.sharedInfra")}</span>
                <span className="v">CDK · launchpad-base ✓</span>
              </div>
              <div className="kv">
                <span className="k">{t("create.launchPanel.agentResources")}</span>
                <span className="v">{t("create.launchPanel.agentResourcesV")}</span>
              </div>
              <div className="kv">
                <span className="k">
                  {t(editing ? "create.republishPanel.effect" : "create.launchPanel.onSuccess")}
                </span>
                <span className="v">
                  {t(editing ? "create.republishPanel.effectV" : "create.launchPanel.onSuccessV")}
                </span>
              </div>
            </Panel>
            <div style={{ height: 14 }} />
            {submitError && (
              <div className="note" style={{ borderColor: "var(--crit)", marginBottom: 14 }}>
                <span className="i" style={{ color: "var(--crit)" }}>
                  [✕]
                </span>
                <span>{submitError}</span>
              </div>
            )}
            <Panel>
              <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
                <Btn
                  onClick={() => {
                    setStep(1);
                    resetForm();
                  }}
                >
                  ◂ {t("create.back")}
                </Btn>
                <Btn
                  primary
                  disabled={!configValid || !canDeploy}
                  onClick={() => (editing ? setConfirm({ kind: "republish" }) : void submit())}
                >
                  {editing ? `⟳ ${t("create.republish")}` : `▲ ${t("create.launch")}`}
                </Btn>
              </div>
            </Panel>
          </div>
        </div>
      )}

      {step === 3 && (
        <>
          <LaunchSequence
            deployment={deployment}
            job={job}
            agentStatus={agentStatus}
            detailsMode={detailsMode}
            onRestart={() => {
              setStep(1);
              setLaunch(null);
              setDeployment(null);
              setJob(null);
              resetForm();
              reloadAgents();
            }}
          />
          {detailsMode && detailConversion && (
            <>
              <div style={{ height: 14 }} />
              <Panel title={t("create.list.convertedTitle")} data-testid="conversion-panel">
                <div className="mono dim" style={{ fontSize: 11, marginBottom: 6 }}>
                  ⇄ {t("create.list.convertedFrom", { name: detailConversion.source })}
                </div>
                {Object.entries(detailConversion.notes).map(([cap, note]) => (
                  <div className="kv" key={cap}>
                    <span className="k mono">{cap}</span>
                    <span className="v mono" style={{ fontSize: 10.5 }}>{note}</span>
                  </div>
                ))}
              </Panel>
            </>
          )}
          {detailKbs.length > 0 && (
            <>
              <div style={{ height: 14 }} />
              <Panel title={t("create.configure.kbMountedTitle")}>
                <div className="selchips">
                  {detailKbs.map((kb) => (
                    <span key={kb.kb_id} className="selchip on" title={kb.description || kb.name}>
                      {kb.name} · kb
                    </span>
                  ))}
                </div>
              </Panel>
            </>
          )}
        </>
      )}

      <ConfirmDialog
        open={confirm?.kind === "republish"}
        title={t("create.republishConfirm.title")}
        body={t("create.republishConfirm.body", { name })}
        confirmLabel={t("create.republish")}
        onConfirm={() => {
          setConfirm(null);
          void submit();
        }}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm?.kind === "convert"}
        title={t("create.list.convertConfirmTitle")}
        body={t("create.list.convertConfirm", {
          name: confirm?.kind === "convert" ? confirm.name : "",
        })}
        confirmLabel={t("create.list.convert")}
        onConfirm={() => {
          if (confirm?.kind === "convert") void doConvert(confirm.id);
          setConfirm(null);
        }}
        onCancel={() => setConfirm(null)}
      />
      <ConfirmDialog
        open={confirm?.kind === "delete"}
        title={t(
          confirm?.kind === "delete" && confirm.external
            ? "create.list.confirmDetachTitle"
            : "create.list.confirmDeleteTitle",
        )}
        body={t(
          confirm?.kind === "delete" && confirm.external
            ? "create.list.confirmDetach"
            : "create.list.confirmDelete",
          {
            name: confirm?.kind === "delete" ? confirm.name : "",
          },
        )}
        confirmLabel={t(
          confirm?.kind === "delete" && confirm.external
            ? "create.list.remove"
            : "create.list.delete",
        )}
        onConfirm={() => {
          if (confirm?.kind === "delete") void doDelete(confirm.id);
          setConfirm(null);
        }}
        onCancel={() => setConfirm(null)}
      />
    </section>
  );
}

const STATUS_TONE: Record<string, "good" | "warn" | "crit" | "muted"> = {
  active: "good",
  deploying: "warn",
  failed: "crit",
};

function AgentList({
  agents,
  onEdit,
  onDetails,
  onDelete,
  onConvert,
}: {
  agents: AgentInfo[];
  onEdit: (a: AgentInfo) => void;
  onDetails: (a: AgentInfo) => void;
  onDelete: (a: AgentInfo) => void;
  onConvert: (id: string, name: string) => void;
}) {
  const { t } = useTranslation();
  const { can } = useAuth();
  const permHint = (allowed: boolean) =>
    allowed ? undefined : t("create.permissionRequired");
  const canEdit = can("agents.deploy"); // editing re-publishes
  const canDelete = can("agents.delete");
  const canConvert = can("agents.convert");

  const [methodFilter, setMethodFilter] = useState("all");
  const [statusFilter, setStatusFilter] = useState("all");
  const [query, setQuery] = useState("");
  const [page, setPage] = useState(1);
  const [pageSize, setPageSize] = useState(DEFAULT_PAGE_SIZE);
  useEffect(() => {
    setPage(1); // filters change the result set — restart from page 1
  }, [methodFilter, statusFilter, query]);

  const methods = useMemo(() => [...new Set(agents.map((a) => a.method))].sort(), [agents]);
  const statuses = useMemo(() => [...new Set(agents.map((a) => a.status))].sort(), [agents]);
  const rows = agents.filter((a) => {
    if (methodFilter !== "all" && a.method !== methodFilter) return false;
    if (statusFilter !== "all" && a.status !== statusFilter) return false;
    const q = query.trim().toLowerCase();
    return !q || a.name.toLowerCase().includes(q) || a.id.toLowerCase().includes(q);
  });
  const currentPage = Math.min(page, Math.max(1, Math.ceil(rows.length / pageSize)));
  const pageRows = rows.slice((currentPage - 1) * pageSize, currentPage * pageSize);

  return (
    <Panel title={t("create.list.title")} sub={t("create.list.sub")} pad={false}>
      <div className="filters">
        <select
          className="fsel"
          value={methodFilter}
          onChange={(e) => setMethodFilter(e.target.value)}
          aria-label={t("create.list.colMethod")}
        >
          <option value="all">{t("create.list.filterMethodAll")}</option>
          {methods.map((method) => (
            <option key={method} value={method}>
              {methodLabel(method)}
            </option>
          ))}
        </select>
        <select
          className="fsel"
          value={statusFilter}
          onChange={(e) => setStatusFilter(e.target.value)}
          aria-label={t("create.list.colStatus")}
        >
          <option value="all">{t("create.list.filterStatusAll")}</option>
          {statuses.map((status) => (
            <option key={status} value={status}>
              {t(`status.${status}`, status.toUpperCase())}
            </option>
          ))}
        </select>
        <input
          className="fsearch"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder={t("create.list.searchPlaceholder")}
        />
      </div>
      <table>
        <thead>
          <tr>
            <th>{t("create.list.colName")}</th>
            <th>{t("create.list.colMethod")}</th>
            <th>{t("create.list.colStatus")}</th>
            <th>{t("create.list.colRev")}</th>
            <th>{t("create.list.colUpdated")}</th>
            <th style={{ textAlign: "right" }}>{t("create.list.colActions")}</th>
          </tr>
        </thead>
        <tbody>
          {pageRows.map((a) => (
            <tr key={a.id}>
              <td className="pri">{a.name}</td>
              <td>
                <div className="agent-method-cell">
                  <MethodChip method={a.method} />
                  {a.method === "discovered_runtime" && (
                    <span className="mono dim">
                      {String(a.spec.protocol ?? "unknown").toUpperCase()}
                    </span>
                  )}
                </div>
              </td>
              <td>
                <Chip
                  tone={STATUS_TONE[a.status] ?? "muted"}
                  icon={a.status === "active" ? "●" : a.status === "failed" ? "✕" : "◐"}
                >
                  {t(`status.${a.status}`, a.status.toUpperCase())}
                </Chip>
              </td>
              <td className="mono">
                {a.method === "discovered_runtime" ? `v${a.version ?? "—"}` : (a.revision ?? "—")}
              </td>
              <td className="mono dim">{(a.updated_at ?? "").replace("T", " ").slice(0, 16)}</td>
              <td>
                <div style={{ display: "flex", gap: 6, justifyContent: "flex-end", flexWrap: "wrap" }}>
                  {a.method !== "discovered_runtime" && (
                    <button
                      type="button"
                      className="rowact"
                      disabled={!canEdit || a.status === "deploying"}
                      style={
                        !canEdit || a.status === "deploying" ? { opacity: 0.35 } : undefined
                      }
                      title={permHint(canEdit)}
                      onClick={() => onEdit(a)}
                    >
                      {t("create.list.edit")}
                    </button>
                  )}
                  {a.invoke_capability.eligible && (
                    <Link className="rowact" to={`/chat?agent=${a.id}`}>
                      {t("create.list.chat")}
                    </Link>
                  )}
                  {a.method === "harness" && a.status === "active" && (
                    <button
                      type="button"
                      className="rowact"
                      data-testid={`convert-${a.name}`}
                      disabled={!canConvert}
                      style={!canConvert ? { opacity: 0.35 } : undefined}
                      title={permHint(canConvert)}
                      onClick={() => onConvert(a.id, a.name)}
                    >
                      {t("create.list.convert")}
                    </button>
                  )}
                  {a.deployment && (
                    <button type="button" className="rowact" onClick={() => onDetails(a)}>
                      {t("create.list.details")}
                    </button>
                  )}
                  <button
                    type="button"
                    className="rowact"
                    disabled={!canDelete}
                    style={!canDelete ? { opacity: 0.35 } : undefined}
                    title={permHint(canDelete)}
                    onClick={() => onDelete(a)}
                  >
                    {t(
                      a.method === "discovered_runtime"
                        ? "create.list.remove"
                        : "create.list.delete",
                    )}
                  </button>
                </div>
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr>
              <td colSpan={6} className="dim mono" style={{ textAlign: "center" }}>
                {t(agents.length ? "create.list.noMatch" : "create.list.empty")}
              </td>
            </tr>
          )}
        </tbody>
      </table>
      <Pager
        total={rows.length}
        page={currentPage}
        size={pageSize}
        onPage={setPage}
        onSize={(size) => {
          setPageSize(size);
          setPage(1);
        }}
      />
    </Panel>
  );
}
