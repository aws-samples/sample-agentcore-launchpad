import { Plus, Trash2, Upload } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  BUILTIN_TOOLS,
  BYOC_BUILTINS,
  byocBuiltinLabelKey,
  BYOC_KINDS,
  BYOC_MODELS_MAX,
  BYOC_PYTHON_VERSIONS,
  type AgentForm,
  type ByocBuiltin,
  byoMountsAllowed,
  DEFAULT_SESSION_MOUNT,
  DISPLAY_NAME_MAX,
  filesystemIssues,
  hasByoMounts,
  hasNfsMounts,
  managedGatewayAttached,
  MAX_MOUNTS_PER_KIND,
  type MountRow,
  promptWithToolkit,
  pythonLabel,
  randomAgentSlug,
  slugFromDisplayName,
  TOOLKITS,
  toggle,
  toolkitToolNames,
} from "../../../lib/agent-spec";
import { type ByocPythonVersion, errorMessage, HARNESS_NATIVE_TOOLS, type HarnessNativeTool } from "../../../lib/api";
import { adotLauncher } from "../../../lib/byocTelemetry";
import {
  MCP_STDIO_ARGS_MAX,
  MCP_STDIO_BASES,
  MCP_STDIO_DEFAULT_COMMAND,
  MCP_STDIO_OFF,
  type McpStdioBase,
  type McpStdioForm,
  mcpStdioArgs,
  mcpStdioIssues,
  withBase,
} from "../../../lib/mcp-stdio";
import {
  type CatalogMountRow,
  catalogMountsSpec,
  maxMounts,
  modeAllowed,
  mountedPrefixHint,
  newCatalogRow,
  pickable,
  type StorageEntry,
  type StoragePreflight,
  storageApi,
} from "../../../lib/api/storage";
import { useModelCatalog } from "../../../lib/model-catalog";
import { bucketMountIssue, byoRowsMode, mountNeedsProfile } from "../../../lib/storage-mounts";
import { runtimeConfigSupported } from "../../../lib/runtime-config";
import { CUSTOM_MODEL_OPTION, type ModelSource, modelOptionsFor } from "../../../lib/models";
import { useAuth } from "../../../auth/auth-context";
import { HintLabel } from "../../Glossary";
import { Alert, Button, Card, Field, LinkButton, OptionCard, Segmented, Select, Spin, Tag } from "../../ui";
import { useNetworkProfiles } from "../../../lib/useNetworkProfiles";
import { NetworkSection } from "./NetworkSection";
import { StrandsToolPicker } from "./StrandsSections";
import { CheckList, type SectionProps, type WizardUi } from "./wizardKit";

/** Next slug while it still follows the display name: the ASCII-derived slug, or a
 *  random `agent-xxxxxx` kept stable while the member keeps typing. */
function followSlug(displayName: string, current: string): string {
  const derived = slugFromDisplayName(displayName);
  if (derived) return derived;
  return /^agent-[a-z0-9]{6}$/.test(current) ? current : randomAgentSlug();
}

/** Display name (free text, any language) + the resource-name slug derived from it.
 *  Editing the slug by hand stops the derivation; a re-publish locks the slug only. */
export function NameField({ form, set, err, nameLocked }: Omit<SectionProps, "cat">) {
  const { t } = useTranslation();
  const onDisplayName = (value: string) =>
    set((prev) => ({
      displayName: value,
      ...(nameLocked || prev.nameEdited ? {} : { name: value.trim() ? followSlug(value, prev.name) : "" }),
    }));
  return (
    <>
      <Field label={t("v2.agents.wizard.displayName")} hint={t("v2.agents.wizard.displayNameHint")}>
        <input
          className="v2-input"
          value={form.displayName}
          maxLength={DISPLAY_NAME_MAX}
          placeholder={t("v2.agents.wizard.displayNamePlaceholder")}
          onChange={(e) => onDisplayName(e.target.value)}
          data-testid="v2-agent-display-name"
        />
      </Field>
      <Field
        label={
          <HintLabel term="runtime">{t("v2.agents.wizard.resourceName")}</HintLabel>
        }
        required
        hint={t(nameLocked ? "v2.agents.wizard.nameLocked" : "v2.agents.wizard.nameHint")}
        error={err("name")}
      >
        <input
          className="v2-input mono"
          value={form.name}
          disabled={nameLocked}
          maxLength={48}
          placeholder="hr-assistant-v3"
          onChange={(e) => set({ name: e.target.value.toLowerCase(), nameEdited: true })}
          data-testid="v2-agent-name"
        />
      </Field>
    </>
  );
}

/** Name + system prompt (harness / Strands / other SDK). */
export function BasicCard(props: Omit<SectionProps, "cat">) {
  const { t } = useTranslation();
  const { form, set, err, promptLocked } = props;
  return (
    <Card title={t("v2.agents.basic")}>
      <div className="v2-form">
        <NameField {...props} />
        <Field
          label={t("v2.agents.wizard.systemPrompt")}
          required
          error={err("prompt")}
          hint={promptLocked ? t("v2.agents.wizard.convertedPromptLocked") : undefined}
        >
          <textarea
            className="v2-textarea"
            rows={6}
            maxLength={20000}
            readOnly={promptLocked}
            value={form.systemPrompt}
            placeholder={t("v2.agents.wizard.promptPlaceholder")}
            onChange={(e) => set({ systemPrompt: e.target.value })}
            data-testid="v2-agent-prompt"
          />
        </Field>
      </div>
    </Card>
  );
}

/** The workspace's managed COA gateway, attached by the context layer's 经 Gateway
 *  mode rather than picked here: shown checked and locked, with a way back to the
 *  card that owns the choice. */
function ManagedGatewayRow() {
  const { t } = useTranslation();
  const toContextCard = () =>
    document.querySelector('[data-testid="v2-agent-context-layer"]')?.scrollIntoView({ behavior: "smooth", block: "start" });
  return (
    <div className="v2-checks v2-agents-managed-gw" data-testid="v2-agent-gateway-managed">
      <label className="v2-check disabled" title={t("v2.agents.wizard.managedGatewayHint")}>
        <input type="checkbox" checked disabled readOnly />
        {t("v2.agents.wizard.managedGateway")}
        <Tag tone="blue">{t("v2.agents.wizard.managedGatewayTag")}</Tag>
      </label>
      <span className="v2-muted">
        {t("v2.agents.wizard.managedGatewayHint")}{" "}
        <LinkButton onClick={toContextCard} testId="v2-agent-gateway-managed-go">
          {t("v2.agents.wizard.managedGatewayGo")}
        </LinkButton>
      </span>
    </div>
  );
}

