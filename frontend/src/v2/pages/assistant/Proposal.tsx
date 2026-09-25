import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { FishboneDiagram } from "../../../components";
import {
  type AgentInfo,
  type AssistantApproval,
  type AssistantCatalog,
  type AssistantMemoryMode,
  type AssistantProposal,
  type AssistantStatus,
  HARNESS_NATIVE_TOOLS,
  type JobInfo,
  type StageInfo,
} from "../../../lib/api";
import { isFishbone, type ProposalEditDraft } from "../../../lib/assistant";
import { MODEL_CATALOG, type ModelSource } from "../../../lib/models";
import { fmtTime } from "../../format";
import { Alert, Descriptions, Field, Table, Tag } from "../../ui";
import { AGENT_TONE, JOB_TONE, shortId, STAGE_TONE } from "./common";

const list = (v: unknown): string[] => (Array.isArray(v) ? v.map(String) : []);

function Section({ title, children, testId }: { title: string; children: React.ReactNode; testId?: string }) {
  return (
    <div className="v2-assistant-section" data-testid={testId}>
      <h3>{title}</h3>
      {children}
    </div>
  );
}

/** The proposal verbatim, with the exact resources it binds to. */
export function ProposalView({
  proposal, catalog, account, region,
}: {
  proposal: AssistantProposal;
  catalog: AssistantCatalog | null;
  account: string;
  region: string;
}) {
  const { t } = useTranslation();
  const c = proposal.content;
  const label = (key: string) => {
    const tool = catalog?.tools.find((x) => x.key === key);
    const skill = catalog?.skills.find((x) => x.key === key);
    const kb = catalog?.knowledge_bases.find((x) => x.kb_id === key);
    return tool?.name ?? skill?.name ?? (kb ? `${kb.name || kb.kb_id} (${kb.kb_id})` : key);
  };
  const tags = (keys: string[], testId: string) =>
    keys.length ? (
      <span className="v2-tags" data-testid={testId}>
        {keys.map((k) => (
          <Tag key={k} tone="outline">{label(k)}</Tag>
        ))}
      </span>
    ) : (
      t("assistantPage.none")
    );
  const golden = Array.isArray(c.golden_tests) ? c.golden_tests : [];
  const b = proposal.bindings;
  const nativeChoices = list(c.native_tools);
  const toolsOverride = b?.allowed_tools;
  const selectedToolPolicy = b?.resources?.tool_access_policy === "selected-v1";
  const authLabel = (auth: Record<string, unknown> | null) => {
    const oauth = (auth?.oauth ?? null) as { providerArn?: string; grantType?: string } | null;
    if (oauth) return `oauth · ${oauth.grantType ?? ""} · ${(oauth.providerArn ?? "").split("/").slice(-1)[0]}`;
    return auth ? Object.keys(auth).join(",") : "none";
  };
  return (
    <div data-testid="v2-assistant-proposal-view" data-revision={proposal.revision} data-status={proposal.status}>
      <div className="v2-muted" style={{ fontSize: 12.5, marginBottom: 10 }}>
        {proposal.source === "model"
          ? t("assistantPage.source.model")
          : t("assistantPage.source.member", { who: proposal.created_by })}
        {proposal.created_at ? ` · ${fmtTime(proposal.created_at)}` : ""}
      </div>
      {proposal.status === "invalid" && (
        <Alert tone="error">
          {t("assistantPage.invalidNote")}
          <ul className="v2-list">
            {proposal.validation_errors.map((e, i) => (
              <li key={i} className="mono" style={{ fontSize: 12 }}>{e}</li>
            ))}
          </ul>
        </Alert>
      )}
      <Descriptions
        items={[
          { label: t("assistantPage.field.name"), value: <span className="mono" data-testid="v2-assistant-proposal-name">{String(c.name ?? "")}</span> },
          { label: t("assistantPage.field.model"), value: <span className="mono">{String(c.model_id ?? "")} · {String(c.model_source ?? "")}</span> },
          { label: t("assistantPage.field.tools"), value: tags(list(c.tools), "v2-assistant-proposal-tools") },
          {
            label: t("create.nativeTools.title"),
            value: nativeChoices.length
              ? nativeChoices.map((tool) => t(`create.nativeTools.${tool}`, { defaultValue: tool })).join(" · ")
              : t(selectedToolPolicy && toolsOverride == null ? "create.nativeTools.unavailable" : "create.nativeTools.none"),
          },
          { label: t("assistantPage.field.skills"), value: tags(list(c.skills), "v2-assistant-proposal-skills") },
          { label: t("assistantPage.field.kbs"), value: tags(list(c.knowledge_bases), "v2-assistant-proposal-kbs") },
          {
            label: t("assistantPage.field.memory"),
            value: c.memory === "workspace" ? t("assistantPage.memoryWorkspace") : t("assistantPage.memoryDisabled"),
          },
          {
            label: `${t("assistantPage.field.iterations")} / ${t("assistantPage.field.timeout")}`,
            value: <span className="mono">{String(c.max_iterations ?? "")} / {String(c.timeout_seconds ?? "")}</span>,
          },
          { label: t("assistantPage.field.target"), value: <span className="mono">{account} · {region} · harness</span> },
        ]}
      />
      {Array.isArray(toolsOverride) ? (
        <div style={{ marginTop: 12 }}>
          <Alert tone="warn">
            <strong>{t("create.nativeTools.overrideTitle")}</strong>
            <pre className="mono" style={{ whiteSpace: "pre-wrap", overflowWrap: "anywhere", margin: "4px 0" }}>
              {JSON.stringify(toolsOverride)}
            </pre>
            {t("create.nativeTools.overrideSummary")}
          </Alert>
        </div>
      ) : b && !selectedToolPolicy ? (
        <div style={{ marginTop: 12 }}>
          <Alert>{t("create.nativeTools.legacyHint")}</Alert>
        </div>
      ) : null}
      {b && (
        <Section title={t("assistantPage.bindings")} testId="v2-assistant-bindings">
          <ul className="v2-assistant-bindings">
            {Object.entries(b.resources?.gateways ?? {}).map(([id, g]) => (
              <li key={`gw-${id}`}>
                gateway · {g.gateway_name ?? id} → {g.gateway_arn} · {t("assistantPage.bindingsAuth")} {authLabel(g.outbound_auth)}
              </li>
            ))}
            {Object.entries(b.resources?.remote_mcp ?? {}).map(([name, m]) => (
              <li key={`mcp-${name}`}>mcp · {name} → {m.url}</li>
            ))}
            {Object.entries(b.resources?.skills ?? {}).map(([key, s]) => (
              <li key={`skill-${key}`}>
                skill · {key} → {s.path} · {t("assistantPage.bindingsDigest")} {(s.content_digest ?? "").slice(0, 12)}
                {s.object_count != null ? ` (${s.object_count})` : ""}
              </li>
            ))}
            {b.knowledge_bases.map((kb) => (
              <li key={kb.kb_id}>
                kb → {kb.kb_id}
                {kb.name ? ` (${kb.name})` : ""}
                {b.resources?.kb_gateway ? ` · via ${b.resources.kb_gateway.gateway_id}` : ""}
              </li>
            ))}
            <li>
              memory → {b.resources?.memory.mode}
              {b.resources?.memory.arn ? ` · ${b.resources.memory.arn}` : ""}
            </li>
          </ul>
        </Section>
      )}
      <Section title={t("assistantPage.field.prompt")}>
        <pre className="v2-pre v2-assistant-prompt" data-testid="v2-assistant-proposal-prompt">{String(c.system_prompt ?? "")}</pre>
      </Section>
      {typeof c.summary === "string" && c.summary && (
        <Section title={t("assistantPage.summary")}>
          <p style={{ margin: 0, fontSize: 13 }}>{c.summary}</p>
        </Section>
      )}
      {isFishbone(c.fishbone) && (
        <Section title={t("assistantPage.fishbone")} testId="v2-assistant-fishbone">
          <div className="v2-muted" style={{ fontSize: 12.5, marginBottom: 8 }}>{t("assistantPage.fishboneNote")}</div>
          <div className="v2-assistant-fishbone">
            <FishboneDiagram fishbone={c.fishbone} />
          </div>
        </Section>
      )}
      {list(c.requirements_baseline).length > 0 && (
        <Section title={t("assistantPage.baseline")}>
          <ul>{list(c.requirements_baseline).map((x, i) => <li key={i}>{x}</li>)}</ul>
        </Section>
      )}
      {list(c.assumptions).length > 0 && (
        <Section title={t("assistantPage.assumptions")}>
          <ul>{list(c.assumptions).map((x, i) => <li key={i}>{x}</li>)}</ul>
        </Section>
      )}
      {list(c.manual_tasks).length > 0 && (
        <Section title={t("assistantPage.manualTasks")} testId="v2-assistant-manual-tasks">
          <ul>{list(c.manual_tasks).map((x, i) => <li key={i}>{x}</li>)}</ul>
        </Section>
      )}
      {golden.length > 0 && (
        <Section title={t("assistantPage.goldenTests")} testId="v2-assistant-golden-tests">
          <Table
            density="dense"
            rows={golden}
            rowKey={(g) => g.id}
            columns={[
              { key: "id", title: t("assistantPage.gt.id"), render: (g) => <span className="mono">{g.id}</span> },
              { key: "input", title: t("assistantPage.gt.input"), render: (g) => g.input },
              { key: "exp", title: t("assistantPage.gt.expected"), render: (g) => g.expected_response ?? "" },
              { key: "forbid", title: t("assistantPage.gt.forbidden"), render: (g) => g.forbidden_behavior ?? "" },
              {
                key: "eval",
                title: t("assistantPage.gt.evaluator"),
                render: (g) => (
                  <>
                    <span className="mono">{g.evaluator ?? ""}</span>
                    {g.source && <span className="sub mono">{g.source}</span>}
                  </>
                ),
              },
            ]}
          />
        </Section>
      )}
      {list(c.evaluator_recommendations).length > 0 && (
        <Section title={t("assistantPage.evaluators")}>
          <ul>{list(c.evaluator_recommendations).map((x, i) => <li key={i} className="mono">{x}</li>)}</ul>
        </Section>
      )}
    </div>
  );
}

