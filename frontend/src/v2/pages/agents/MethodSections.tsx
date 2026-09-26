import { Plus, Trash2, Upload } from "lucide-react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import {
  BUILTIN_TOOLS,
  BYOC_KINDS,
  BYOC_MODELS_MAX,
  BYOC_PYTHON_VERSIONS,
  DEFAULT_SESSION_MOUNT,
  filesystemIssues,
  hasByoMounts,
  MAX_MOUNTS_PER_KIND,
  type MountRow,
  promptWithToolkit,
  pythonLabel,
  TOOLKITS,
  toggle,
  toolkitToolNames,
} from "../../../lib/agent-spec";
import { type ByocPythonVersion, HARNESS_NATIVE_TOOLS, type HarnessNativeTool } from "../../../lib/api";
import { CUSTOM_MODEL_OPTION, type ModelSource, modelOptionsFor } from "../../../lib/models";
import { Alert, Button, Card, Field, LinkButton, OptionCard, Segmented, Spin, Tag } from "../../ui";
import { CheckList, type SectionProps, type WizardUi } from "./wizardKit";

/** The name field every method starts with. */
export function NameField({ form, set, err }: Omit<SectionProps, "cat">) {
  const { t } = useTranslation();
  return (
    <Field label={t("v2.agents.colName")} required hint={t("v2.agents.wizard.nameHint")} error={err("name")}>
      <input
        className="v2-input"
        value={form.name}
        maxLength={48}
        placeholder="hr-assistant-v3"
        onChange={(e) => set({ name: e.target.value.toLowerCase() })}
        data-testid="v2-agent-name"
      />
    </Field>
  );
}