/** The gateway-target picker shared by the harness and the HTTP Strands runtime. */
function GatewayField({ form, set, cat, err }: SectionProps) {
  const { t } = useTranslation();
  const extra = form.selectedGateway
    .filter((name) => !cat.gatewayTargets.some((g) => g.name === name))
    .map((name) => ({ key: name, label: name }));
  return (
    <Field
      label={<HintLabel term="gateway">{t("v2.agents.wizard.gateways")}</HintLabel>}
      error={err("gateway")}
      hint={cat.gatewayTargets.length > 0 ? t("create.configure.gatewayWholeNote") : undefined}
    >
      {managedGatewayAttached(form) && <ManagedGatewayRow />}
      {cat.loading ? (
        <Spin />
      ) : (
        <CheckList
          items={cat.gatewayTargets}
          keyOf={(g) => g.name}
          labelOf={(g) => g.name}
          hintOf={(g) => g.attachability_reason ?? g.description}
          disabledOf={(g) => !g.attachable}
          selected={form.selectedGateway}
          onToggle={(n) => set({ selectedGateway: toggle(form.selectedGateway, n) })}
          empty={t("v2.agents.wizard.noGateways")}
          extra={extra}
          testId="v2-agent-gateways"
        />
      )}
    </Field>
  );
}

function RemoteMcpField({ form, set, cat }: SectionProps) {
  const { t } = useTranslation();
  return (
    <Field label={<HintLabel term="mcp">{t("v2.agents.wizard.remoteMcp")}</HintLabel>}>
      <CheckList
        items={cat.remoteMcp}
        keyOf={(m) => m.name}
        labelOf={(m) => m.name}
        hintOf={(m) => m.url}
        selected={form.selectedMcp}
        onToggle={(n) => set({ selectedMcp: toggle(form.selectedMcp, n) })}
        empty={t("v2.agents.wizard.noMcp")}
        testId="v2-agent-mcp"
      />
    </Field>
  );
}

/* ── managed Harness ───────────────────────────────────────────────────── */

export function HarnessToolsCard(props: SectionProps) {
  const { t } = useTranslation();
  const { form, set } = props;
  return (
    <Card title={t("v2.agents.wizard.tools")} sub={t("v2.agents.wizard.toolsSub")}>
      <div className="v2-form">
        <Field label={t("v2.agents.wizard.builtinTools")}>
          <CheckList
            items={[...BUILTIN_TOOLS]}
            keyOf={(n) => n}
            labelOf={(n) => t(`v2.agents.wizard.builtin.${n}`)}
            selected={form.tools}
            onToggle={(n) => set({ tools: toggle(form.tools, n) })}
            empty=""
            testId="v2-agent-builtins"
          />
        </Field>
        <GatewayField {...props} />
        <RemoteMcpField {...props} />
        <Field
          label={t("v2.agents.wizard.nativeTools")}
          hint={form.nativeTools.length === 0 ? t("create.nativeTools.unavailable") : t("v2.agents.wizard.nativeToolsHint")}
        >
          <CheckList
            items={[...HARNESS_NATIVE_TOOLS]}
            keyOf={(n) => n}
            labelOf={(n) => t(`v2.agents.wizard.native.${n}`)}
            selected={form.nativeTools}
            onToggle={(n) => set({ nativeTools: toggle(form.nativeTools, n as HarnessNativeTool) })}
            empty=""
            testId="v2-agent-native-tools"
          />
        </Field>
      </div>
    </Card>
  );
}

/* ── Strands (zip_runtime) ─────────────────────────────────────────────── */

export function ProtocolCard({
  form,
  set,
  onProtocol,
}: Omit<SectionProps, "cat" | "err"> & { onProtocol: (next: "http" | "a2a") => void }) {
  const { t } = useTranslation();
  const rows = form.a2aSkills;
  const patchRow = (i: number, patch: Partial<(typeof rows)[number]>) =>
    set((prev) => ({ a2aSkills: prev.a2aSkills.map((r, j) => (j === i ? { ...r, ...patch } : r)) }));
  return (
    <Card title={t("v2.agents.wizard.protocolTitle")}>
      <div className="v2-options">
        {(["http", "a2a"] as const).map((p) => (
          <OptionCard
            key={p}
            title={t(`v2.agents.wizard.protocol.${p}`)}
            desc={t(`v2.agents.wizard.protocol.${p}Desc`)}
            on={form.protocol === p}
            onClick={() => form.protocol !== p && onProtocol(p)}
            hint={p === "a2a" ? t("glossary.a2a") : undefined}
            testId={`v2-agent-protocol-${p}`}
          />
        ))}
      </div>
      {form.protocol === "a2a" && (
        <div className="v2-agents-block">
          <Alert>{t("create.configure.a2aNote")}</Alert>
          <h3 className="v2-sub-title">{t("v2.agents.wizard.a2aSkills")}</h3>
          <div className="v2-agents-rows" data-testid="v2-agent-a2a-skills">
            {rows.map((row, i) => (
              <div key={i} className="v2-agents-row a2a">
                <input
                  className="v2-input"
                  placeholder={t("create.configure.a2aSkillName")}
                  value={row.name}
                  onChange={(e) => patchRow(i, { name: e.target.value })}
                  data-testid={`v2-agent-a2a-name-${i}`}
                />
                <input
                  className="v2-input"
                  placeholder={t("create.configure.a2aSkillDesc")}
                  value={row.description}
                  onChange={(e) => patchRow(i, { description: e.target.value })}
                />
                <input
                  className="v2-input"
                  placeholder={t("create.configure.a2aSkillTags")}
                  value={row.tags}
                  onChange={(e) => patchRow(i, { tags: e.target.value })}
                />
                <RemoveButton onClick={() => set((prev) => ({ a2aSkills: prev.a2aSkills.filter((_, j) => j !== i) }))} />
              </div>
            ))}
          </div>
          <Button
            size="sm"
            onClick={() => set((prev) => ({ a2aSkills: [...prev.a2aSkills, { name: "", description: "", tags: "" }] }))}
            testId="v2-agent-a2a-add"
          >
            <Plus size={13} aria-hidden="true" />
            {t("create.configure.a2aSkillAdd")}
          </Button>
        </div>
      )}
    </Card>
  );
}

