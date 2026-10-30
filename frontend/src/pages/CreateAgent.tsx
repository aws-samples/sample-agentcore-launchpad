import type { CSSProperties } from "react";
import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useParams, useSearchParams } from "react-router-dom";
import { ArrowLeft, Download, RefreshCw, Search } from "lucide-react";

import { useAuth } from "../auth/auth-context";
import {
  Btn,
  Chip,
  ConfirmDialog,
  DEFAULT_PAGE_SIZE,
  LaunchSequence,
  Pager,
  Panel,
  useToast,
  ViewHead,
} from "../components";
import type {
  AgentInfo,
  AgentSpecInput,
  ByocArtifactKind,
  ByocConfigInput,
  ConnectionInfo,
  ByocPythonVersion,
  ByocUploadInfo,
  DeploymentInfo,
  HarnessDiscoveryCandidate,
  HarnessNativeTool,
  InspectedSkill,
  JobInfo,
  MemoryResourceRow,
  RuntimeDiscoveryCandidate,
  RuntimeImportResult,
  SystemPresetInstallInput,
  SystemPresetSettings,
  SystemPresetStatus,
  Toolkit,
} from "../lib/api";
import { api, ApiError, HARNESS_NATIVE_TOOLS } from "../lib/api";
import { DEFAULT_HARNESS_NATIVE_TOOLS, DEFAULT_TIMEOUT_SECONDS } from "../lib/agent-defaults";
import {
  A2A_MODEL_SOURCE,
  A2A_SKILL_SEEDS,
  agentFormValid,
  authRowFromStored,
  authToolsCarried,
  BUILTIN_TOOLS,
  buildAgentSpec,
  BYOC_MODELS_MAX,
  DEFAULT_SESSION_MOUNT,
  defaultModelForMethod,
  entrypointAfterUpload,
  isAuthTool,
  promptWithToolkit,
  republishSpec,
  resolveKb,
  selectableKbs,
  skillNameFromPath,
  sourceForMethod,
  sourceOnMethodSwitch,
  TOOLKITS,
  toolkitToolNames as toolkitTools,
} from "../lib/agent-spec";
import type {
  AgentForm,
  AgentFormCatalogs,
  AgentMethod,
  A2aSkillRow,
  AttachableKb,
  AttachableAgent,
  AttachableMcp,
  AttachableSkill,
  AuthToolRow,
  KbRef,
  StoredTool,
} from "../lib/agent-spec";
import ClassicIdentitySection from "../components/ClassicIdentitySection";
import type { ModelSource, ReasoningEffort } from "../lib/models";
import { useWorkspace } from "../workspace/workspace-context";
import { SystemPresetsPanel } from "./create/SystemPresetsPanel";
import { entryShown } from "../v2/hiddenSurfaces";
import {
  apiErrorRows,
  DEFAULT_MAX_ITERATIONS,
  diffPresetSettings,
  EFFORT_NONE,
  formFromSettings,
  isPresetDefault,
  knobProblems,
  presetConfigureRequest,
} from "./create/presetSettings";
import type { EffortChoice, PresetConfigureRequest, PresetForm } from "./create/presetSettings";
import {
  CUSTOM_MODEL_OPTION,
  DEFAULT_MODEL_SOURCE,
  defaultModelFor,
  isCustomModelId,
  modelOptionLabel,
  modelOptionsFor,
  modelUnavailableReason,
  providerBadgeKey,
  REASONING_EFFORTS,
  SPEC_DEFAULT_MODEL_ID,
  supportsReasoningEffort,
} from "../lib/models";


type Step = 1 | 2 | 3;

interface LaunchState {
  agentId: string;
  jobId: string;
  /** set for a system-preset re-publish: the poll follows the workspace the save
   * was pinned to, never the shared selection another tab may have moved */
  workspaceId?: string | null;
}

type Method = AgentMethod;

/**
 * A system-managed preset opened in the shared editor (Create → SYSTEM PRESETS →
 * CONFIGURE, or Existing agents → EDIT as an administrator). Saving never goes
 * through `redeploy` (403 for a preset): it posts a PARTIAL edit of the
 * administrator-editable settings to the maintenance route, pinned to the workspace
 * the row was read from. Everything else on the preset is catalogue-owned and shown
 * read-only. `editable: false` is the member's review of the same page: no save.
 */
interface SystemEditContext {
  key: string;
  label: string;
  workspaceId: string | null;
  stored: SystemPresetSettings;
  defaults: SystemPresetSettings;
  editable: boolean;
  readOnlyReason?: string;
  status: SystemPresetStatus;
  allowedTools: string[];
  /** the protected Skill's name = the server-owned preset name (display only) */
  skillName: string;
  skillVersion: string | null;
}

interface EditingTarget {
  id: string;
  name: string;
  method: Method;
  system?: SystemEditContext;
  /** the stored spec, so a re-publish carries the fields the form does not own */
  spec?: unknown;
}