/** Member edit of the latest revision: resources only from this workspace. */
export function ProposalEditor({
  draft, catalog, capabilities, errors, onChange, resourcesLocked,
}: {
  draft: ProposalEditDraft;
  catalog: AssistantCatalog;
  capabilities: AssistantStatus["capabilities"];
  errors: Record<string, boolean>;
  onChange: (next: ProposalEditDraft) => void;
  resourcesLocked: boolean;
}) {
  const { t } = useTranslation();
  const set = <K extends keyof ProposalEditDraft>(key: K, value: ProposalEditDraft[K]) =>
    onChange({ ...draft, [key]: value });
  const toggle = (key: "tools" | "skills" | "knowledge_bases", value: string) => {
    if (resourcesLocked) return;
    const has = draft[key].includes(value);
    set(key, has ? draft[key].filter((x) => x !== value) : [...draft[key], value]);
  };
  const bad = (k: string) => (errors[k] ? t("v2.assistant.fieldInvalid") : null);
  const models = MODEL_CATALOG[draft.model_source];
  const memoryModes: AssistantMemoryMode[] = ["disabled", "workspace"];
  const check = (on: boolean, disabled: boolean, label: string, onToggle: () => void, testId?: string, title?: string) => (
    <label key={label} className={`v2-check${disabled ? " disabled" : ""}`} title={title}>
      <input type="checkbox" checked={on} disabled={disabled} onChange={onToggle} data-testid={testId} />
      {label}
    </label>
  );
  return (
    <div data-testid="v2-assistant-proposal-editor">
      <Alert>{t("assistantPage.editHint")}</Alert>
      {resourcesLocked && <Alert tone="warn">{t("assistantPreparation.lockedHint")}</Alert>}
      <div className="v2-form">
        <Field label={t("assistantPage.field.name")} required error={bad("name")}>
          <input className="v2-input mono" value={draft.name} onChange={(e) => set("name", e.target.value)} data-testid="v2-assistant-edit-name" />
        </Field>
        <Field label={t("assistantPage.field.modelSource")}>
          <select
            className="v2-select"
            value={draft.model_source}
            onChange={(e) => {
              const source = e.target.value as ModelSource;
              onChange({ ...draft, model_source: source, model_id: MODEL_CATALOG[source][0].model_id });
            }}
          >
            <option value="bedrock">bedrock</option>
            <option value="mantle">mantle</option>
          </select>
        </Field>
        <Field label={t("assistantPage.field.model")} required error={bad("model_id")}>
          <input
            className="v2-input mono"
            value={draft.model_id}
            list="v2-assistant-models"
            onChange={(e) => set("model_id", e.target.value)}
            data-testid="v2-assistant-edit-model"
          />
          <datalist id="v2-assistant-models">
            {models.map((m) => (
              <option key={m.model_id} value={m.model_id}>{m.label}</option>
            ))}
          </datalist>
        </Field>
        <Field label={t("assistantPage.field.prompt")} required error={bad("system_prompt")}>
          <textarea
            className="v2-textarea code"
            style={{ minHeight: 180 }}
            value={draft.system_prompt}
            onChange={(e) => set("system_prompt", e.target.value)}
            data-testid="v2-assistant-edit-prompt"
          />
        </Field>
        <Field label={t("assistantPage.field.tools")}>
          <div className="v2-checks">
            {catalog.tools.filter((x) => x.attachable).map((x) =>
              check(draft.tools.includes(x.key), resourcesLocked, x.name, () => toggle("tools", x.key), `v2-assistant-edit-tool-${x.name}`))}
            {catalog.tools.length === 0 && <span className="v2-muted">{t("assistantPage.catalogNone")}</span>}
          </div>
        </Field>
        <Field label={t("create.nativeTools.title")} hint={t("create.nativeTools.selectedHint")}>
          {draft.native_tools.length === 0 && (
            <div className="v2-muted" style={{ marginBottom: 6 }}>{t("create.nativeTools.unavailable")}</div>
          )}
          <div className="v2-checks">
            {HARNESS_NATIVE_TOOLS.map((tool) =>
              check(
                draft.native_tools.includes(tool),
                false,
                t(`create.nativeTools.${tool}`),
                () => set("native_tools", draft.native_tools.includes(tool)
                  ? draft.native_tools.filter((value) => value !== tool)
                  : [...draft.native_tools, tool]),
                `v2-assistant-edit-native-${tool}`,
              ))}
          </div>
        </Field>
        <Field label={t("assistantPage.field.skills")}>
          <div className="v2-checks">
            {catalog.skills.map((x) => check(draft.skills.includes(x.key), resourcesLocked, x.name, () => toggle("skills", x.key)))}
            {catalog.skills.length === 0 && <span className="v2-muted">{t("assistantPage.catalogNone")}</span>}
          </div>
        </Field>
        <Field label={t("assistantPage.field.kbs")} hint={capabilities.kb_gateway ? undefined : t("assistantPage.kbGatewayMissing")}>
          <div className="v2-checks">
            {catalog.knowledge_bases.map((x) =>
              check(
                draft.knowledge_bases.includes(x.kb_id),
                resourcesLocked || !capabilities.kb_gateway,
                x.name || x.kb_id,
                () => toggle("knowledge_bases", x.kb_id),
              ))}
            {catalog.knowledge_bases.length === 0 && <span className="v2-muted">{t("assistantPage.catalogNone")}</span>}
          </div>
        </Field>
        <Field label={t("assistantPage.field.memory")} hint={capabilities.shared_memory ? undefined : t("assistantPage.memoryNoShared")}>
          <div className="v2-checks">
            {memoryModes.map((mode) => (
              <label key={mode} className={`v2-check${mode === "workspace" && !capabilities.shared_memory ? " disabled" : ""}`}>
                <input
                  type="radio"
                  name="v2-assistant-memory"
                  checked={draft.memory === mode}
                  disabled={mode === "workspace" && !capabilities.shared_memory}
                  onChange={() => set("memory", mode)}
                  data-testid={`v2-assistant-edit-memory-${mode}`}
                />
                {mode === "workspace" ? t("assistantPage.memoryWorkspace") : t("assistantPage.memoryDisabled")}
              </label>
            ))}
          </div>
        </Field>
      </div>
      <div className="v2-form cols-2" style={{ marginTop: 16 }}>
        <Field label={t("assistantPage.field.iterations")} hint="1 – 100" error={bad("max_iterations")}>
          <input
            className="v2-input mono"
            type="number"
            min={1}
            max={100}
            value={draft.max_iterations}
            onChange={(e) => set("max_iterations", Number(e.target.value))}
          />
        </Field>
        <Field label={t("assistantPage.field.timeout")} hint="10 – 3600" error={bad("timeout_seconds")}>
          <input
            className="v2-input mono"
            type="number"
            min={10}
            max={3600}
            value={draft.timeout_seconds}
            onChange={(e) => set("timeout_seconds", Number(e.target.value))}
          />
        </Field>
      </div>
    </div>
  );
}