export function StrandsToolsCard(props: SectionProps & { touched?: boolean }) {
  const { t } = useTranslation();
  const { form, set } = props;
  const a2a = form.protocol === "a2a";
  const kitTools = toolkitToolNames(form.toolkits);
  const toggleKit = (kit: (typeof TOOLKITS)[number]) => {
    const on = form.toolkits.includes(kit.name);
    set((prev) =>
      on
        ? { toolkits: prev.toolkits.filter((x) => x !== kit.name) }
        : { toolkits: [...prev.toolkits, kit.name], systemPrompt: promptWithToolkit(prev.systemPrompt, kit.prompt) },
    );
  };
  return (
    <Card title={t("v2.agents.wizard.tools")} sub={t("v2.agents.wizard.toolsSub")}>
      <div className="v2-form">
        <Field label={t("v2.agents.wizard.templateTools")} hint={t("v2.agents.wizard.templateToolsHint")}>
          <div className="v2-row" data-testid="v2-agent-template-tools">
            {(kitTools.length || form.strands?.tools.length
              ? [...kitTools, ...(form.strands?.tools ?? [])]
              : ["calculator", "current_utc_time"]
            ).map((name) => (
              <Tag key={name} tone={kitTools.length || form.strands?.tools.length ? "blue" : "gray"}>
                <span className="mono">{name}</span>
              </Tag>
            ))}
          </div>
        </Field>
        <StrandsToolPicker form={form} set={set} touched={Boolean(props.touched)} />
        {a2a ? (
          <Field label={t("v2.agents.wizard.gateways")}>
            <span className="v2-muted">{t("v2.agents.wizard.gatewayA2a")}</span>
          </Field>
        ) : (
          <>
            <GatewayField {...props} />
            <Field label={t("v2.agents.wizard.toolkits")} hint={t("create.configure.toolkitNote")}>
              <div className="v2-checks" data-testid="v2-agent-toolkits">
                {TOOLKITS.map((kit) => (
                  <label key={kit.name} className="v2-check" title={t(`create.configure.toolkitDesc.${kit.name}`)}>
                    <input type="checkbox" checked={form.toolkits.includes(kit.name)} onChange={() => toggleKit(kit)} />
                    <span className="mono">{t(`create.configure.toolkitName.${kit.name}`)}</span>
                    <span className="v2-muted">· {t(`v2.agents.wizard.toolkitDesc.${kit.name}`)}</span>
                  </label>
                ))}
              </div>
            </Field>
          </>
        )}
      </div>
    </Card>
  );
}

/* ── other Agent SDK (container) ───────────────────────────────────────── */

export function SdkCard({ form, set }: Omit<SectionProps, "cat" | "err">) {
  const { t } = useTranslation();
  return (
    <Card title={t("v2.agents.wizard.sdkTitle")}>
      <div className="v2-options">
        <OptionCard
          title={t("create.configure.agentSdkClaude")}
          desc={t("v2.agents.wizard.sdkClaudeDesc")}
          on={form.agentSdk === "claude_agent_sdk"}
          onClick={() => set({ agentSdk: "claude_agent_sdk" })}
          testId="v2-agent-sdk-claude"
        />
      </div>
      <div className="v2-agents-foot">
        <Alert>{t("create.configure.agentSdkNote")}</Alert>
      </div>
    </Card>
  );
}

export function ContainerToolsCard(props: SectionProps) {
  const { t } = useTranslation();
  const { form, set } = props;
  return (
    <Card title={t("v2.agents.wizard.sdkTools")} sub={t("v2.agents.wizard.toolsSub")}>
      <div className="v2-form">
        <Field label={t("v2.agents.wizard.builtinTools")}>
          <div className="v2-row">
            <Tag tone="blue">{t("v2.agents.wizard.subagents")}</Tag>
          </div>
        </Field>
        <RemoteMcpField {...props} />
        <Field label={t("v2.agents.wizard.gateways")}>
          <span className="v2-muted">{t("v2.agents.wizard.gatewayContainer")}</span>
        </Field>
        <Field label={t("v2.agents.wizard.mcpJson")} hint={t("v2.agents.wizard.mcpJsonHint")}>
          <textarea
            className="v2-textarea code"
            rows={3}
            value={form.mcpServers}
            onChange={(e) => set({ mcpServers: e.target.value })}
            placeholder='{"docs": {"command": "uvx", "args": ["mcp-server-docs"]}}'
            data-testid="v2-agent-mcp-json"
          />
        </Field>
      </div>
    </Card>
  );
}

function RemoveButton({ onClick, testId }: { onClick: () => void; testId?: string }) {
  const { t } = useTranslation();
  return (
    <Button size="sm" onClick={onClick} title={t("v2.common.delete")} testId={testId}>
      <Trash2 size={13} aria-hidden="true" />
    </Button>
  );
}

const PREFLIGHT_DEBOUNCE_MS = 600;
const SEVERITY_TONE = { ok: "green", warn: "orange", block: "red", info: "gray" } as const;

/**
 * Mounts from the workspace's storage catalog (storage-mounts §4, §10): pick an entry of
 * the project's allowed set, its path (default `/mnt/<id>`) and its mode — read-only by
 * default, read-write only where the entry allows it. Each change re-runs the §5
 * preflight (storage × the network step's profile) and shows its findings; the deploy
 * runs it again and refuses on a blocking one. Members pick here too — the entry, not the ARN, is what they name.
 */