// Spec fields we read back when loading an existing agent into the wizard.
interface StoredSpec {
  /** D20 §4.2 — carried through a classic re-publish, edited in the V2 wizard */
  policy_ids?: string[];
  model_id?: string;
  model_source?: ModelSource;
  // harness-only inference knobs (absent on every spec written before they existed)
  max_tokens?: number | null;
  reasoning_effort?: ReasoningEffort | null;
  // agent-loop bounds (backend defaults 10 / 180 when absent)
  max_iterations?: number;
  timeout_seconds?: number;
  system_prompt?: string;
  tools?: StoredTool[];
  toolkits?: Toolkit[];
  skills?: string[];
  allowed_tools?: string[] | null;
  native_tools?: HarnessNativeTool[];
  knowledge_bases?: KbRef[];
  memory?: { long_term?: boolean; memory_id?: string | null };
  protocol?: "http" | "a2a";
  a2a_skills?: { id?: string; name?: string; description?: string; tags?: string[] }[];
  env?: Record<string, string>;
  filesystem?: {
    session_storage?: { mount_path?: string } | null;
  };
  byoc?: ByocConfigInput;
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

/**
 * The agent flows that have no native V2 page yet (the list, detail and
 * re-publish wizard live in `v2/pages/Agents.tsx`):
 *   new    `/agents/new`           — the 3-step wizard with the SYSTEM PRESETS panel
 *   import `/agents/import`        — discovery of existing Runtime/Harness resources
 *   edit   `/agents/:agentId/edit` — a system preset's configure flow (App.tsx sends
 *                                    every other agent to the V2 wizard / Studio)
 * The wizard keeps its state machine; the mode only decides what step 1 shows
 * and where "back"/"done" go.
 */
export type AgentsMode = "new" | "import" | "edit";

export function CreateAgent({ mode }: { mode: AgentsMode }) {
  const { agentId } = useParams();
  // Members reach the whole module: the list, details and the discovery scan
  // are reads. Each mutating action gates itself on the caller's granted
  // agent-management permissions (default granted, revocable per user in the
  // Users console — mirrors route_policy's perm:agents.*).
  if (mode === "import") return <RuntimeDiscovery />;
  return <CreateAgentWizard key={`${mode}:${agentId ?? ""}`} mode={mode} agentId={agentId} />;
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
        <Btn onClick={() => navigate("/agents")}>
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
            <option value="harness">{t("create.discovery.kindManagedHarness")}</option>
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
        <Chip tone="aqua">{t("create.discovery.kindManagedHarness")}</Chip>
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
          <button type="button" className="rowact" onClick={() => navigate(`/agents/${harness.managed_agent_id}`)}>
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
          <button type="button" className="rowact" onClick={() => navigate(`/agents/${runtime.managed_agent_id}`)}>
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


function CreateAgentWizard({ mode, agentId }: { mode: AgentsMode; agentId?: string }) {
  const { t } = useTranslation();
  // /agents/new is the system-presets page: the classic method chooser is hidden by flag
  const presetsOnly = mode === "new" && !entryShown("classicMethods");
  const toast = useToast();
  const navigate = useNavigate();
  const { can, isAdmin } = useAuth();
  const canDeploy = can("agents.deploy");
  const { current: currentWorkspace } = useWorkspace();
  const workspaceId = currentWorkspace?.id ?? null;
  const [params] = useSearchParams();
  const prefillGateway = params.get("gateway");
  const prefillSkill = params.get("skill");
  const [step, setStep] = useState<Step>(prefillGateway || prefillSkill ? 2 : 1);
  const [method, setMethod] = useState<Method>("harness");
  const [skills, setSkills] = useState<string[]>(prefillSkill ? [prefillSkill] : []);
  // Preserve expert overrides until the member explicitly opts into derivation.
  const [allowedTools, setAllowedTools] = useState<string[] | null>(null);
  const [nativeTools, setNativeTools] = useState<HarnessNativeTool[]>([...DEFAULT_HARNESS_NATIVE_TOOLS]);
  const [name, setName] = useState("");
  const [modelId, setModelId] = useState(defaultModelFor(DEFAULT_MODEL_SOURCE));
  const [modelSource, setModelSource] = useState<ModelSource>(DEFAULT_MODEL_SOURCE);
  // true ⇒ the model dropdown sits on "Custom model ID…" and the free-text input shows
  const [customModel, setCustomModel] = useState(false);
  const [systemPrompt, setSystemPrompt] = useState("");
  // Inference / loop knobs (rendered for the harness method). Free text so a
  // half-typed number is validated, not clamped; "" ⇒ no per-call ceiling.
  const [maxTokens, setMaxTokens] = useState("");
  const [reasoningEffort, setReasoningEffort] = useState<EffortChoice>(EFFORT_NONE);
  const [maxIterations, setMaxIterations] = useState(String(DEFAULT_MAX_ITERATIONS));
  const [timeoutSeconds, setTimeoutSeconds] = useState(String(DEFAULT_TIMEOUT_SECONDS));
  const [tools, setTools] = useState<string[]>([]);
  // Platform toolkits (zip_runtime only) — local @tool functions the backend
  // inlines into the generated agent, replacing the template's own two tools.
  const [toolkits, setToolkits] = useState<Toolkit[]>([]);
  const [gatewayTargets, setGatewayTargets] = useState<AttachableMcp[]>([]);
  const [remoteMcp, setRemoteMcp] = useState<AttachableMcp[]>([]);
  // N10v2-bug5: APPROVED A2A records (other Launchpad agents) mountable as ask_<agent> tools
  const [agentCatalog, setAgentCatalog] = useState<AttachableAgent[]>([]);
  const [selectedAgents, setSelectedAgents] = useState<string[]>([]);
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
  // KBs shown read-only on the step-3 view of the just-published agent.
  const [detailKbs, setDetailKbs] = useState<KbRef[]>([]);
  const [longTerm, setLongTerm] = useState(true);
  // per-agent AgentCore Memory pin — "" means the workspace's shared default
  const [memoryId, setMemoryId] = useState("");
  const [memoryOptions, setMemoryOptions] = useState<MemoryResourceRow[]>([]);
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
  // AgentCore Runtime managed session storage (harness / zip_runtime)
  const [sessionFs, setSessionFs] = useState(true);
  const [sessionMount, setSessionMount] = useState(DEFAULT_SESSION_MOUNT);
  // zip runtime service protocol: standard HTTP invocations vs a real A2A
  // JSON-RPC server (serverProtocol=A2A) with configurable agent-card skills
  const [protocol, setProtocol] = useState<"http" | "a2a">("http");
  const [a2aSkills, setA2aSkills] = useState<A2aSkillRow[]>([]);
  // BYOC (bring your own code): staged upload + artifact settings
  const [byocKind, setByocKind] = useState<ByocArtifactKind>("code_zip");
  const [byocUpload, setByocUpload] = useState<ByocUploadInfo | null>(null);
  const [byocUploading, setByocUploading] = useState(false);
  const [byocImageUri, setByocImageUri] = useState("");
  const [byocEntrypoint, setByocEntrypoint] = useState("main.py");
  const [byocPython, setByocPython] = useState<ByocPythonVersion>("PYTHON_3_13");
  const [byocInstallReqs, setByocInstallReqs] = useState(true);
  const [byocRawContract, setByocRawContract] = useState(false);
  // every model the execution role will permit; [0] is the PRIMARY (= spec.model_id,
  // injected as env MODEL_ID) — the whole list reaches the runtime as ALLOWED_MODEL_IDS
  const [byocModels, setByocModels] = useState<string[]>([
    defaultModelFor(DEFAULT_MODEL_SOURCE),
  ]);
  // free-text "Custom model ID…" branch of the byoc model picker
  const [byocModelCustomOpen, setByocModelCustomOpen] = useState(false);
  const [byocModelDraft, setByocModelDraft] = useState("");
  const [byocEnvRows, setByocEnvRows] = useState<{ key: string; value: string }[]>([]);
  // zip_runtime (HTTP) / byoc: rest + mcp tools behind a Connection (Identity section)
  const [authTools, setAuthTools] = useState<AuthToolRow[]>([]);
  // D20: the classic wizard has no POLICIES step; a loaded spec's `policy_ids` are carried
  // through a re-publish untouched (the V2 wizard edits them)
  const [policyIds, setPolicyIds] = useState<string[]>([]);
  // the workspace Connection catalog; null until loaded (and after a failed read),
  // which blocks a spec whose rows name a Connection — see authToolIssues
  const [connections, setConnections] = useState<ConnectionInfo[] | null>(null);
  const [connectionsError, setConnectionsError] = useState(false);
  const loadConnections = () => {
    setConnectionsError(false);
    api
      .listConnections()
      .then((d) => alive.current && setConnections(d.connections))
      .catch(() => alive.current && setConnectionsError(true));
  };
  const [byocContractOpen, setByocContractOpen] = useState(false);
  const [byocDescription, setByocDescription] = useState("");
  const byocFileRef = useRef<HTMLInputElement>(null);
  // the staged zip, kept so a Python-version change can re-run the
  // requirements pre-resolve (re-staging the same bytes under the new target)
  const byocLastFile = useRef<File | null>(null);
  // when set, the wizard edits an existing agent and the launch button re-publishes it
  const [editing, setEditing] = useState<EditingTarget | null>(null);
  // one submit at a time (both paths); the outcome of an accepted request is
  // consumed only while this component is still mounted
  const [submitting, setSubmitting] = useState(false);
  const [submitErrorRows, setSubmitErrorRows] = useState<string[]>([]);
  // system-preset edit: the pinned KB catalog read is an explicit error with RETRY,
  // never folded into an empty catalog (the stored chips stay either way)
  const [systemKbLoading, setSystemKbLoading] = useState(false);
  const [systemKbError, setSystemKbError] = useState<string | null>(null);
  const alive = useRef(true);
  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);
  // Async-race guards. `kbCatalogGen` owns `kbCatalog`: every writer (the mount-time
  // generic fetch, each pinned system-edit read) captures the generation it started
  // in and applies only if no newer writer/owner took over — a late generic response
  // that followed another tab's shared selection can never replace the catalog a
  // preset edit reads pinned to its own workspace. `editorGen` is the editor-opening
  // intent: an async open (table EDIT on a system row) completes only if no newer
  // intent (edit / configure / new / back / details / restart) happened meanwhile,
  // so a stale completion never resets a draft the user typed into.
  const kbCatalogGen = useRef(0);
  const editorGen = useRef(0);
  const nextEditorIntent = () => ++editorGen.current;

  useEffect(loadConnections, []);

  useEffect(() => {
    // Mountable assets come from the registry catalog: only APPROVED records
    // are offered, so the registry lifecycle gates availability.
    fetch("/api/registry/attachables")
      .then((res) => (res.ok ? res.json() : { mcp_servers: [], skills: [], agents: [] }))
      .then((d: { mcp_servers: AttachableMcp[]; skills: AttachableSkill[]; agents?: AttachableAgent[] }) => {
        setGatewayTargets(d.mcp_servers.filter((m) => m.gateway));
        setRemoteMcp(d.mcp_servers.filter((m) => !m.gateway));
        setAgentCatalog(d.agents ?? []);
        setSkillCatalog(d.skills);
      })
      .catch(() => {
        /* registry not bootstrapped — chips stay hidden */
      });
    // Managed KB catalog — failures are tolerated: an empty catalog just leaves
    // the Knowledge section empty and never blocks the wizard.
    const catalogGen = kbCatalogGen.current;
    fetch("/api/knowledge-bases")
      .then((res) => (res.ok ? res.json() : { items: [] }))
      .then((d: { items: AttachableKb[] }) => {
        // superseded by a pinned system-edit read ⇒ this (possibly foreign) list is dropped
        if (!alive.current || kbCatalogGen.current !== catalogGen) return;
        setKbCatalog(d.items ?? []);
      })
      .catch(() => {
        /* KB catalog unavailable — section stays empty */
      });
    // Memory resources — offered as a per-agent pick; a failed list just
    // leaves the selector on the workspace's shared default memory.
    void api
      .memoryResources()
      .then((d) => setMemoryOptions(d.items))
      .catch(() => {
        /* memory list unavailable — default memory only */
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
    | null
  >(null);

  const failureToasted = useRef(false);
  const poll = useCallback(async () => {
    if (!launch) return;
    try {
      const agent = await api.getAgent(launch.agentId, launch.workspaceId);
      setDeployment(agent.deployments?.[0] ?? null);
      setJob(await api.getJob(launch.jobId, launch.workspaceId));
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


  // A2A zip agents render from strands_a2a_agent and stay on the Converse path —
  // the Model source control is hidden and pinned to A2A_MODEL_SOURCE for them.
  const isA2a = method === "zip_runtime" && protocol === "a2a";

  // Switching source re-seeds the model to that source's catalog default. `forMethod`
  // is the method the form is landing on — during a switch, state still holds the old one.
  const applyModelSource = (source: ModelSource, forMethod: Method = method) => {
    setModelSource(source);
    setModelId(defaultModelForMethod(forMethod, source));
    setCustomModel(false);
    // the byoc allowed-models list is seeded from the same catalog
    setByocModels([defaultModelFor(source)]);
    setByocModelCustomOpen(false);
    setByocModelDraft("");
  };

const deployLock = !canDeploy
    ? ({ opacity: 0.45, pointerEvents: "none" } as CSSProperties)
    : undefined;
  
  const pickMethod = (next: Method) => {
    if (next === method) return;
    setMethod(next);
    // protocol survives a method switch, so re-entering zip_runtime with A2A
    // still selected must land back on the pinned source, not the default.
    applyModelSource(sourceOnMethodSwitch(next, protocol), next);
  };

  // `/agents/new?method=zip_runtime|byoc` (the V2 console's hand-off for
  // the methods it has no native form for) opens the configure step directly.
  const prefillMethod = params.get("method");
  useEffect(() => {
    if (mode !== "new") return;
    if (prefillMethod !== "zip_runtime" && prefillMethod !== "byoc") {
      return;
    }
    pickMethod(prefillMethod);
    setStep(2);
    // once per landing: pickMethod is recreated every render
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, prefillMethod]);

  const resetForm = () => {
    // back / restart / a new edit: any pending editor open or catalog read is stale
    nextEditorIntent();
    kbCatalogGen.current += 1;
    setEditing(null);
    setName("");
    applyModelSource(sourceForMethod(method));
    setSystemPrompt("");
    setMaxTokens("");
    setReasoningEffort(EFFORT_NONE);
    setMaxIterations(String(DEFAULT_MAX_ITERATIONS));
    setTimeoutSeconds(String(DEFAULT_TIMEOUT_SECONDS));
    setSystemKbError(null);
    setSubmitErrorRows([]);
    setTools([]);
    setToolkits([]);
    setSelectedGateway([]);
    setStoredGatewayConfig({});
    setSelectedMcp([]);
    setAuthTools([]);
    setPolicyIds([]);
    setSelectedKbs([]);
    setSpecKbs([]);
    setDetailKbs([]);
    setSkills([]);
    setAllowedTools(null);
    setNativeTools([...DEFAULT_HARNESS_NATIVE_TOOLS]);
    setLongTerm(true);
    setCustomSkills([]);
    setPendingSkills(null);
    setGitOpen(false);
    setGitUrl("");
    setSessionFs(true);
    setSessionMount(DEFAULT_SESSION_MOUNT);
    setProtocol("http");
    setA2aSkills([]);
    setByocKind("code_zip");
    setByocUpload(null);
    setByocUploading(false);
    setByocImageUri("");
    setByocEntrypoint("main.py");
    setByocPython("PYTHON_3_13");
    setByocInstallReqs(true);
    setByocRawContract(false);
    setByocModels([defaultModelFor(sourceForMethod(method))]);
    setByocModelCustomOpen(false);
    setByocModelDraft("");
    setByocEnvRows([]);
    setByocContractOpen(false);
    setByocDescription("");
    setSubmitError(null);
  };

  // Tool names the selected toolkits contribute. Non-empty ⇒ they replace the
  // template's own calculator/current_utc_time, matching what the backend emits.
  const toolkitToolNames = toolkitTools(toolkits);

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

  // N10v2-bug5: other Launchpad agents (APPROVED A2A records) — same selection for the
  // harness and the HTTP zip runtime; both bake an ask_<agent> tool.
  const agentChips = () =>
    agentCatalog.map((peer) => (
      <button
        key={peer.record_id}
        type="button"
        data-testid={`a2a-peer-${peer.name}`}
        className={`selchip${selectedAgents.includes(peer.name) ? " on" : ""}`}
        disabled={!peer.attachable}
        style={{ cursor: peer.attachable ? "pointer" : "not-allowed" }}
        title={peer.attachability_reason ?? peer.description}
        onClick={() => {
          if (!peer.attachable) return;
          setSelectedAgents((prev) =>
            prev.includes(peer.name) ? prev.filter((x) => x !== peer.name) : [...prev, peer.name],
          );
        }}
      >
        {peer.name} · a2a {peer.attachable ? (selectedAgents.includes(peer.name) ? "✓" : "+") : "—"}
      </button>
    ));

  const toggleToolkit = (kit: (typeof TOOLKITS)[number]) => {
    const on = toolkits.includes(kit.name);
    setToolkits((prev) => (on ? prev.filter((x) => x !== kit.name) : [...prev, kit.name]));
    if (on) return;
    // Offer the toolkit's default prompt, but never clobber the user's own text —
    // only an empty box or another toolkit's untouched default is replaced.
    setSystemPrompt((prev) => promptWithToolkit(prev, kit.prompt));
  };

  // Resolve a KB id to its name/description, preferring the live catalog and
  // falling back to the loaded spec so out-of-catalog KBs keep their label.
  const kbInfo = (id: string): KbRef => resolveKb(id, kbCatalog, specKbs);

  // Only ACTIVE managed KBs are selectable; the catalog may already exclude
  // non-managed KBs, so the type guard is defensive.
  const activeKbs = selectableKbs(kbCatalog);

  // The shared form model (lib/agent-spec.ts) builds the spec the V2 wizard posts too.
  const agentForm = (): AgentForm => ({
    method,
    name,
    modelId,
    modelSource,
    systemPrompt,
    maxTokens,
    reasoningEffort,
    maxIterations,
    timeoutSeconds,
    tools,
    toolkits,
    selectedGateway,
    selectedMcp,
    selectedAgents,
    selectedKbs,
    skills,
    allowedTools,
    nativeTools,
    longTerm,
    memoryId,
    sessionFs,
    sessionMount,
    protocol,
    a2aSkills,
    byocKind,
    byocUploadId: byocUpload?.upload_id ?? null,
    byocImageUri,
    byocEntrypoint,
    byocPython,
    byocInstallReqs,
    byocCnTraceExporter: true, // N17-obs465: the classic wizard keeps the default (V2 has the toggle)
    byocRawContract,
    byocModels,
    byocEnvRows,
    byocDescription,
    authTools,
    policyIds,
  });
  const specCatalogs: AgentFormCatalogs = { gatewayTargets, remoteMcp, agents: agentCatalog, storedGatewayConfig, kbInfo };

  const buildSpec = (): AgentSpecInput => buildAgentSpec(agentForm(), specCatalogs);

  /* ── system-preset edit: the same page, a different save ─────────────── */

  const systemEdit = editing?.system ?? null;
  // the page is read-only for a member's review (and for an administrator while
  // the preset is not settled / the workspace lost a prerequisite)
  const locked = systemEdit ? !systemEdit.editable : false;
  // a converted agent's exported code bakes its prompt and model; the backend refuses
  // changing either on re-publish, so both are read-only while editing one
  // re-publish of an agent deployed without short-term memory (kept off on save)
  const storedShortOff =
    (editing?.spec as { memory?: { short_term?: boolean } } | undefined)?.memory?.short_term === false;
  const bakedLocked =
    locked || Boolean((editing?.spec as { code_bundle?: unknown } | undefined)?.code_bundle);
  const presetForm = (): PresetForm => ({
    model_source: modelSource,
    model_id: modelId,
    max_tokens: maxTokens,
    reasoning_effort: reasoningEffort,
    system_prompt: systemPrompt,
    max_iterations: maxIterations,
    timeout_seconds: timeoutSeconds,
    knowledge_bases: selectedKbs.map(kbInfo),
  });
  // the PARTIAL edit a system save sends: only the members that differ from what
  // is stored (`{}` ⇒ nothing changed ⇒ an explicit re-publish is a forced repair)
  const systemBody: SystemPresetInstallInput = systemEdit
    ? diffPresetSettings(presetForm(), systemEdit.stored)
    : {};
  const systemChanged = Object.keys(systemBody).length > 0;
  const knobIssues =
    method === "harness"
      ? knobProblems(
          { max_tokens: maxTokens, max_iterations: maxIterations, timeout_seconds: timeoutSeconds },
          t,
        )
      : [];
  const effortAllowed = supportsReasoningEffort(modelId.trim(), modelSource);
  const applyPresetForm = (form: PresetForm) => {
    setModelSource(form.model_source);
    setModelId(form.model_id);
    setCustomModel(isCustomModelId(form.model_id, form.model_source));
    setMaxTokens(form.max_tokens);
    setReasoningEffort(form.reasoning_effort);
    setSystemPrompt(form.system_prompt);
    setMaxIterations(form.max_iterations);
    setTimeoutSeconds(form.timeout_seconds);
    setSelectedKbs(form.knowledge_bases.map((kb) => kb.kb_id));
    setSpecKbs(form.knowledge_bases);
  };
  // "differs from this build's default" hint next to a label (system edit only)
  const defaultHint = (key: keyof SystemPresetSettings) => {
    if (!systemEdit || isPresetDefault(key, presetForm(), systemEdit.defaults)) return null;
    const defaults = systemEdit.defaults;
    return (
      <span className="dim mono" style={{ fontSize: 10 }} data-testid={`differs-${key}`}>
        {" "}
        · {t("create.system.settings.differsFromDefault", {
          value:
            key === "knowledge_bases"
              ? t("create.system.settings.kbNoneShort")
              : key === "system_prompt"
                ? t("create.system.settings.buildPrompt")
                : String(defaults[key] ?? t("create.system.settings.effortNone")),
        })}
      </span>
    );
  };
  // The KB catalog for a system edit is read through the typed client pinned to the
  // workspace the row came from (the mount-time fetch follows the shared selection).
  // Each call takes ownership of the catalog: an older read (generic or pinned) that
  // lands afterwards is dropped, so two loads out of order settle on the newest.
  const loadSystemKbCatalog = (pinned: string | null) => {
    const catalogGen = ++kbCatalogGen.current;
    const current = () => alive.current && kbCatalogGen.current === catalogGen;
    setSystemKbLoading(true);
    setSystemKbError(null);
    void api
      .listAttachableKnowledgeBases(pinned)
      .then((d) => {
        if (!current()) return;
        setKbCatalog(d.items ?? []);
      })
      .catch((err: unknown) => {
        if (!current()) return;
        setSystemKbError(
          err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err),
        );
      })
      .finally(() => {
        if (current()) setSystemKbLoading(false);
      });
  };

  const submit = async () => {
    if (submitting) return; // one request at a time — a double click posts once
    setSubmitError(null);
    setSubmitErrorRows([]);
    setSubmitting(true);
    try {
      if (systemEdit) {
        if (!systemEdit.editable) return; // a review never posts
        // pinned to the workspace the row was read from: another tab moving the
        // shared selection meanwhile cannot redirect this save
        const res = await api.installSystemPreset(
          systemEdit.key,
          systemChanged ? systemBody : { force: true },
          systemEdit.workspaceId,
        );
        if (!alive.current) return;
        if (!res.job_id) {
          toast(t("create.system.alreadyCurrent", { name: systemEdit.label }), "good");
          return;
        }
        toast(
          t(res.changed ? "create.system.settings.saved" : "create.system.alreadyCurrent", {
            name: systemEdit.label,
          }),
          "good",
        );
        setDetailKbs(res.preset.knowledge_bases);
        failureToasted.current = false;
        setLaunch({ agentId: res.agent.id, jobId: res.job_id, workspaceId: systemEdit.workspaceId });
        setAgentStatus("deploying");
        setStep(3);
        reloadAgents();
        return;
      }
      const spec = buildSpec();
      const res = editing
        ? await api.redeployAgent(editing.id, republishSpec(spec, editing.spec))
        : await api.createAgent(spec);
      if (!alive.current) return;
      setDetailKbs((spec as { knowledge_bases?: KbRef[] }).knowledge_bases ?? []);
      failureToasted.current = false;
      setLaunch({ agentId: res.agent.id, jobId: res.job_id });
      setAgentStatus("deploying");
      setStep(3);
      reloadAgents();
    } catch (err) {
      if (!alive.current) return;
      setSubmitError(
        err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err),
      );
      if (err instanceof ApiError) setSubmitErrorRows(apiErrorRows(err.detail));
    } finally {
      if (alive.current) setSubmitting(false);
    }
  };

  /**
   * CONFIGURE / VIEW SETTINGS on a system preset (from the panel, or EDIT on its row
   * in the table): the same configure page as an ordinary edit, prefilled from the
   * preset's STORED settings (never the wizard defaults) and saved through the
   * maintenance route. Nothing here reaches the network except the pinned KB
   * catalog read for an editable session.
   */
  const startSystemEdit = ({ preset, workspaceId: pinned, editable, readOnlyReason }:
    PresetConfigureRequest) => {
    if (!preset.agent_id || !("model_id" in preset.settings)) return;
    const stored = preset.settings as SystemPresetSettings;
    resetForm(); // also a new editor intent + catalog generation
    setEditing({
      id: preset.agent_id,
      name: preset.name,
      method: "harness",
      system: {
        key: preset.key,
        label: preset.label,
        workspaceId: pinned,
        stored,
        defaults: preset.defaults,
        editable,
        readOnlyReason,
        status: preset.status,
        allowedTools: preset.allowed_tools,
        skillName: preset.name,
        skillVersion: preset.installed_skill_version ?? preset.skill_version,
      },
    });
    setMethod("harness");
    setName(preset.name);
    applyPresetForm(formFromSettings(stored));
    setAllowedTools(preset.allowed_tools); // display only — never sent from here
    setLongTerm(false);
    setSubmitError(null);
    setStep(2);
    if (editable) loadSystemKbCatalog(pinned);
  };

  // EDIT on a system row of the table: fed by a fresh pinned read of the preset row
  // (stored settings + server verdicts), never by the agent's raw spec
  const openSystemEdit = async (agent: AgentInfo) => {
    const startedIn = workspaceId;
    const intent = nextEditorIntent();
    // a newer intent (another edit, configure, new, back, details, restart, leave)
    // owns the editor now: this completion — success or error — is dropped silently
    const current = () => alive.current && editorGen.current === intent;
    try {
      const res = await api.listSystemPresets(startedIn);
      if (!current()) return;
      const preset = res.presets.find((row) => row.key === agent.system?.key);
      if (!preset) {
        toast(t("create.system.settings.rowMissing"));
        return;
      }
      startSystemEdit(presetConfigureRequest(preset, startedIn, isAdmin, t));
    } catch (err) {
      if (!current()) return;
      toast(err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err));
    }
  };

  const startEdit = (agent: AgentInfo) => {
    nextEditorIntent();
    const spec = (agent.spec ?? {}) as StoredSpec;
    setEditing({ id: agent.id, name: agent.name, method: agent.method as Method, spec: agent.spec });
    setMethod(agent.method as Method);
    setName(agent.name);
    // A spec stored before model_source existed is a Converse-API agent; an id
    // outside the catalog rides the "Custom model ID…" branch.
    const storedModel = spec.model_id ?? SPEC_DEFAULT_MODEL_ID;
    const storedSource = spec.model_source ?? "bedrock";
    setModelId(storedModel);
    setModelSource(storedSource);
    // Custom unless the id is actually offered for the stored source — covers
    // unknown ids and an id belonging to the other source.
    setCustomModel(isCustomModelId(storedModel, storedSource));
    setSystemPrompt(spec.system_prompt ?? "");
    // knobs come back exactly as stored — a re-publish must not reset them
    setMaxTokens(spec.max_tokens == null ? "" : String(spec.max_tokens));
    setReasoningEffort(spec.reasoning_effort ?? EFFORT_NONE);
    setMaxIterations(String(spec.max_iterations ?? DEFAULT_MAX_ITERATIONS));
    setTimeoutSeconds(String(spec.timeout_seconds ?? DEFAULT_TIMEOUT_SECONDS));
    setSystemKbError(null);
    setSubmitErrorRows([]);
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
    // an mcp tool carrying auth is an Identity row, not a catalog pick
    setSelectedMcp(
      (spec.tools ?? []).filter((x) => x.type === "mcp" && !isAuthTool(x)).map((x) => x.name),
    );
    setAuthTools((spec.tools ?? []).filter(isAuthTool).map(authRowFromStored));
    setPolicyIds(Array.isArray(spec.policy_ids) ? spec.policy_ids.filter((id) => typeof id === "string") : []);
    // absent on every zip spec written before toolkits existed
    setToolkits((spec.toolkits ?? []).filter((k) => TOOLKITS.some((x) => x.name === k)));
    setSelectedKbs((spec.knowledge_bases ?? []).map((k) => k.kb_id));
    setSpecKbs(spec.knowledge_bases ?? []);
    setSkills(spec.skills ?? []);
    setAllowedTools(spec.allowed_tools ?? null);
    setNativeTools(agent.method === "harness" ? spec.native_tools ?? [] : []);
    setLongTerm(spec.memory?.long_term ?? true);
    setMemoryId(spec.memory?.memory_id ?? "");
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
    setProtocol(spec.protocol ?? "http");
    setA2aSkills(
      (spec.a2a_skills ?? []).map((s) => ({
        name: s.name ?? "",
        description: s.description ?? "",
        tags: (s.tags ?? []).join(", "),
      })),
    );
    if (agent.method === "byoc" && spec.byoc) {
      setByocKind(spec.byoc.artifact_kind);
      setByocImageUri(spec.byoc.image_uri ?? "");
      setByocEntrypoint(spec.byoc.entrypoint ?? "main.py");
      setByocPython(spec.byoc.python_version ?? "PYTHON_3_13");
      setByocInstallReqs(spec.byoc.install_requirements ?? true);
      setByocRawContract(spec.byoc.invoke_contract === "raw");
      // a spec stored before allowed_models existed reads back as its one model
      setByocModels(
        spec.byoc.allowed_models?.length ? spec.byoc.allowed_models : [storedModel],
      );
      setByocModelCustomOpen(false);
      setByocModelDraft("");
      setByocDescription(spec.system_prompt ?? "");
      setByocEnvRows(Object.entries(spec.env ?? {}).map(([key, value]) => ({ key, value })));
      // a re-publish reuses the stored upload unless a new zip is staged
      if (spec.byoc.upload_id) {
        void api
          .getByocUpload(spec.byoc.upload_id)
          .then((info) => setByocUpload(info))
          .catch(() => {
            /* manifest gone — the member must upload a fresh zip to change code */
          });
      }
    }
    setSubmitError(null);
    setStep(2);
  };


  // `/agents/:agentId/edit` opens one agent straight from the URL. Read the row
  // directly rather than waiting for the list (a fresh deploy may not be in it
  // yet); an unknown id falls back to the list.
  const routeOpened = useRef<string | null>(null);
  useEffect(() => {
    if (!agentId || mode !== "edit") return;
    const key = `${mode}:${agentId}`;
    if (routeOpened.current === key) return;
    routeOpened.current = key;
    void api
      .getAgent(agentId)
      .then((fresh) => {
        if (!alive.current) return;
        const info: AgentInfo = { ...fresh, deployment: fresh.deployments?.[0] };
        if (info.system) void openSystemEdit(info);
        else if (info.method === "studio")
          navigate(`/create/studio?agent=${info.id}`, { replace: true });
        else startEdit(info);
      })
      .catch((err) => {
        if (!alive.current) return;
        toast(err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : String(err));
        navigate("/agents", { replace: true });
      });
    // startEdit/openSystemEdit are stable per mount for this purpose
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mode, agentId]);

  // A deploy started from `/agents/new` hands over to the agent's own page once
  // it succeeds (a failed one stays on the wizard with its error and RESTART).
  // System-preset saves are pinned to another workspace and stay put.
  useEffect(() => {
    if (mode !== "new" || !launch || launch.workspaceId) return;
    if (agentStatus === "active") navigate(`/agents/${launch.agentId}`, { replace: true });
  }, [mode, launch, agentStatus, navigate]);

  const doDelete = async (id: string) => {
    try {
      await api.deleteAgent(id);
      toast(t("create.list.deleted"));
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

  const uploadByocZip = async (file: File, pythonVersion?: ByocPythonVersion) => {
    setByocUploading(true);
    try {
      const info = await api.uploadByocArtifact(file, pythonVersion ?? byocPython);
      if (!alive.current) return;
      byocLastFile.current = file;
      setByocUpload(info);
      setByocEntrypoint(entrypointAfterUpload(info.detected.entrypoint_candidates, byocEntrypoint));
    } catch (err) {
      if (alive.current) toast(apiMsg(err));
    } finally {
      if (alive.current) setByocUploading(false);
    }
  };

  const changeByocPython = (version: ByocPythonVersion) => {
    setByocPython(version);
    // the pre-resolve result is per-Python-version — re-check the staged zip
    if (byocLastFile.current && byocUpload?.detected.has_requirements) {
      void uploadByocZip(byocLastFile.current, version);
    }
  };

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

  /* ── configure-step gate (per-method rules in lib/agent-spec.ts) ─────── */

  const configValid = agentFormValid(agentForm(), specCatalogs, {
    knobIssues,
    byocUploading,
    connections,
  });

  return (
    <section>
      <nav className="agents-crumb" aria-label={t("agents.breadcrumb")}>
        <Link to="/agents">{t("create.title")}</Link>
        <span aria-hidden="true"> / </span>
        <span>
          {mode === "new"
            ? t("agents.crumbNew")
            : mode === "edit"
              ? t("agents.crumbEdit", { name: editing?.name ?? "" })
              : (launch && agents.find((a) => a.id === launch.agentId)?.name) ||
                t("agents.crumbDetail")}
        </span>
      </nav>
      {presetsOnly && step === 1 ? (
        <ViewHead kicker={t("create.kicker")} title={t("create.system.title")} meta={t("create.system.sub")} />
      ) : (
        <ViewHead kicker={t("create.kicker")} title={t("create.title")} meta={t("create.meta")} />
      )}

      {(!presetsOnly || step > 1) && (
      <div className="steps">
        {([1, 2, 3] as const).map((n) => (
          <div key={n} className={`step${step === n ? " now" : step > n ? " done" : ""}`}>
            <span className="n">{step > n ? "✓" : `0${n}`}</span>
            <b>{t(`create.steps.${n}`)}</b>
          </div>
        ))}
      </div>
      )}

      {step === 1 && (
        <>
          {!presetsOnly && (
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
              <Link
                className="studio-link"
                to="/create/assistant"
                onClick={(e) => e.stopPropagation()}
                data-testid="open-assistant"
              >
                {t("create.methods.harness.assistant")}
              </Link>
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
              className={`method${method === "byoc" ? " sel" : ""}`}
              style={{ "--i": 2, ...deployLock } as CSSProperties}
              onClick={() => pickMethod("byoc")}
              data-method="byoc"
            >
              <div className="m-badge">{t("create.methods.byoc.badge")}</div>
              <div className="m-icon">⬆</div>
              <h3>{t("create.methods.byoc.title")}</h3>
              <p>{t("create.methods.byoc.desc")}</p>
              <div className="m-specs">
                <span>ZIP → Runtime · Dockerfile → CodeBuild → ECR → Runtime</span>
                <span>{t("create.methods.byoc.spec2")}</span>
                <span>{t("create.methods.byoc.spec3")}</span>
              </div>
            </div>
          </div>
          <div style={{ display: "flex", justifyContent: "space-between", gap: 10 }}>
            <Btn onClick={() => navigate("/agents/import")}>
              <Search size={14} aria-hidden="true" />
              {t("create.methods.discovery.title")}
            </Btn>
            <span title={canDeploy ? undefined : t("create.permissionRequired")}>
              <Btn
                primary
                disabled={!canDeploy}
                onClick={() => {
                  nextEditorIntent(); // a new agent draft supersedes any pending open
                  setStep(2);
                }}
              >
                {t("create.next")} ▸
              </Btn>
            </span>
          </div>

          </>
          )}
          {/* System presets install from the create page (below the method cards),
              as they did before the list split — configure opens the shared editor
              on step 2 right here; details go to the agent's page. */}
          <div style={{ height: 18 }} />
          <SystemPresetsPanel
            onChanged={reloadAgents}
            onConfigure={startSystemEdit}
            onDetails={(id) => navigate(`/agents/${id}`)}
          />
        </>
      )}


      {step === 2 && (
        <div className="cfg-grid">
          <Panel
            brk
            data-testid="configure-step"
            data-system-edit={systemEdit ? (locked ? "review" : "edit") : undefined}
            title={
              systemEdit
                ? t("create.system.settings.title", { name: systemEdit.label })
                : t(
                    method === "harness"
                      ? "create.configure.title"
                      : method === "byoc"
                        ? "create.configure.titleByoc"
                        : "create.configure.titleZip",
                  )
            }
            sub={
              name
                ? method === "harness"
                  ? `harnessName: ${name.replace(/-/g, "_")}`
                  : `runtime: ${name.replace(/-/g, "_")}_*`
                : undefined
            }
            style={{ "--i": 0 } as CSSProperties}
          >
            {editing && !systemEdit && (
              <div className="note" style={{ borderColor: "var(--amber)", marginBottom: 12 }}>
                <span className="i" style={{ color: "var(--amber)" }}>
                  [⟳]
                </span>
                <span>{t("create.editing", { name: editing.name })}</span>
              </div>
            )}
            {systemEdit && (
              <div
                className="note"
                style={{ borderColor: "var(--amber)", marginBottom: 12 }}
                data-testid="system-edit-note"
              >
                <span className="i" style={{ color: "var(--amber)" }}>◈</span>
                <span>{t("create.system.settings.editingNote", { label: systemEdit.label })}</span>
              </div>
            )}
            {bakedLocked && !locked && (
              <div className="note" style={{ marginBottom: 12 }} data-testid="converted-edit-note">
                <span className="i">[i]</span>
                <span>{t("v2.agents.wizard.convertedNote")}</span>
              </div>
            )}
            {systemEdit && locked && (
              <div className="note" style={{ marginBottom: 12 }} data-testid="preset-settings-readonly">
                <span className="i">[i]</span>
                <span>{systemEdit.readOnlyReason ?? t("create.system.settings.readOnly")}</span>
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
            {method === "byoc" && !systemEdit && (
              <>
                <div className="field">
                  <label>{t("create.configure.byocKind")}</label>
                  <div className="selchips">
                    {(["code_zip", "container_source", "container_image"] as const).map(
                      (kind) => (
                        <button
                          key={kind}
                          type="button"
                          data-testid={`byoc-kind-${kind}`}
                          className={`selchip${byocKind === kind ? " on" : ""}`}
                          style={{ cursor: "pointer" }}
                          title={t(`create.configure.byocKindDesc.${kind}`)}
                          onClick={() => setByocKind(kind)}
                        >
                          {t(`create.configure.byocKindName.${kind}`)}{" "}
                          {byocKind === kind ? "✓" : ""}
                        </button>
                      ),
                    )}
                  </div>
                </div>
                {byocKind !== "container_image" && (
                  <div className="field">
                    <label>{t("create.configure.byocUpload")}</label>
                    <input
                      ref={byocFileRef}
                      type="file"
                      accept=".zip"
                      style={{ display: "none" }}
                      onChange={(e) => {
                        const file = e.target.files?.[0];
                        e.target.value = "";
                        if (file) void uploadByocZip(file);
                      }}
                    />
                    <div
                      className="field"
                      data-testid="byoc-dropzone"
                      style={{
                        border: "1px dashed var(--line)",
                        padding: 14,
                        textAlign: "center",
                        cursor: "pointer",
                      }}
                      onClick={() => byocFileRef.current?.click()}
                      onDragOver={(e) => e.preventDefault()}
                      onDrop={(e) => {
                        e.preventDefault();
                        const file = Array.from(e.dataTransfer.files).find((f) =>
                          f.name.toLowerCase().endsWith(".zip"),
                        );
                        if (file) void uploadByocZip(file);
                      }}
                    >
                      {byocUploading ? (
                        <span className="dim">{t("create.configure.byocUploading")}</span>
                      ) : byocUpload ? (
                        <span className="mono" style={{ fontSize: 12 }}>
                          {byocUpload.original_filename} ·{" "}
                          {(byocUpload.size_bytes / 1e6).toFixed(1)}MB · sha256{" "}
                          {byocUpload.sha256.slice(0, 12)}… ({byocUpload.entries_count}{" "}
                          {t("create.configure.byocEntries")})
                        </span>
                      ) : (
                        <span className="dim">{t("create.configure.byocDrop")}</span>
                      )}
                    </div>
                    {byocUpload && byocKind === "code_zip" &&
                      !byocUpload.detected.agentcore_sdk_detected && (
                        <div className="note" style={{ borderColor: "var(--amber)" }}>
                          <span className="i" style={{ color: "var(--amber)" }}>[!]</span>
                          <span>{t("create.configure.byocNoSdkWarn")}</span>
                        </div>
                      )}
                    {byocUpload && byocKind === "container_source" &&
                      !byocUpload.detected.has_dockerfile && (
                        <div className="note" style={{ borderColor: "var(--amber)" }}>
                          <span className="i" style={{ color: "var(--amber)" }}>[!]</span>
                          <span>{t("create.configure.byocNoDockerfileWarn")}</span>
                        </div>
                      )}
                    {byocUpload && byocKind === "code_zip" && byocInstallReqs &&
                      byocUpload.detected.requirements &&
                      byocUpload.detected.requirements.status !== "skipped" && (
                        byocUpload.detected.requirements.status === "ok" ? (
                          <div
                            className="note"
                            style={{ borderColor: "var(--good)" }}
                            data-testid="byoc-reqs-ok"
                          >
                            <span className="i" style={{ color: "var(--good)" }}>✓</span>
                            <span>
                              {t("create.configure.byocReqsOk", {
                                count: byocUpload.detected.requirements.package_count ?? 0,
                              })}
                            </span>
                          </div>
                        ) : (
                          <div
                            className="note"
                            style={{ borderColor: "var(--crit-text)" }}
                            data-testid="byoc-reqs-failed"
                          >
                            <span className="i" style={{ color: "var(--crit-text)" }}>[✗]</span>
                            <span>
                              {t("create.configure.byocReqsFailed")}{" "}
                              <span className="mono" style={{ fontSize: 11 }}>
                                {byocUpload.detected.requirements.error}
                              </span>
                            </span>
                          </div>
                        )
                      )}
                  </div>
                )}
                {byocKind === "container_image" && (
                  <div className="field">
                    <label htmlFor="byoc-image">{t("create.configure.byocImageUri")}</label>
                    <input
                      id="byoc-image"
                      className="input mono"
                      data-testid="byoc-image-uri"
                      value={byocImageUri}
                      onChange={(e) => setByocImageUri(e.target.value)}
                      placeholder="123456789012.dkr.ecr.cn-northwest-1.amazonaws.com.cn/my-agents:v1"
                    />
                    <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>
                      {t("create.configure.byocImageHint")}
                    </div>
                  </div>
                )}
                {byocKind === "code_zip" && (
                  <div className="preset-settings-grid">
                    <div className="field">
                      <label htmlFor="byoc-entrypoint">
                        {t("create.configure.byocEntrypoint")}
                      </label>
                      {byocUpload && byocUpload.detected.entrypoint_candidates.length > 0 ? (
                        <select
                          id="byoc-entrypoint"
                          className="input mono"
                          data-testid="byoc-entrypoint"
                          value={byocEntrypoint}
                          onChange={(e) => setByocEntrypoint(e.target.value)}
                        >
                          {[
                            ...byocUpload.detected.entrypoint_candidates,
                            ...(byocUpload.detected.entrypoint_candidates.includes(
                              byocEntrypoint,
                            )
                              ? []
                              : [byocEntrypoint]),
                          ].map((candidate) => (
                            <option key={candidate} value={candidate}>
                              {candidate}
                            </option>
                          ))}
                        </select>
                      ) : (
                        <input
                          id="byoc-entrypoint"
                          className="input mono"
                          data-testid="byoc-entrypoint"
                          value={byocEntrypoint}
                          onChange={(e) => setByocEntrypoint(e.target.value)}
                          placeholder="main.py"
                        />
                      )}
                    </div>
                    <div className="field">
                      <label htmlFor="byoc-python">{t("create.configure.byocPython")}</label>
                      <select
                        id="byoc-python"
                        className="input mono"
                        data-testid="byoc-python"
                        value={byocPython}
                        onChange={(e) => changeByocPython(e.target.value as ByocPythonVersion)}
                      >
                        {(["PYTHON_3_13", "PYTHON_3_12", "PYTHON_3_11", "PYTHON_3_10"] as const)
                          .map((v) => (
                            <option key={v} value={v}>
                              {v.replace("PYTHON_", "Python ").replace("_", ".")}
                            </option>
                          ))}
                      </select>
                    </div>
                  </div>
                )}
                {byocKind === "code_zip" && (
                  <div className="field">
                    <div className="selchips">
                      <button
                        type="button"
                        className={`selchip${byocInstallReqs ? " on" : ""}`}
                        style={{ cursor: "pointer" }}
                        onClick={() => setByocInstallReqs((v) => !v)}
                      >
                        {t("create.configure.byocInstallReqs")} {byocInstallReqs ? "✓" : "+"}
                      </button>
                      <button
                        type="button"
                        className={`selchip${byocRawContract ? " on" : ""}`}
                        style={{ cursor: "pointer" }}
                        title={t("create.configure.byocRawHint")}
                        onClick={() => setByocRawContract((v) => !v)}
                      >
                        {t("create.configure.byocRaw")} {byocRawContract ? "✓" : "+"}
                      </button>
                    </div>
                  </div>
                )}
                <div className="field">
                  <label htmlFor="byoc-desc">{t("create.configure.byocDescription")}</label>
                  <input
                    id="byoc-desc"
                    className="input"
                    value={byocDescription}
                    onChange={(e) => setByocDescription(e.target.value)}
                    placeholder={t("create.configure.byocDescriptionPlaceholder")}
                  />
                </div>
                <div className="field">
                  <label>{t("create.configure.byocEnv")}</label>
                  {byocEnvRows.map((row, i) => (
                    <div key={i} style={{ display: "flex", gap: 8, marginBottom: 6 }}>
                      <input
                        className="input mono"
                        style={{ flex: 1 }}
                        value={row.key}
                        aria-label={t("create.configure.byocEnvKey")}
                        placeholder="MODEL_ID"
                        onChange={(e) =>
                          setByocEnvRows((prev) =>
                            prev.map((r, j) => (j === i ? { ...r, key: e.target.value } : r)),
                          )
                        }
                      />
                      <input
                        className="input mono"
                        style={{ flex: 2 }}
                        value={row.value}
                        aria-label={t("create.configure.byocEnvValue")}
                        onChange={(e) =>
                          setByocEnvRows((prev) =>
                            prev.map((r, j) => (j === i ? { ...r, value: e.target.value } : r)),
                          )
                        }
                      />
                      <Btn
                        onClick={() =>
                          setByocEnvRows((prev) => prev.filter((_, j) => j !== i))
                        }
                      >
                        ✕
                      </Btn>
                    </div>
                  ))}
                  <Btn
                    className="small"
                    onClick={() => setByocEnvRows((prev) => [...prev, { key: "", value: "" }])}
                  >
                    + {t("create.configure.byocEnvAdd")}
                  </Btn>
                </div>
                <ClassicIdentitySection
                  form={agentForm()}
                  setRows={setAuthTools}
                  connections={connections}
                  catalogError={connectionsError}
                  onRetry={loadConnections}
                />
                <div className="field">
                  <button
                    type="button"
                    className="selchip"
                    style={{ cursor: "pointer" }}
                    data-testid="byoc-contract-toggle"
                    onClick={() => setByocContractOpen((v) => !v)}
                  >
                    {byocContractOpen ? "▾" : "▸"} {t("create.configure.byocContract")}
                  </button>
                  {byocContractOpen && (
                    <div className="note" style={{ marginTop: 8 }} data-testid="byoc-contract">
                      <span className="i">[i]</span>
                      <span style={{ whiteSpace: "pre-line" }}>
                        {t("create.configure.byocContractBody")}
                      </span>
                    </div>
                  )}
                </div>
              </>
            )}
            {!isA2a && (
              <div className="field">
                <label>{t("create.configure.modelSource")}</label>
                <div className="selchips">
                  {(["bedrock"] as const).map((source) => (
                    <button
                      key={source}
                      type="button"
                      data-testid={`model-source-${source}`}
                      className={`selchip${modelSource === source ? " on" : ""}`}
                      style={{ cursor: bakedLocked ? "default" : "pointer" }}
                      disabled={bakedLocked}
                      onClick={() => source !== modelSource && applyModelSource(source)}
                    >
                      {t("create.configure.modelSourceBedrock")} {modelSource === source ? "✓" : ""}
                    </button>
                  ))}
                </div>
                <div className="note" style={{ margin: "8px 0 0" }}>
                  <span className="i">[i]</span>
                  <span>{t("create.configure.modelSourceBedrockDesc")}</span>
                </div>
              </div>
            )}
            {method !== "byoc" && (
              <div className="field">
                <label htmlFor="agent-model-select">
                  {t("create.configure.model")}
                  {defaultHint("model_id")}
                </label>
                <select
                  id="agent-model-select"
                  className="input"
                  data-testid="model-select"
                  disabled={bakedLocked}
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
                  {modelOptionsFor(modelSource).map((option) => {
                    // N-R6-obs1: an unconfigured provider's row is disabled; tooltip = SSM parameter
                    const reason = modelUnavailableReason(option.model_id);
                    return (
                      <option
                        key={option.model_id}
                        value={option.model_id}
                        disabled={!option.available}
                        title={reason ? t("models.unavailable", reason) : undefined}
                        style={{ background: "var(--panel)" }}
                      >
                        {modelOptionLabel(option, t("models.unavailableShort"), true)}
                      </option>
                    );
                  })}
                  <option value={CUSTOM_MODEL_OPTION} style={{ background: "var(--panel)" }}>
                    {t("create.configure.modelCustom")}
                  </option>
                </select>
                {!customModel && (
                  <div className="note" style={{ margin: "6px 0 0" }} data-testid="model-provider-badge">
                    <span>
                      {(() => {
                        const reason = modelUnavailableReason(modelId);
                        return reason
                          ? t("models.unavailable", reason)
                          : t("models.badge", { provider: t(providerBadgeKey(modelId)) });
                      })()}
                    </span>
                  </div>
                )}
                {customModel && (
                  <input
                    id="agent-model"
                    className="input mono"
                    style={{ marginTop: 8 }}
                    data-testid="model-custom"
                    disabled={bakedLocked}
                    value={modelId}
                    onChange={(e) => setModelId(e.target.value)}
                    placeholder={t("create.configure.modelCustomPlaceholder")}
                  />
                )}
              </div>
            )}
            {method === "byoc" && (
              <div className="field">
                <label htmlFor="byoc-model-add">{t("create.configure.byocModels")}</label>
                {byocModels.map((model, i) => (
                  <div
                    key={`${model}-${i}`}
                    data-testid="byoc-model-row"
                    style={{ display: "flex", gap: 8, marginBottom: 6, alignItems: "center" }}
                  >
                    <span className="mono" style={{ flex: 1, fontSize: 12 }}>
                      {model}
                      {i === 0 && (
                        <span className="dim"> · {t("create.configure.byocModelPrimary")}</span>
                      )}
                    </span>
                    {i > 0 && (
                      <Btn
                        className="small"
                        data-testid={`byoc-model-primary-${i}`}
                        onClick={() =>
                          setByocModels((prev) => [
                            prev[i],
                            ...prev.filter((_, j) => j !== i),
                          ])
                        }
                      >
                        {t("create.configure.byocModelMakePrimary")}
                      </Btn>
                    )}
                    {byocModels.length > 1 && (
                      <Btn
                        className="small"
                        onClick={() => setByocModels((prev) => prev.filter((_, j) => j !== i))}
                      >
                        ✕
                      </Btn>
                    )}
                  </div>
                ))}
                <select
                  id="byoc-model-add"
                  className="input"
                  data-testid="byoc-model-add"
                  disabled={byocModels.length >= BYOC_MODELS_MAX}
                  value=""
                  onChange={(e) => {
                    const picked = e.target.value;
                    if (!picked) return;
                    if (picked === CUSTOM_MODEL_OPTION) {
                      setByocModelCustomOpen(true);
                      return;
                    }
                    setByocModelCustomOpen(false);
                    setByocModels((prev) => (prev.includes(picked) ? prev : [...prev, picked]));
                  }}
                >
                  <option value="" style={{ background: "var(--panel)" }}>
                    {t("create.configure.byocModelAdd")}
                  </option>
                  {modelOptionsFor(modelSource)
                    .filter((option) => !byocModels.includes(option.model_id))
                    .map((option) => {
                      const reason = modelUnavailableReason(option.model_id);
                      return (
                        <option
                          key={option.model_id}
                          value={option.model_id}
                          disabled={!option.available}
                          title={reason ? t("models.unavailable", reason) : undefined}
                          style={{ background: "var(--panel)" }}
                        >
                          {modelOptionLabel(option, t("models.unavailableShort"), true)}
                        </option>
                      );
                    })}
                  <option value={CUSTOM_MODEL_OPTION} style={{ background: "var(--panel)" }}>
                    {t("create.configure.modelCustom")}
                  </option>
                </select>
                {byocModelCustomOpen && (
                  <div style={{ display: "flex", gap: 8, marginTop: 8 }}>
                    <input
                      className="input mono"
                      style={{ flex: 1 }}
                      data-testid="byoc-model-custom"
                      value={byocModelDraft}
                      onChange={(e) => setByocModelDraft(e.target.value)}
                      placeholder={t("create.configure.modelCustomPlaceholder")}
                    />
                    <Btn
                      className="small"
                      data-testid="byoc-model-custom-add"
                      onClick={() => {
                        const id = byocModelDraft.trim();
                        if (!id || byocModels.length >= BYOC_MODELS_MAX) return;
                        setByocModels((prev) => (prev.includes(id) ? prev : [...prev, id]));
                        setByocModelDraft("");
                        setByocModelCustomOpen(false);
                      }}
                    >
                      + {t("create.configure.byocModelAddCustom")}
                    </Btn>
                  </div>
                )}
                <div className="note" style={{ margin: "8px 0 0" }} data-testid="byoc-model-note">
                  <span className="i">[i]</span>
                  <span>{t("create.configure.byocModelsHint")}</span>
                </div>
              </div>
            )}
            {/* Harness inference / loop knobs. Per-call output ceiling and reasoning
                effort map onto bedrockModelConfig; the loop bounds onto
                maxIterations / timeoutSeconds. Stored values round-trip untouched. */}
            {method === "harness" && (
              <div className="preset-settings-grid" data-testid="inference-knobs">
                <div className="field">
                  <label htmlFor="agent-max-tokens">
                    {t("create.system.settings.maxTokens")}
                    {defaultHint("max_tokens")}
                  </label>
                  <input
                    id="agent-max-tokens"
                    className="input mono"
                    inputMode="numeric"
                    data-testid="agent-max-tokens"
                    disabled={locked}
                    placeholder={t("create.system.settings.maxTokensEmpty")}
                    value={maxTokens}
                    onChange={(e) => setMaxTokens(e.target.value)}
                  />
                  <div className="dim" style={{ fontSize: 11, marginTop: 4 }}>
                    {t("create.system.settings.maxTokensHint")}
                  </div>
                </div>
                <div className="field">
                  <label htmlFor="agent-effort">
                    {t("create.system.settings.effort")}
                    {defaultHint("reasoning_effort")}
                  </label>
                  <select
                    id="agent-effort"
                    className="input"
                    data-testid="agent-effort"
                    disabled={locked || !effortAllowed}
                    value={effortAllowed ? reasoningEffort : EFFORT_NONE}
                    onChange={(e) => setReasoningEffort(e.target.value as EffortChoice)}
                  >
                    <option value={EFFORT_NONE} style={{ background: "var(--panel)" }}>
                      {t("create.system.settings.effortNone")}
                    </option>
                    {REASONING_EFFORTS.map((effort) => (
                      <option key={effort} value={effort} style={{ background: "var(--panel)" }}>
                        {t(`create.system.settings.effortLevels.${effort}`)}
                      </option>
                    ))}
                  </select>
                  <div
                    className="dim"
                    style={{ fontSize: 11, marginTop: 4 }}
                    data-testid="agent-effort-hint"
                  >
                    {effortAllowed
                      ? t("create.system.settings.effortHint")
                      : t("create.system.settings.effortUnsupported")}
                  </div>
                </div>
                <div className="field">
                  <label htmlFor="agent-max-iterations">
                    {t("create.system.settings.maxIterations")}
                    {defaultHint("max_iterations")}
                  </label>
                  <input
                    id="agent-max-iterations"
                    className="input mono"
                    inputMode="numeric"
                    data-testid="agent-max-iterations"
                    disabled={locked}
                    value={maxIterations}
                    onChange={(e) => setMaxIterations(e.target.value)}
                  />
                </div>
                <div className="field">
                  <label htmlFor="agent-timeout">
                    {t("create.system.settings.timeout")}
                    {defaultHint("timeout_seconds")}
                  </label>
                  <input
                    id="agent-timeout"
                    className="input mono"
                    inputMode="numeric"
                    data-testid="agent-timeout"
                    disabled={locked}
                    value={timeoutSeconds}
                    onChange={(e) => setTimeoutSeconds(e.target.value)}
                  />
                </div>
              </div>
            )}
            {method === "harness" && (
              <div className="dim" style={{ fontSize: 11, marginBottom: 12 }}>
                {t("create.system.settings.loopNote")}
              </div>
            )}
            {method !== "byoc" && (
            <div className="field">
              <label htmlFor="agent-prompt">
                {t("create.configure.systemPrompt")}
                {defaultHint("system_prompt")}
              </label>
              {method === "harness" && (
                <div className="dim" style={{ fontSize: 11, marginBottom: 4 }}>
                  {t("v2.agents.wizard.reservedPlaceholder")}
                </div>
              )}
              <textarea
                id="agent-prompt"
                className="input mono"
                style={{ minHeight: 88, resize: "vertical" }}
                data-testid="agent-prompt"
                disabled={bakedLocked}
                value={systemPrompt}
                onChange={(e) => setSystemPrompt(e.target.value)}
                placeholder={t("create.configure.systemPromptPlaceholder")}
              />
              {systemEdit && !locked && systemPrompt !== systemEdit.defaults.system_prompt && (
                <Btn
                  className="small"
                  style={{ marginTop: 6 }}
                  data-testid="preset-settings-prompt-default"
                  onClick={() => setSystemPrompt(systemEdit.defaults.system_prompt)}
                >
                  {t("create.system.settings.usePromptDefault")}
                </Btn>
              )}
            </div>
            )}
            {/* A preset's capabilities are catalogue-owned: shown, never edited, and
                no skill upload/import or tool attachment is offered for it. */}
            {systemEdit && (
              <div className="field" data-testid="preset-protected">
                <label>{t("create.system.settings.protected")}</label>
                <div className="selchips">
                  {systemEdit.allowedTools.map((pattern) => (
                    <span key={pattern} className="selchip on" style={{ cursor: "default" }}>
                      {pattern} · {t("create.system.tools")}
                    </span>
                  ))}
                  <span
                    className="selchip on"
                    style={{ cursor: "default" }}
                    data-testid="preset-protected-skill"
                  >
                    {t("create.system.settings.skillVersion", {
                      name: systemEdit.skillName,
                      v: systemEdit.skillVersion ?? "?",
                    })}
                  </span>
                  <span className="selchip on" style={{ cursor: "default" }}>
                    {t("create.system.settings.memoryOff")}
                  </span>
                </div>
                <div className="note" style={{ marginTop: 8 }}>
                  <span className="i">◈</span>
                  <span>{t("create.system.settings.protectedNote")}</span>
                </div>
              </div>
            )}
            {!systemEdit && method !== "byoc" && (
            <div className="field">
              <label>
                {method === "harness"
                  ? t("create.configure.tools")
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
                    {agentChips()}
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
                    generated MCP client; A2A still cannot. */}
                {method === "zip_runtime" && !isA2a && gatewayChips()}
                {method === "zip_runtime" && !isA2a && agentChips()}
                {method === "zip_runtime" && isA2a && (
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
            )}
            {method === "zip_runtime" && (authToolsCarried({ method, protocol }) || authTools.length > 0) && (
              <ClassicIdentitySection
                form={agentForm()}
                setRows={setAuthTools}
                connections={connections}
                catalogError={connectionsError}
                onRetry={loadConnections}
              />
            )}
            {method === "harness" && !systemEdit && (
              <div className="field" data-testid="harness-native-tools">
                <label>{t("create.nativeTools.title")}</label>
                {allowedTools !== null ? (
                  <div className="note" style={{ marginBottom: 8 }}>
                    <span className="i">[!]</span>
                    <div>
                      <strong>{t("create.nativeTools.overrideTitle")}</strong>
                      <pre
                        className="mono"
                        style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere" }}
                        data-testid="harness-tools-override"
                      >
                        {JSON.stringify(allowedTools)}
                      </pre>
                      <p>{t("create.nativeTools.overrideHint")}</p>
                      <Btn
                        data-testid="harness-tools-reset"
                        onClick={() => setAllowedTools(null)}
                      >
                        {t("create.nativeTools.useSelected")}
                      </Btn>
                    </div>
                  </div>
                ) : (
                  <p className="dim" style={{ fontSize: 11 }}>
                    {t("create.nativeTools.selectedHint")}
                  </p>
                )}
                {allowedTools === null && nativeTools.length === 0 && (
                  <p data-testid="native-tools-unavailable">{t("create.nativeTools.unavailable")}</p>
                )}
                {HARNESS_NATIVE_TOOLS.map((tool) => (
                  <label
                    key={tool}
                    style={{
                      display: "block", marginTop: 8, fontSize: 12,
                      letterSpacing: 0, color: "var(--ink-2)",
                    }}
                  >
                    <input
                      type="checkbox"
                      checked={nativeTools.includes(tool)}
                      disabled={allowedTools !== null}
                      data-testid={`native-tool-${tool}`}
                      onChange={(e) => setNativeTools((current) =>
                        e.target.checked
                          ? [...current, tool]
                          : current.filter((value) => value !== tool),
                      )}
                    />{" "}
                    {t(`create.nativeTools.${tool}`)}
                  </label>
                ))}
              </div>
            )}
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
                      // the A2A template stays on the Converse path — see A2A_MODEL_SOURCE.
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
            {!systemEdit && method !== "byoc" && (
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
            {method !== "byoc" && (
            <div className="field" data-testid="kb-picker">
              <label>
                {t("create.configure.kbLabel")}
                {defaultHint("knowledge_bases")}
              </label>
              <div className="selchips">
                {activeKbs.map((kb) => (
                  <button
                    key={kb.kb_id}
                    type="button"
                    data-testid={`kb-${kb.kb_id}`}
                    className={`selchip${selectedKbs.includes(kb.kb_id) ? " on" : ""}`}
                    style={{ cursor: locked ? "default" : "pointer" }}
                    disabled={locked}
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
                        data-testid={`kb-${id}`}
                        className="selchip on"
                        style={{ cursor: locked ? "default" : "pointer" }}
                        disabled={locked}
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
              {systemEdit && systemKbLoading && (
                <div
                  className="dim mono"
                  style={{ fontSize: 11, marginTop: 6 }}
                  data-testid="preset-settings-kb-loading"
                >
                  {t("create.system.settings.kbLoading")}
                </div>
              )}
              {systemEdit && systemKbError && (
                <div
                  className="note"
                  style={{ borderColor: "var(--crit)", marginTop: 6 }}
                  data-testid="preset-settings-kb-error"
                >
                  <span className="i" style={{ color: "var(--crit)" }}>[!]</span>
                  <span>{t("create.system.settings.kbLoadFailed", { reason: systemKbError })}</span>
                  <Btn
                    data-testid="preset-settings-kb-retry"
                    disabled={submitting}
                    onClick={() => loadSystemKbCatalog(systemEdit.workspaceId)}
                  >
                    {t("create.system.retry")}
                  </Btn>
                </div>
              )}
              <div className="note" style={{ marginTop: 8 }}>
                <span className="i">[i]</span>
                <span>
                  {method === "harness"
                    ? t("create.configure.kbNote")
                    : t("create.configure.kbNoteDirect")}
                </span>
              </div>
            </div>
            )}
            {!systemEdit && (
            <>
            {method !== "byoc" && (
            <>
            <div className="field">
              <label>{t("create.configure.memory")}</label>
              <div className="selchips">
                {/* short-term memory has no toggle; an agent deployed without it keeps
                    it off on re-publish (republishSpec), and the chip says so */}
                {storedShortOff ? (
                  <span className="selchip" title={t("create.configure.memoryShortOffHint")} data-testid="memory-short-off">
                    {t("create.configure.memoryShort")} · {t("create.configure.memoryOff")}
                  </span>
                ) : (
                  <span className="selchip on">{t("create.configure.memoryShort")} ✓</span>
                )}
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
            <div className="field">
              <label htmlFor="agent-memory-select">
                {t("create.configure.memoryResource")}
              </label>
              <select
                id="agent-memory-select"
                className="input"
                data-testid="memory-select"
                value={memoryId}
                onChange={(e) => setMemoryId(e.target.value)}
              >
                <option value="">{t("create.configure.memoryDefault")}</option>
                {memoryOptions
                  // only memories this workspace manages may be pinned
                  .filter((m) => m.id && !m.is_default && m.managed)
                  .map((m) => (
                    <option
                      key={m.id}
                      value={m.id ?? ""}
                      disabled={m.status !== "ACTIVE"}
                    >
                      {m.name ?? m.id}
                      {m.status !== "ACTIVE" ? ` (${m.status ?? "?"})` : ""}
                    </option>
                  ))}
                {/* an edited spec may pin a memory that has since vanished from
                    the list — keep it selectable so re-publish round-trips */}
                {memoryId &&
                  !memoryOptions.some((m) => m.id === memoryId && m.managed) && (
                    <option value={memoryId}>
                      {memoryId}
                      {memoryOptions.some((m) => m.id === memoryId)
                        ? ` (${t("memoryPage.resources.external")})`
                        : ""}
                    </option>
                  )}
              </select>
              <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
                {t("create.configure.memoryResourceHint")}
              </div>
            </div>
            <div className="note">
              <span className="i">[i]</span>
              <span>{t("create.configure.note")}</span>
            </div>
            </>
            )}
            </>
            )}
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
              {systemEdit && !locked && (
                <div className="kv" data-testid="system-edit-pending">
                  <span className="k">{t("create.system.settings.pendingLabel")}</span>
                  <span className="v">
                    {systemChanged ? (
                      <Chip tone="amber" title={Object.keys(systemBody).join(", ")}>
                        {t("create.system.settings.pending", { n: Object.keys(systemBody).length })}
                      </Chip>
                    ) : (
                      <span className="dim">{t("create.system.settings.noChangesForce")}</span>
                    )}
                  </span>
                </div>
              )}
            </Panel>
            <div style={{ height: 14 }} />
            {knobIssues.length > 0 && (
              <div
                className="note"
                style={{ borderColor: "var(--crit)", marginBottom: 14 }}
                data-testid="preset-settings-problems"
              >
                <span className="i" style={{ color: "var(--crit)" }}>[!]</span>
                <span className="mono" style={{ fontSize: 11 }}>{knobIssues.join(" · ")}</span>
              </div>
            )}
            {submitError && (
              <div
                className="note"
                style={{ borderColor: "var(--crit)", marginBottom: 14 }}
                data-testid="submit-error"
              >
                <span className="i" style={{ color: "var(--crit)" }}>
                  [✕]
                </span>
                <span>
                  {submitError}
                  {submitErrorRows.length > 0 && (
                    <ul className="mono" style={{ fontSize: 11, margin: "6px 0 0", paddingLeft: 16 }}>
                      {submitErrorRows.map((row, i) => (
                        <li key={i}>{row}</li>
                      ))}
                    </ul>
                  )}
                </span>
              </div>
            )}
            <Panel>
              <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", flexWrap: "wrap" }}>
                <Btn
                  data-testid="configure-back"
                  disabled={submitting}
                  disabledReason={submitting ? t("create.system.settings.saving") : undefined}
                  onClick={() => {
                    if (mode === "edit") {
                      navigate("/agents");
                      return;
                    }
                    setStep(1);
                    resetForm();
                  }}
                >
                  ◂ {t(locked ? "common.close" : "create.back")}
                </Btn>
                {systemEdit && !locked && (
                  <Btn
                    data-testid="preset-settings-defaults"
                    disabled={submitting}
                    onClick={() =>
                      applyPresetForm({
                        ...formFromSettings(systemEdit.defaults),
                        knowledge_bases: selectedKbs.map(kbInfo), // mounts are kept
                      })
                    }
                  >
                    {t("create.system.settings.useDefaults")}
                  </Btn>
                )}
                {!locked && (
                  <Btn
                    primary
                    data-testid="launch-submit"
                    disabled={!configValid || !canDeploy || submitting}
                    disabledReason={knobIssues[0]}
                    onClick={() => (editing ? setConfirm({ kind: "republish" }) : void submit())}
                  >
                    {submitting
                      ? t("create.system.settings.saving")
                      : systemEdit
                        ? `⟳ ${t(systemChanged ? "create.system.settings.save" : "create.republish")}`
                        : editing
                          ? `⟳ ${t("create.republish")}`
                          : `▲ ${t("create.launch")}`}
                  </Btn>
                )}
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
            onRestart={() => navigate("/agents")}
          />
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
        title={t(systemEdit ? "create.system.settings.confirmTitle" : "create.republishConfirm.title")}
        body={
          systemEdit
            ? systemChanged
              ? t("create.system.settings.confirm", {
                  name: systemEdit.label,
                  fields: Object.keys(systemBody).join(", "),
                })
              : t("create.system.settings.confirmForce", { name: systemEdit.label })
            : t("create.republishConfirm.body", { name })
        }
        confirmLabel={t(
          systemEdit && systemChanged ? "create.system.settings.save" : "create.republish",
        )}
        onConfirm={() => {
          setConfirm(null);
          void submit();
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