/** Deployment outcome of an approved revision: the ordinary job + agent, polled. */
export function Outcome({
  revision, approval, job, agent,
}: {
  revision: number;
  approval: AssistantApproval;
  job: JobInfo | null;
  agent: AgentInfo | null;
}) {
  const { t } = useTranslation();
  const jobStatus = job?.status ?? approval.job_status;
  const agentStatus = agent?.status ?? approval.agent_status;
  const failed = jobStatus === "failed" || agentStatus === "failed";
  const succeeded = jobStatus === "succeeded" && agentStatus === "active";
  const stages: StageInfo[] = agent?.deployments?.find((d) => d.job_id === approval.job_id)?.stages ?? [];
  const error = agent?.error || approval.agent_error || job?.error;
  return (
    <div
      className="v2-assistant-section"
      data-testid="v2-assistant-outcome"
      data-job-status={jobStatus ?? ""}
      data-revision={revision}
    >
      <h3 className="v2-row">
        {t("assistantPage.outcomeTitle")} · {t("assistantPage.revision", { n: revision })}
        <span className="v2-muted" style={{ fontWeight: 400, fontSize: 12.5 }}>
          {t("assistantPage.outcomeApprovedBy", { who: approval.approved_by ?? "", at: fmtTime(approval.approved_at) })}
        </span>
      </h3>
      <Descriptions
        items={[
          {
            label: t("assistantPage.outcomeAgent"),
            value: (
              <span className="v2-row">
                {approval.agent_name ?? agent?.name ?? ""}
                {agentStatus && (
                  <Tag tone={failed ? "red" : AGENT_TONE[agentStatus] ?? "gray"} dot>
                    {t(`status.${agentStatus}`, { defaultValue: agentStatus })}
                  </Tag>
                )}
              </span>
            ),
          },
          {
            label: t("assistantPage.outcomeJob"),
            value: (
              <span className="v2-row">
                <span className="mono">{shortId(approval.job_id)}</span>
                {jobStatus && <Tag tone={JOB_TONE[jobStatus] ?? "gray"}>{String(jobStatus).toUpperCase()}</Tag>}
              </span>
            ),
          },
        ]}
      />
      {stages.length > 0 && (
        <div className="v2-stages" style={{ marginTop: 12 }} data-testid="v2-assistant-deploy-stages">
          {stages.map((s, i) => (
            <div key={s.name} className={`v2-stage ${s.status}`}>
              <span className="n">{s.status === "succeeded" || s.status === "skipped" ? "✓" : s.status === "failed" ? "✕" : i + 1}</span>
              <div className="b">
                <div className="t">
                  {t(`create.stages.${s.name}`, { defaultValue: s.name })}
                </div>
                <div className="d">
                  <Tag tone={STAGE_TONE[s.status] ?? "gray"}>{t(`assistantPage.stage.${s.status}`)}</Tag>
                </div>
              </div>
            </div>
          ))}
        </div>
      )}
      <div style={{ marginTop: 12 }} data-testid="v2-assistant-deploy-verdict">
        <Alert tone={failed ? "error" : succeeded ? "success" : "info"}>
          {failed ? t("assistantPage.outcomeFailed") : succeeded ? t("assistantPage.outcomeSucceeded") : t("assistantPage.outcomeRunning")}
          {failed && error && <div className="mono" style={{ fontSize: 12, marginTop: 4 }}>{error}</div>}
        </Alert>
      </div>
      <div className="v2-row">
        {approval.agent_id && (
          <Link
            className="v2-btn sm"
            to={`/v2/agents?view=detail&id=${encodeURIComponent(approval.agent_id)}`}
            data-testid="v2-assistant-open-agent"
          >
            {t("assistantPage.openAgent")}
          </Link>
        )}
        {succeeded && approval.agent_id && (
          <Link
            className="v2-btn sm primary"
            to={`/v2/chat?agent=${encodeURIComponent(approval.agent_id)}`}
            data-testid="v2-assistant-open-chat"
          >
            {t("assistantPage.openChat")}
          </Link>
        )}
      </div>
    </div>
  );
}