function CatalogMountsField({ form, set, inlineCount }: Pick<SectionProps, "set"> & { form: AgentForm; inlineCount: number }) {
  const { t } = useTranslation();
  const rows = form.catalogMounts ?? [];
  const [entries, setEntries] = useState<StorageEntry[] | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [report, setReport] = useState<StoragePreflight | null>(null);
  const [checking, setChecking] = useState(false);
  const [pick, setPick] = useState("");
  const setRows = (next: CatalogMountRow[]) => set({ catalogMounts: next });

  useEffect(() => {
    let live = true;
    storageApi
      .list()
      .then((res) => live && setEntries(res.storage))
      .catch((e: unknown) => {
        if (!live) return;
        setEntries([]);
        setLoadError(errorMessage(e));
      });
    return () => {
      live = false;
    };
  }, []);

  const platform = runtimeConfigSupported(form.method) ? form.runtimeConfig?.platform_version ?? null : null;
  const key = JSON.stringify([rows, form.networkProfileId, form.method, platform]);
  useEffect(() => {
    if (rows.length === 0) {
      setReport(null);
      return;
    }
    let live = true;
    const timer = window.setTimeout(() => {
      setChecking(true);
      storageApi
        .preflight({
          mounts: catalogMountsSpec(rows), network_profile_id: form.networkProfileId || null, method: form.method,
          ...(platform ? { platform_version: platform } : {}),
        })
        .then((res) => live && setReport(res))
        .catch((e: unknown) => live && setReport({ status: "blocked", results: [{ check: "preflight", severity: "block", message: errorMessage(e) }] }))
        .finally(() => live && setChecking(false));
    }, PREFLIGHT_DEBOUNCE_MS);
    return () => {
      live = false;
      window.clearTimeout(timer);
    };
    // the serialized inputs are the dependency
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key]);

  const byId = new Map((entries ?? []).map((e) => [e.id, e]));
  // an S3 bucket mount: Runtime methods only (the Harness drops it), and platform V2
  const harness = form.method === "harness";
  const options = pickable(entries ?? [], rows).map((e) => ({
    value: e.id,
    label: `${e.name} (${e.id})${e.kind === "s3bucket" ? ` · ${t("v3.storage.kind.s3bucket")}` : ""}`,
    disabled: harness && e.kind === "s3bucket",
  }));
  const limit = maxMounts(form.sessionFs);
  const full = rows.length + inlineCount >= limit;
  const patch = (i: number, change: Partial<CatalogMountRow>) => setRows(rows.map((r, j) => (j === i ? { ...r, ...change } : r)));
  return (
    <Field label={t("v3.storage.picker.title")} hint={t("v3.storage.picker.hint", { max: limit })}>
      {entries === null ? <Spin /> : (
        <div className="v2-agents-rows" data-testid="v2-agent-fs-catalog">
          {loadError && <Alert tone="error">{loadError}</Alert>}
          {rows.map((row, i) => {
            const entry = byId.get(row.storageId);
            return (
              <div key={row.storageId} className="v2-agents-row mount">
                <span className="mono" title={entry?.kind === "s3bucket" ? mountedPrefixHint(entry) : entry?.access_point_arn}>
                  {entry ? `${entry.name} (${row.storageId})` : row.storageId}
                  {entry?.kind === "s3bucket" && entry.scope && (
                    <small> · {t(`v3.storage.scope.${entry.scope}`)}</small>
                  )}
                </span>
                <input
                  className="v2-input mono"
                  value={row.path}
                  aria-label={t("v2.agents.wizard.fsPath")}
                  onChange={(e) => patch(i, { path: e.target.value })}
                  data-testid={`v2-agent-fs-catalog-path-${i}`}
                />
                <Select
                  value={row.mode}
                  ariaLabel={t("v3.storage.detail.mode")}
                  options={[
                    { value: "ro", label: t("v3.storage.mode.ro") },
                    ...(entry?.max_mode === "rw" || row.mode === "rw" ? [{ value: "rw", label: t("v3.storage.mode.rw") }] : []),
                  ]}
                  onChange={(v) => patch(i, { mode: v === "rw" ? "rw" : "ro" })}
                  testId={`v2-agent-fs-catalog-mode-${i}`}
                />
                <RemoveButton onClick={() => setRows(rows.filter((_, j) => j !== i))} />
                {entry && !modeAllowed(row, entry) && <span className="v2-agents-err">{t("v3.storage.picker.modeTooWide")}</span>}
                {(() => {
                  const issue = bucketMountIssue(entry?.kind, form.method, form.runtimeConfig);
                  return issue && <span className="v2-agents-err" data-testid={`v2-agent-fs-catalog-${issue}-${i}`}>{t(`v3.storage.picker.${issue}`)}</span>;
                })()}
              </div>
            );
          })}
          {options.length > 0 && !full ? (
            <div style={{ display: "flex", gap: 8 }}>
              <Select value={pick} options={options} placeholder={t("v3.storage.picker.choose")} onChange={setPick} testId="v2-agent-fs-catalog-pick" />
              <Button size="sm" disabled={!pick} onClick={() => { setRows([...rows, newCatalogRow(pick, byId.get(pick)?.kind)]); setPick(""); }} testId="v2-agent-fs-catalog-add">
                <Plus size={13} aria-hidden="true" />
                {t("v3.storage.picker.add")}
              </Button>
            </div>
          ) : (entries.length === 0 && <div className="v2-muted">{t("v3.storage.picker.none")}</div>)}
          {checking && <Spin />}
          {report && report.results.filter((r) => r.severity !== "ok").length > 0 && (
            <div data-testid="v2-agent-fs-catalog-preflight" style={{ display: "grid", gap: 4 }}>
              {report.results.filter((r) => r.severity !== "ok").map((r, i) => (
                <div key={`${r.check}-${i}`}>
                  <Tag tone={SEVERITY_TONE[r.severity] ?? "gray"}>{r.storage_id ?? r.check}</Tag> <span>{r.message}</span>
                </div>
              ))}
            </div>
          )}
          {report?.status === "ok" && <div className="v2-muted">{t("v3.storage.picker.preflightOk")}</div>}
        </div>
      )}
    </Field>
  );
}

/**
 * The storage card: managed session storage, and the BYO S3 Files / EFS mounts of the
 * methods that take them (storage-mounts §3). The BYO rows are inline access point
 * ARNs that only an administrator edits (D-2); a member sees an administrator's rows
 * read-only. A mount runs in the agent's network profile (the network step); only a
 * container agent may still carry the legacy inline subnets / security groups.
 * `network={false}` leaves the network step out, for a method whose own card has it
 * (BYOC's artifact card).
 */