/** Name + system prompt (harness / Strands / other SDK). */
export function BasicCard(props: Omit<SectionProps, "cat">) {
  const { t } = useTranslation();
  const { form, set, err } = props;
  return (
    <Card title={t("v2.agents.basic")}>
      <div className="v2-form">
        <NameField {...props} />
        <Field label={t("v2.agents.wizard.systemPrompt")} required error={err("prompt")}>
          <textarea
            className="v2-textarea"
            rows={6}
            maxLength={20000}
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

/** The gateway-target picker shared by the harness and the HTTP Strands runtime. */
function GatewayField({ form, set, cat, err }: SectionProps) {
  const { t } = useTranslation();
  const extra = form.selectedGateway
    .filter((name) => !cat.gatewayTargets.some((g) => g.name === name))
    .map((name) => ({ key: name, label: name }));
  return (
    <Field
      label={t("v2.agents.wizard.gateways")}
      error={err("gateway")}
      hint={cat.gatewayTargets.length > 0 ? t("create.configure.gatewayWholeNote") : undefined}
    >
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
    <Field label={t("v2.agents.wizard.remoteMcp")}>
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

export function StrandsToolsCard(props: SectionProps) {
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
            {(kitTools.length ? kitTools : ["calculator", "current_utc_time"]).map((name) => (
              <Tag key={name} tone={kitTools.length ? "blue" : "gray"}>
                <span className="mono">{name}</span>
              </Tag>
            ))}
          </div>
        </Field>
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

export function FilesystemCard({ form, set, touched }: Omit<SectionProps, "cat" | "err"> & { touched: boolean }) {
  const { t } = useTranslation();
  const issues = filesystemIssues(form);
  const byo = hasByoMounts(form);
  const show = (on: boolean, key: string) => (touched && on ? t(key) : undefined);
  const kinds = [
    { kind: "s3" as const, field: "s3Mounts" as const, label: "S3 Files", ph: "create.configure.fsS3ArnPlaceholder" },
    { kind: "efs" as const, field: "efsMounts" as const, label: "EFS", ph: "create.configure.fsEfsArnPlaceholder" },
  ];
  const patchRow = (field: "s3Mounts" | "efsMounts", i: number, patch: Partial<MountRow>) =>
    set((prev) => ({ [field]: prev[field].map((r, j) => (j === i ? { ...r, ...patch } : r)) }));
  return (
    <Card title={t("v2.agents.wizard.fsTitle")} sub={t(byo ? "create.configure.fsNoteByo" : "create.configure.fsNote")} testId="v2-agent-fs">
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
        {kinds.map(({ kind, field, label, ph }) => (
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
                          onChange={(e) => patchRow(field, i, { arn: e.target.value })}
                          data-testid={`v2-agent-fs-${kind}-arn-${i}`}
                        />
                        <input
                          className="v2-input mono"
                          value={row.path}
                          placeholder="/mnt/data"
                          aria-label={t("v2.agents.wizard.fsPath")}
                          onChange={(e) => patchRow(field, i, { path: e.target.value })}
                          data-testid={`v2-agent-fs-${kind}-path-${i}`}
                        />
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
          </Field>
        ))}
        {touched && issues.duplicatePaths && <span className="v2-agents-err">{t("v2.agents.wizard.errDupPath")}</span>}
        {byo && (
          <div className="v2-form cols-2">
            <Field label={t("create.configure.fsSubnets")} required error={show(issues.vpc && !form.vpcSubnets.trim(), "v2.agents.wizard.errVpc")}>
              <input
                className="v2-input mono"
                value={form.vpcSubnets}
                onChange={(e) => set({ vpcSubnets: e.target.value })}
                placeholder="subnet-0abc, subnet-0def"
                data-testid="v2-agent-fs-subnets"
              />
            </Field>
            <Field label={t("create.configure.fsSgs")} required error={show(issues.vpc && !form.vpcSgs.trim(), "v2.agents.wizard.errVpc")}>
              <input
                className="v2-input mono"
                value={form.vpcSgs}
                onChange={(e) => set({ vpcSgs: e.target.value })}
                placeholder="sg-0abc"
                data-testid="v2-agent-fs-sgs"
              />
            </Field>
          </div>
        )}
      </div>
    </Card>
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
  return (
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
            <Field label={t("create.configure.byocEntrypoint")} required error={err("byocEntrypoint")}>
              {candidates.length > 0 ? (
                <select
                  className="v2-select mono"
                  value={form.byocEntrypoint}
                  onChange={(e) => set({ byocEntrypoint: e.target.value })}
                  data-testid="v2-agent-byoc-entrypoint"
                >
                  {[...candidates, ...(candidates.includes(form.byocEntrypoint) ? [] : [form.byocEntrypoint])].map((c) => (
                    <option key={c} value={c}>
                      {c}
                    </option>
                  ))}
                </select>
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
            <Field label={t("create.configure.byocPython")}>
              <select
                className="v2-select"
                value={form.byocPython}
                onChange={(e) => onPython(e.target.value as ByocPythonVersion)}
                data-testid="v2-agent-byoc-python"
              >
                {BYOC_PYTHON_VERSIONS.map((v) => (
                  <option key={v} value={v}>
                    {pythonLabel(v)}
                  </option>
                ))}
              </select>
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
          </div>
        )}
      </div>
    </Card>
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
          <select
            className="v2-select"
            disabled={full}
            value=""
            onChange={(e) => {
              const picked = e.target.value;
              if (!picked) return;
              if (picked === CUSTOM_MODEL_OPTION) {
                setCustomOpen(true);
                return;
              }
              setCustomOpen(false);
              add(picked);
            }}
            data-testid="v2-agent-byoc-model-add"
          >
            <option value="">{t("create.configure.byocModelAdd")}</option>
            {modelOptionsFor(form.modelSource)
              .filter((o) => !models.includes(o.model_id))
              .map((o) => (
                <option key={o.model_id} value={o.model_id}>
                  {o.label} · {o.model_id}
                </option>
              ))}
            <option value={CUSTOM_MODEL_OPTION}>{t("v2.agents.wizard.customModel")}</option>
          </select>
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
          <h3 className="v2-sub-title">{t("create.configure.byocContract")}</h3>
          <pre className="v2-pre">{t("create.configure.byocContractBody")}</pre>
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