export function FilesystemCard({
  form,
  set,
  touched,
  network = true,
}: Omit<SectionProps, "cat" | "err"> & { touched: boolean; network?: boolean }) {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const issues = filesystemIssues(form);
  const byoAllowed = byoMountsAllowed(form.method);
  const byo = byoAllowed && hasByoMounts(form);
  const rowsMode = byoAllowed ? byoRowsMode(form.method, isAdmin, byo) : "hidden";
  const editable = rowsMode === "edit";
  const { profiles } = useNetworkProfiles();
  const needsProfile = mountNeedsProfile({ method: form.method, networkProfileId: form.networkProfileId, hasByo: byoAllowed && hasNfsMounts(form) });
  const show = (on: boolean, key: string) => (touched && on ? t(key) : undefined);
  const kinds = [
    { kind: "s3" as const, field: "s3Mounts" as const, label: "S3 Files", ph: "create.configure.fsS3ArnPlaceholder" },
    { kind: "efs" as const, field: "efsMounts" as const, label: "EFS", ph: "create.configure.fsEfsArnPlaceholder" },
  ];
  const patchRow = (field: "s3Mounts" | "efsMounts", i: number, patch: Partial<MountRow>) =>
    set((prev) => ({ [field]: prev[field].map((r, j) => (j === i ? { ...r, ...patch } : r)) }));
  return (
    <>
    <Card
      title={t("v2.agents.wizard.fsTitle")}
      sub={t(byo ? "storage.mount.noteByo" : byoAllowed ? "storage.mount.note" : "v2.agents.wizard.fsNoteSession")}
      testId="v2-agent-fs"
    >
      <div className="v2-form">
        <div className="v2-form cols-2">
          <Field label={t("v2.agents.wizard.fsSession")}>
            <label className="v2-check">
              <input type="checkbox" checked={form.sessionFs} onChange={(e) => set({ sessionFs: e.target.checked })} data-testid="v2-agent-fs-session" />
              {t("v2.agents.wizard.fsSessionOn")}
            </label>
          </Field>
          {form.sessionFs && (
            <Field label={t("v2.agents.wizard.fsSessionMount")} error={show(issues.sessionMount, "v2.agents.wizard.errMount")}>
              <input
                className="v2-input mono"
                value={form.sessionMount}
                onChange={(e) => set({ sessionMount: e.target.value })}
                placeholder={DEFAULT_SESSION_MOUNT}
                data-testid="v2-agent-fs-session-mount"
              />
            </Field>
          )}
        </div>
        {rowsMode !== "hidden" && kinds.map(({ kind, field, label, ph }) => (
          <Field key={kind} label={t("v2.agents.wizard.fsMounts", { kind: label })} hint={t("v2.agents.wizard.fsMountsHint", { max: MAX_MOUNTS_PER_KIND })}>
            {form[field].length > 0 && (
              <div className="v2-agents-rows">
                {form[field].map((row, i) => {
                  const bad = issues.rows[`${kind}:${i}`];
                  return (
                    <div key={i}>
                      <div className="v2-agents-row mount">
                        <input
                          className="v2-input mono"
                          value={row.arn}
                          placeholder={t(ph)}
                          aria-label={t("v2.agents.wizard.fsArn")}
                          readOnly={!editable}
                          onChange={(e) => patchRow(field, i, { arn: e.target.value })}
                          data-testid={`v2-agent-fs-${kind}-arn-${i}`}
                        />
                        <input
                          className="v2-input mono"
                          value={row.path}
                          placeholder="/mnt/data"
                          aria-label={t("v2.agents.wizard.fsPath")}
                          readOnly={!editable}
                          onChange={(e) => patchRow(field, i, { path: e.target.value })}
                          data-testid={`v2-agent-fs-${kind}-path-${i}`}
                        />
                        {/* removing is allowed to a member too: it adds no access */}
                        <RemoveButton onClick={() => set((prev) => ({ [field]: prev[field].filter((_, j) => j !== i) }))} />
                      </div>
                      {touched && bad && (
                        <span className="v2-agents-err">
                          {[bad.arn && t("v2.agents.wizard.errArn"), bad.path && t("v2.agents.wizard.errMount")].filter(Boolean).join(" · ")}
                        </span>
                      )}
                    </div>
                  );
                })}
              </div>
            )}
            {editable && (
              <div>
                <Button
                  size="sm"
                  disabled={form[field].length >= MAX_MOUNTS_PER_KIND}
                  onClick={() => set((prev) => ({ [field]: [...prev[field], { arn: "", path: "" }] }))}
                  testId={`v2-agent-fs-add-${kind}`}
                >
                  <Plus size={13} aria-hidden="true" />
                  {t("v2.agents.wizard.fsAdd", { kind: label })}
                </Button>
              </div>
            )}
          </Field>
        ))}
        {rowsMode === "readonly" && (
          <div className="v2-muted" data-testid="v2-agent-fs-admin-only">
            {t("storage.mount.adminOnly")}
          </div>
        )}
        {byoAllowed && !isAdmin && !byo && (
          <div className="v2-muted" data-testid="v2-agent-fs-ask-admin">
            {t("storage.mount.askAdmin")}
          </div>
        )}
        {byoAllowed && (
          <CatalogMountsField form={form} set={set} inlineCount={form.s3Mounts.length + form.efsMounts.length} />
        )}
        {touched && issues.duplicatePaths && <span className="v2-agents-err">{t("v2.agents.wizard.errDupPath")}</span>}
        {issues.tooMany && <span className="v2-agents-err" data-testid="v2-agent-fs-too-many">{t("v2.agents.wizard.errTooManyMounts", { max: maxMounts(form.sessionFs) })}</span>}
        {needsProfile && (
          <div data-testid="v2-agent-fs-needs-profile">
            <Alert tone={touched ? "error" : "warn"}>{t("storage.mount.needsProfile")}</Alert>
          </div>
        )}
        {form.networkProfileId ? (
          // a stored profile reference: the profile's ids live on the profile, not here
          // its own test id: the network card's profile picker owns v2-agent-network-profile
          <div className="v2-muted" data-testid="v2-agent-fs-network-profile">
            {t("v2.agents.wizard.networkProfile", {
              id: profiles?.find((p) => p.id === form.networkProfileId)?.name ?? form.networkProfileId,
            })}
          </div>
        ) : byo && form.method === "container" && (
          // the legacy inline network a container agent's mounts predate profiles with
          <div className="v2-form cols-2">
            <Field label={t("create.configure.fsSubnets")} required error={show(issues.vpc && !form.vpcSubnets.trim(), "v2.agents.wizard.errVpc")}>
              <input
                className="v2-input mono"
                value={form.vpcSubnets}
                readOnly={!editable}
                onChange={(e) => set({ vpcSubnets: e.target.value })}
                placeholder="subnet-0abc, subnet-0def"
                data-testid="v2-agent-fs-subnets"
              />
            </Field>
            <Field label={t("create.configure.fsSgs")} required error={show(issues.vpc && !form.vpcSgs.trim(), "v2.agents.wizard.errVpc")}>
              <input
                className="v2-input mono"
                value={form.vpcSgs}
                readOnly={!editable}
                onChange={(e) => set({ vpcSgs: e.target.value })}
                placeholder="sg-0abc"
                data-testid="v2-agent-fs-sgs"
              />
            </Field>
          </div>
        )}
      </div>
    </Card>
    {/* the network step (projects §12.6) — harness, Strands / A2A and container */}
    {network && <NetworkSection form={form} set={set} />}
    </>
  );
}

/* ── bring your own code ───────────────────────────────────────────────── */

export function ByocBasicCard(props: Omit<SectionProps, "cat">) {
  const { t } = useTranslation();
  const { form, set } = props;
  return (
    <Card title={t("v2.agents.basic")}>
      <div className="v2-form cols-2">
        <NameField {...props} />
        <Field label={t("create.configure.byocDescription")} hint={t("v2.agents.wizard.byocDescriptionHint")}>
          <input
            className="v2-input"
            value={form.byocDescription}
            onChange={(e) => set({ byocDescription: e.target.value })}
            placeholder={t("create.configure.byocDescriptionPlaceholder")}
            data-testid="v2-agent-byoc-desc"
          />
        </Field>
      </div>
    </Card>
  );
}

/** byoc + protocol "mcp": where the server's tools appear on the Gateway, and
 *  (code zip) whether it is a stdio-only server run behind the platform bridge. */
export function McpServerCard({ form, set, err }: Omit<SectionProps, "cat">) {
  const { t } = useTranslation();
  const target = form.mcpTargetName.trim();
  // a form without it (older drafts) reads as off
  const stdio = form.mcpStdio ?? MCP_STDIO_OFF;
  const setStdio = (next: McpStdioForm) => set({ mcpStdio: next });
  const zip = form.byocKind === "code_zip";
  const issues = mcpStdioIssues(stdio, form.byocKind);
  const argv = [stdio.command.trim(), ...mcpStdioArgs(stdio.args)].filter(Boolean).join(" ");
  return (
    <Card title={t("v2.agents.wizard.mcp.cardTitle")} sub={t("v2.agents.wizard.mcp.cardSub")} testId="v2-agent-mcp">
      <div className="v2-form">
        <Field
          label={t("v2.agents.wizard.mcp.targetName")}
          required
          error={err("mcpTarget")}
          hint={t("v2.agents.wizard.mcp.targetHint", { example: `${target || "crm-lookup"}___find_order` })}
        >
          <input
            className="v2-input mono"
            value={form.mcpTargetName}
            maxLength={100}
            placeholder="crm-lookup"
            onChange={(e) => set({ mcpTargetName: e.target.value })}
            data-testid="v2-agent-mcp-target"
          />
        </Field>
        <Field label={t("mcpServer.stdio.label")} hint={t(zip ? "mcpServer.stdio.hint" : "mcpServer.stdio.zipOnly")}>
          <label className="v2-check">
            <input
              type="checkbox"
              checked={zip && stdio.on}
              disabled={!zip}
              onChange={(e) => setStdio({ ...stdio, on: e.target.checked })}
              data-testid="v2-agent-mcp-stdio"
            />
            {t("mcpServer.stdio.on")}
          </label>
        </Field>
        {zip && stdio.on && (
          <>
            <Field label={t("mcpServer.stdio.base")} hint={t(`mcpServer.stdio.baseHint.${stdio.base}`)}>
              <Segmented<McpStdioBase>
                value={stdio.base}
                ariaLabel={t("mcpServer.stdio.base")}
                options={MCP_STDIO_BASES.map((b) => ({ value: b, label: t(`mcpServer.stdio.baseName.${b}`) }))}
                onChange={(b) => setStdio(withBase(stdio, b))}
              />
            </Field>
            <div className="v2-form cols-2">
              <Field
                label={t("mcpServer.stdio.command")}
                required
                hint={t("mcpServer.stdio.commandHint")}
                error={err("mcpStdio") ?? (issues.command && stdio.command.trim() ? t("mcpServer.stdio.errCommand") : undefined)}
              >
                <input
                  className="v2-input mono"
                  value={stdio.command}
                  maxLength={255}
                  placeholder={MCP_STDIO_DEFAULT_COMMAND[stdio.base]}
                  onChange={(e) => setStdio({ ...stdio, command: e.target.value })}
                  data-testid="v2-agent-mcp-stdio-command"
                />
              </Field>
              <Field
                label={t("mcpServer.stdio.args")}
                hint={t("mcpServer.stdio.argsHint", { max: MCP_STDIO_ARGS_MAX })}
                error={issues.args ? t("mcpServer.stdio.errArgs", { max: MCP_STDIO_ARGS_MAX }) : undefined}
              >
                <textarea
                  className="v2-textarea code"
                  rows={3}
                  value={stdio.args}
                  placeholder={stdio.base === "node" ? "build/index.js" : "server.py"}
                  onChange={(e) => setStdio({ ...stdio, args: e.target.value })}
                  data-testid="v2-agent-mcp-stdio-args"
                />
              </Field>
            </div>
            {argv && (
              <span className="v2-muted">
                {t("mcpServer.stdio.preview")} <span className="mono">{argv}</span>
              </span>
            )}
            <Alert tone="warn">{t("mcpServer.stdio.limits")}</Alert>
          </>
        )}
        <Alert>{t("v2.agents.wizard.mcp.wiring")}</Alert>
        {/* §15.10: the server may sit in a VPC profile (the network step below, shared
            with every BYOC artifact); it changes the server's outbound path only */}
        <Field label={t("storage.mount.mcp.network")}>
          <div className="v2-muted" data-testid="v2-agent-mcp-network">
            {form.networkProfileId
              ? t("storage.mount.mcp.vpc", { id: form.networkProfileId })
              : t("storage.mount.mcp.public")}
          </div>
        </Field>
      </div>
    </Card>
  );
}

export function ByocArtifactCard({
  form,
  set,
  err,
  ui,
  onUpload,
  onPython,
}: Omit<SectionProps, "cat"> & {
  ui: WizardUi;
  onUpload: (file: File) => void;
  onPython: (version: ByocPythonVersion) => void;
}) {
  const { t } = useTranslation();
  const kind = form.byocKind;
  const upload = ui.byocUpload;
  const reqs = upload?.detected.requirements;
  const candidates = upload?.detected.entrypoint_candidates ?? [];
  const stdioBridge = form.protocol === "mcp" && !!form.mcpStdio?.on;
  return (
    <>
    <Card title={t("v2.agents.wizard.artifactTitle")}>
      <div className="v2-options">
        {BYOC_KINDS.map((k) => (
          <OptionCard
            key={k}
            title={t(`create.configure.byocKindName.${k}`)}
            desc={t(`create.configure.byocKindDesc.${k}`)}
            on={kind === k}
            onClick={() => set({ byocKind: k })}
            testId={`v2-agent-byoc-kind-${k}`}
          />
        ))}
      </div>
      <div className="v2-form v2-agents-block">
        {kind === "container_image" ? (
          <Field label={t("create.configure.byocImageUri")} required hint={t("create.configure.byocImageHint")} error={err("byocImage")}>
            <input
              className="v2-input mono"
              value={form.byocImageUri}
              onChange={(e) => set({ byocImageUri: e.target.value })}
              placeholder="123456789012.dkr.ecr.us-west-2.amazonaws.com/my-agents:v1"
              data-testid="v2-agent-byoc-image"
            />
          </Field>
        ) : (
          <Field label={t("create.configure.byocUpload")} required error={err("byocUpload")}>
            <label
              className="v2-agents-drop"
              data-testid="v2-agent-byoc-drop"
              onDragOver={(e) => e.preventDefault()}
              onDrop={(e) => {
                e.preventDefault();
                const file = Array.from(e.dataTransfer.files).find((f) => f.name.toLowerCase().endsWith(".zip"));
                if (file) onUpload(file);
              }}
            >
              <Upload size={20} aria-hidden="true" />
              {ui.byocUploading ? (
                <span>{t("create.configure.byocUploading")}</span>
              ) : upload ? (
                <>
                  <span className="mono" data-testid="v2-agent-byoc-uploaded">
                    {upload.original_filename}
                  </span>
                  <span className="v2-muted">
                    {t("v2.agents.wizard.byocUploaded", {
                      size: (upload.size_bytes / 1e6).toFixed(1),
                      sha: upload.sha256.slice(0, 12),
                      count: upload.entries_count,
                    })}
                  </span>
                  <span className="v2-muted">{t("v2.agents.wizard.byocReplace")}</span>
                </>
              ) : (
                <span>{t("create.configure.byocDrop")}</span>
              )}
              <input
                type="file"
                accept=".zip"
                disabled={ui.byocUploading}
                onChange={(e) => {
                  const file = e.target.files?.[0];
                  e.target.value = "";
                  if (file) onUpload(file);
                }}
                data-testid="v2-agent-byoc-file"
              />
            </label>
            {upload && kind === "code_zip" && !upload.detected.agentcore_sdk_detected && (
              <Alert tone="warn">{t("create.configure.byocNoSdkWarn")}</Alert>
            )}
            {upload && kind === "code_zip" && (
              <div data-testid="v2-agent-byoc-telemetry">
                {adotLauncher(upload.detected, form.byocInstallReqs) ? (
                  <Alert tone="success">{t("byocTelemetry.adotOn")}</Alert>
                ) : (
                  <Alert tone="warn">{t("byocTelemetry.noAdotWarn")}</Alert>
                )}
              </div>
            )}
            {upload && kind === "container_source" && !upload.detected.has_dockerfile && (
              <Alert tone="warn">{t("create.configure.byocNoDockerfileWarn")}</Alert>
            )}
            {upload && kind === "code_zip" && form.byocInstallReqs && reqs && reqs.status !== "skipped" &&
              (reqs.status === "ok" ? (
                <Alert tone="success">{t("create.configure.byocReqsOk", { count: reqs.package_count ?? 0 })}</Alert>
              ) : (
                <Alert tone="error">
                  {t("create.configure.byocReqsFailed")} <span className="mono">{reqs.error}</span>
                </Alert>
              ))}
          </Field>
        )}
        {kind === "code_zip" && (
          <div className="v2-form cols-2">
            {/* a stdio MCP server: the platform bridge is the entrypoint */}
            {!stdioBridge && (
            <Field label={t("create.configure.byocEntrypoint")} required error={err("byocEntrypoint")}>
              {candidates.length > 0 ? (
                <Select
                  mono
                  value={form.byocEntrypoint}
                  onChange={(v) => set({ byocEntrypoint: v })}
                  testId="v2-agent-byoc-entrypoint"
                  options={[...candidates, ...(candidates.includes(form.byocEntrypoint) ? [] : [form.byocEntrypoint])].map((c) => ({
                    value: c,
                    label: c,
                  }))}
                />
              ) : (
                <input
                  className="v2-input mono"
                  value={form.byocEntrypoint}
                  onChange={(e) => set({ byocEntrypoint: e.target.value })}
                  placeholder="main.py"
                  data-testid="v2-agent-byoc-entrypoint"
                />
              )}
            </Field>
            )}
            <Field label={t("create.configure.byocPython")}>
              <Select
                value={form.byocPython}
                onChange={(v) => onPython(v as ByocPythonVersion)}
                testId="v2-agent-byoc-python"
                options={BYOC_PYTHON_VERSIONS.map((v) => ({ value: v, label: pythonLabel(v) }))}
              />
            </Field>
            <Field label={t("v2.agents.wizard.byocBuild")}>
              <label className="v2-check">
                <input
                  type="checkbox"
                  checked={form.byocInstallReqs}
                  onChange={(e) => set({ byocInstallReqs: e.target.checked })}
                  data-testid="v2-agent-byoc-reqs"
                />
                {t("create.configure.byocInstallReqs")}
              </label>
            </Field>
            {form.protocol !== "mcp" && (
              <Field label={t("v2.agents.wizard.byocContractKind")} hint={t("create.configure.byocRawHint")}>
                <label className="v2-check">
                  <input
                    type="checkbox"
                    checked={form.byocRawContract}
                    onChange={(e) => set({ byocRawContract: e.target.checked })}
                    data-testid="v2-agent-byoc-raw"
                  />
                  {t("create.configure.byocRaw")}
                </label>
              </Field>
            )}
          </div>
        )}
      </div>
    </Card>
    {/* the network step (projects §12.6) — both BYOC artifact kinds, agents and MCP servers */}
    <NetworkSection form={form} set={set} />
    </>
  );
}

export function ByocModelsCard({
  form,
  set,
  err,
  applySource,
}: Omit<SectionProps, "cat"> & { applySource: (source: ModelSource) => void }) {
  const { t } = useTranslation();
  const [customOpen, setCustomOpen] = useState(false);
  const [draft, setDraft] = useState("");
  useModelCatalog(); // the account catalog joins the offered models once it lands
  const models = form.byocModels;
  const full = models.length >= BYOC_MODELS_MAX;
  const add = (id: string) => set((prev) => ({ byocModels: prev.byocModels.includes(id) ? prev.byocModels : [...prev.byocModels, id] }));
  return (
    <Card title={t("create.configure.byocModels")}>
      <Alert>{t("create.configure.byocModelsHint")}</Alert>
      <div className="v2-form">
        <Field
          label={t("v2.agents.wizard.modelSource")}
          hint={t(form.modelSource === "mantle" ? "create.configure.modelSourceMantleDesc" : "create.configure.modelSourceBedrockDesc")}
        >
          <div className="v2-row">
<Segmented
            value={form.modelSource}
            options={(["bedrock", "mantle"] as ModelSource[]).map((s) => ({ value: s, label: t(`v2.agents.wizard.source.${s}`) }))}
            onChange={(s) => {
              if (s === form.modelSource) return;
              applySource(s);
              setCustomOpen(false);
              setDraft("");
            }}
          />
</div>
        </Field>
        <Field label={t("v2.agents.wizard.byocModelList")} required error={err("byocModels")} hint={t("v2.agents.wizard.byocModelsMax", { max: BYOC_MODELS_MAX })}>
          <div className="v2-agents-models" data-testid="v2-agent-byoc-models">
            {models.map((model, i) => (
              <div key={`${model}-${i}`} className="v2-agents-model" data-testid="v2-agent-byoc-model-row">
                <span className="mono">{model}</span>
                {i === 0 ? (
                  <Tag tone="blue">{t("create.configure.byocModelPrimary")}</Tag>
                ) : (
                  <LinkButton
                    onClick={() => set((prev) => ({ byocModels: [prev.byocModels[i], ...prev.byocModels.filter((_, j) => j !== i)] }))}
                    testId={`v2-agent-byoc-primary-${i}`}
                  >
                    {t("create.configure.byocModelMakePrimary")}
                  </LinkButton>
                )}
                <span className="end">
                  {models.length > 1 && (
                    <LinkButton danger onClick={() => set((prev) => ({ byocModels: prev.byocModels.filter((_, j) => j !== i) }))}>
                      {t("v2.common.delete")}
                    </LinkButton>
                  )}
                </span>
              </div>
            ))}
          </div>
          <Select
            disabled={full}
            value=""
            onChange={(v) => {
              const picked = v;
              if (!picked) return;
              if (picked === CUSTOM_MODEL_OPTION) {
                setCustomOpen(true);
                return;
              }
              setCustomOpen(false);
              add(picked);
            }}
            testId="v2-agent-byoc-model-add"
            placeholder={t("create.configure.byocModelAdd")}
            options={[
              ...modelOptionsFor(form.modelSource)
                .filter((o) => !models.includes(o.model_id))
                .map((o) => ({ value: o.model_id, label: `${o.label} · ${o.model_id}` })),
              { value: CUSTOM_MODEL_OPTION, label: t("v2.agents.wizard.customModel") },
            ]}
          />
          {customOpen && (
            <div className="v2-row v2-agents-nowrap">
              <input
                className="v2-input mono"
                value={draft}
                onChange={(e) => setDraft(e.target.value)}
                placeholder={t("create.configure.modelCustomPlaceholder")}
                data-testid="v2-agent-byoc-model-custom"
              />
              <Button
                disabled={!draft.trim() || full}
                onClick={() => {
                  const id = draft.trim();
                  if (!id || full) return;
                  add(id);
                  setDraft("");
                  setCustomOpen(false);
                }}
                testId="v2-agent-byoc-model-custom-add"
              >
                {t("create.configure.byocModelAddCustom")}
              </Button>
            </div>
          )}
        </Field>
      </div>
    </Card>
  );
}

/** byoc builtin IAM declarations: the role gets the session actions, the member's code opens the session. */
export function ByocBuiltinsCard({ form, set }: Omit<SectionProps, "cat" | "err">) {
  const { t } = useTranslation();
  return (
    <Card title={t("v2.agents.wizard.byocBuiltinsTitle")} sub={t("v2.agents.wizard.byocBuiltinsSub")} testId="v2-agent-byoc-builtins-card">
      <CheckList
        items={[...BYOC_BUILTINS]}
        keyOf={(n) => n}
        labelOf={(n) => t(byocBuiltinLabelKey(n))}
        selected={form.byocBuiltins}
        onToggle={(n) => set((prev) => ({ byocBuiltins: toggle(prev.byocBuiltins, n as ByocBuiltin) }))}
        empty=""
        testId="v2-agent-byoc-builtins"
      />
      <div className="v2-agents-foot">
        <Alert>{t("v2.agents.wizard.byocBuiltinsNote")}</Alert>
      </div>
    </Card>
  );
}

export function ByocEnvCard({ form, set }: Omit<SectionProps, "cat" | "err">) {
  const { t } = useTranslation();
  const [contractOpen, setContractOpen] = useState(false);
  const patchRow = (i: number, patch: Partial<{ key: string; value: string }>) =>
    set((prev) => ({ byocEnvRows: prev.byocEnvRows.map((r, j) => (j === i ? { ...r, ...patch } : r)) }));
  return (
    <Card
      title={t("create.configure.byocEnv")}
      sub={t("v2.agents.wizard.byocEnvSub")}
      end={
        <LinkButton onClick={() => setContractOpen((v) => !v)} testId="v2-agent-byoc-contract-toggle">
          {t(contractOpen ? "v2.agents.wizard.contractHide" : "v2.agents.wizard.contractShow")}
        </LinkButton>
      }
    >
      {contractOpen && (
        <div className="v2-agents-contract" data-testid="v2-agent-byoc-contract">
          <h3 className="v2-sub-title">
            {t(form.protocol === "mcp" ? "v2.agents.wizard.mcp.contract" : "create.configure.byocContract")}
          </h3>
          <pre className="v2-pre">
            {t(form.protocol === "mcp" ? "v2.agents.wizard.mcp.contractBody" : "create.configure.byocContractBody")}
          </pre>
        </div>
      )}
      {form.byocEnvRows.length === 0 ? (
        <p className="v2-muted">{t("v2.agents.wizard.envEmpty")}</p>
      ) : (
        <div className="v2-agents-rows" data-testid="v2-agent-byoc-env">
          {form.byocEnvRows.map((row, i) => (
            <div key={i} className="v2-agents-row env">
              <input
                className="v2-input mono"
                value={row.key}
                placeholder="MODEL_ID"
                aria-label={t("create.configure.byocEnvKey")}
                onChange={(e) => patchRow(i, { key: e.target.value })}
                data-testid={`v2-agent-byoc-env-key-${i}`}
              />
              <input
                className="v2-input mono"
                value={row.value}
                aria-label={t("create.configure.byocEnvValue")}
                onChange={(e) => patchRow(i, { value: e.target.value })}
                data-testid={`v2-agent-byoc-env-value-${i}`}
              />
              <RemoveButton onClick={() => set((prev) => ({ byocEnvRows: prev.byocEnvRows.filter((_, j) => j !== i) }))} />
            </div>
          ))}
        </div>
      )}
      <Button size="sm" onClick={() => set((prev) => ({ byocEnvRows: [...prev.byocEnvRows, { key: "", value: "" }] }))} testId="v2-agent-byoc-env-add">
        <Plus size={13} aria-hidden="true" />
        {t("create.configure.byocEnvAdd")}
      </Button>
    </Card>
  );
}
