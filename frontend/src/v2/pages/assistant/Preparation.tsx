import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import {
  api,
  ApiError,
  type AssistantCatalog,
  type AssistantCatalogTool,
  type AssistantConversationDetail,
  type AttachableKnowledgeBase,
  errorMessage,
} from "../../../lib/api";
import { type AssistantKbDetail, kbReadiness } from "../../../lib/assistant";
import { Alert, Button, Tag } from "../../ui";
import { KnowledgeBaseCreateDrawer } from "./KnowledgeBaseCreate";
import { SECTION_IDS } from "./common";

const sameKeys = (a: string[], b: string[]) => a.length === b.length && a.every((key) => b.includes(key));
const toggle = (values: string[], value: string) =>
  values.includes(value) ? values.filter((item) => item !== value) : [...values, value];

const MAX_TOOLS = 20;
const PREP_SECTION = { knowledge_base: "v2-assistant-prep-kbs", skill: "v2-assistant-prep-skills", tool: "v2-assistant-prep-tools" };

/**
 * Resource preparation before creation: pick knowledge bases, Skills and MCP tools
 * from this workspace (create a KB here, or a Skill / tool in the Registry), then
 * SAVE the selection as a new preparation revision. Opening history is read-only;
 * refreshing the reviewed catalog is explicit (it may create a new proposal
 * revision when a binding changed). Locked once a revision was approved.
 */
export function PreparationCard({
  conversation, workspaceId, disabled, locked, onDirty, onUpdated, onCatalog, onWorking, onDiscuss,
}: {
  conversation: AssistantConversationDetail;
  workspaceId: string;
  disabled: boolean;
  locked: boolean;
  onDirty: (dirty: boolean) => void;
  onUpdated: (detail: AssistantConversationDetail) => void;
  onCatalog: (catalog: AssistantCatalog) => void;
  onWorking: (working: boolean) => void;
  onDiscuss: (text: string) => void;
}) {
  const { t } = useTranslation();
  const prep = conversation.preparation;
  const savedTools = useMemo(() => {
    if (prep?.tools !== undefined) return prep.tools;
    // Legacy preparation did not project tools. Match the server's latest valid
    // proposal fallback, including superseded revisions with valid bindings.
    const latest = conversation.proposals.reduce<AssistantConversationDetail["proposals"][number] | null>(
      (found, proposal) => proposal.bindings && proposal.validation_errors.length === 0
        && (!found || proposal.revision > found.revision) ? proposal : found,
      null,
    );
    const tools = latest?.content.tools;
    return Array.isArray(tools) && tools.every((key) => typeof key === "string") ? tools : [];
  }, [prep?.tools, conversation.proposals]);
  const [kbs, setKbs] = useState<AttachableKnowledgeBase[] | null>(null);
  const [kbError, setKbError] = useState<string | null>(null);
  const [actionError, setActionError] = useState<string | null>(null);
  const [notice, setNotice] = useState<string | null>(null);
  const [refreshing, setRefreshing] = useState(false);
  const [working, setWorking] = useState(false);
  const [creating, setCreating] = useState(false);
  const [selectedKbs, setSelectedKbs] = useState(prep?.knowledge_bases ?? []);
  const [selectedSkills, setSelectedSkills] = useState(prep?.skills ?? []);
  const [selectedTools, setSelectedTools] = useState(savedTools);
  const previousPreparation = useRef(prep);
  const previousTools = useRef(savedTools);
  const alive = useRef(true);
  const callbacks = useRef({ onCatalog, onUpdated, onWorking, onDirty });
  callbacks.current = { onCatalog, onUpdated, onWorking, onDirty };
  const busy = disabled || working || refreshing;
  const selectionDisabled = busy || locked;
  const catalog = conversation.catalog;
  const resources = catalog.resources;
  const gatewayReady = Boolean(
    resources?.kb_gateway_id && resources.kb_gateway_arn && resources.oauth_provider_arn
      && resources.kb_gateway?.status === "READY" && resources.kb_gateway.url,
  );
  const availableKbIds = new Set(catalog.knowledge_bases.map((kb) => kb.kb_id));
  const availableSkillKeys = new Set(catalog.skills.filter((skill) => skill.content_digest).map((skill) => skill.key));
  const availableToolKeys = new Set(catalog.tools.filter((tool) => tool.attachable).map((tool) => tool.key));
  const changed = !sameKeys(selectedKbs, prep?.knowledge_bases ?? [])
    || !sameKeys(selectedSkills, prep?.skills ?? [])
    || !sameKeys(selectedTools, savedTools);
  const invalidSelection = selectedKbs.some((id) => !availableKbIds.has(id))
    || (selectedKbs.length > 0 && !gatewayReady)
    || selectedSkills.some((key) => !availableSkillKeys.has(key))
    || selectedTools.some((key) => !availableToolKeys.has(key))
    || selectedTools.length > MAX_TOOLS;
  const describeError = (err: unknown) =>
    err instanceof ApiError ? t(`apiErrors.${err.code}`, err.message) : errorMessage(err);

  useEffect(() => {
    callbacks.current.onDirty(changed && !locked);
    return () => callbacks.current.onDirty(false);
  }, [changed, locked]);

  useEffect(() => {
    // A catalog refresh may advance server revisions. Preserve unsaved choices;
    // adopt new server selections only where the local selection was untouched.
    // Approval in another tab wins over a local draft.
    const previous = previousPreparation.current;
    setSelectedKbs((current) => locked || sameKeys(current, previous?.knowledge_bases ?? [])
      ? prep?.knowledge_bases ?? [] : current);
    setSelectedSkills((current) => locked || sameKeys(current, previous?.skills ?? [])
      ? prep?.skills ?? [] : current);
    const priorTools = previousTools.current;
    setSelectedTools((current) => locked || sameKeys(current, priorTools) ? savedTools : current);
    previousPreparation.current = prep;
    previousTools.current = savedTools;
  }, [prep, savedTools, locked]);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
      callbacks.current.onWorking(false);
    };
  }, []);

  // Catalog reads stay pinned to the displayed workspace. Failures retain the
  // previous successful rows and never erase saved selections.
  const refresh = useCallback(async () => {
    setRefreshing(true);
    setKbError(null);
    setActionError(null);
    callbacks.current.onWorking(true);
    const [kbResult, catalogResult] = await Promise.allSettled([
      api.listAttachableKnowledgeBases(workspaceId),
      api.assistantRefreshCatalog(conversation.id, workspaceId),
    ]);
    if (!alive.current) return;
    if (kbResult.status === "fulfilled") setKbs(kbResult.value.items);
    else setKbError(errorMessage(kbResult.reason));
    if (catalogResult.status === "fulfilled") {
      if (catalogResult.value.conversation) callbacks.current.onUpdated(catalogResult.value.conversation);
      else callbacks.current.onCatalog(catalogResult.value.catalog);
    } else setActionError(errorMessage(catalogResult.reason));
    setRefreshing(false);
    callbacks.current.onWorking(false);
  }, [conversation.id, workspaceId]);

  useEffect(() => {
    let cancelled = false;
    void api.listAttachableKnowledgeBases(workspaceId).then(
      (res) => { if (!cancelled) setKbs(res.items); },
      (err: unknown) => { if (!cancelled) setKbError(errorMessage(err)); },
    );
    return () => { cancelled = true; };
  }, [workspaceId]);

  const save = async () => {
    if (selectionDisabled) return;
    setWorking(true);
    setActionError(null);
    setNotice(null);
    callbacks.current.onWorking(true);
    try {
      const detail = await api.assistantSavePreparation(conversation.id, {
        expected_revision: prep?.revision ?? 0,
        knowledge_bases: selectedKbs,
        skills: selectedSkills,
        tools: selectedTools,
      }, workspaceId);
      if (!alive.current) return;
      callbacks.current.onUpdated(detail);
      setNotice(t("assistantPreparation.saved"));
    } catch (err) {
      if (!alive.current) return;
      setActionError(describeError(err));
      if (err instanceof ApiError && (err.code.includes("stale") || err.code === "assistant.resources_locked")) {
        try {
          const detail = await api.assistantConversation(conversation.id, workspaceId);
          if (alive.current) callbacks.current.onUpdated(detail);
        } catch { /* retain the original conflict and offer a manual refresh */ }
      }
    } finally {
      if (alive.current) {
        setWorking(false);
        callbacks.current.onWorking(false);
      }
    }
  };

  const kbRows = [
    ...(kbs ?? catalog.knowledge_bases),
    ...selectedKbs.filter((id) => !(kbs ?? catalog.knowledge_bases).some((kb) => kb.kb_id === id))
      .map((kb_id) => ({ kb_id, name: kb_id, description: "", status: "UNAVAILABLE" })),
  ];
  const skillRows = [
    ...catalog.skills,
    ...selectedSkills.filter((key) => !catalog.skills.some((skill) => skill.key === key))
      .map((key) => ({ key, name: key, description: "", content_digest: null })),
  ];
  const toolRows: AssistantCatalogTool[] = [
    ...catalog.tools,
    ...selectedTools.filter((key) => !catalog.tools.some((tool) => tool.key === key))
      .map((key): AssistantCatalogTool => ({
        key, kind: "mcp", name: key, description: "", attachable: false,
        reason: t("assistantPreparation.toolMissing"),
      })),
  ];
  const jump = (id: string) => document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "center" });

  return (
    <section id={SECTION_IDS.resources} className="v2-card" data-testid="v2-assistant-preparation">
      <div className="v2-card-body">
        <h2 className="v2-sec-title">
          {t("assistantPreparation.title")}
          <span className="sub">{t("assistantPreparation.subtitle")}</span>
          <span className="end">
            <Button size="sm" disabled={busy} onClick={() => void refresh()} testId="v2-assistant-prep-refresh">
              {t(refreshing ? "assistantPreparation.refreshing" : "assistantPreparation.refresh")}
            </Button>
          </span>
        </h2>
        <Alert
          tone={locked ? "success" : "info"}
          action={locked ? <Link className="v2-link" to="/v2/agents">{t("assistantPreparation.manageAgent")}</Link> : undefined}
        >
          <strong>{t(locked ? "assistantPreparation.lockedTitle" : "assistantPreparation.beforeCreateTitle")}</strong>
          <div>{t(locked ? "assistantPreparation.lockedHint" : "assistantPreparation.beforeCreateHint")}</div>
        </Alert>

        {!locked && (prep?.requirements ?? []).length > 0 && (
          <div className="v2-assistant-section" data-testid="v2-assistant-prep-requirements" style={{ marginTop: 0, marginBottom: 16 }}>
            <h3>{t("assistantPreparation.requirements")}</h3>
            <div className="v2-assistant-options">
              {prep!.requirements.map((item) => (
                <div className="v2-assistant-req" key={item.id}>
                  <div className="v2-row">
                    <b>{item.title}</b>
                    <Tag tone={item.required ? "orange" : "gray"}>
                      {t(item.required ? "assistantPreparation.required" : "assistantPreparation.optional")}
                    </Tag>
                  </div>
                  <p>{item.reason}</p>
                  {item.materials.length > 0 && <ul>{item.materials.map((m, i) => <li key={i}>{m}</li>)}</ul>}
                  <button
                    type="button"
                    className="v2-link"
                    onClick={() => {
                      if (item.kind === "clarification") onDiscuss(t("assistantPreparation.discussPrompt", { title: item.title }));
                      else jump(PREP_SECTION[item.kind]);
                    }}
                  >
                    {t(item.kind !== "clarification" ? "assistantPreparation.prepare" : "assistantPreparation.discuss")}
                  </button>
                </div>
              ))}
            </div>
          </div>
        )}

        <div id={PREP_SECTION.knowledge_base} className="v2-assistant-prep-sec">
          <h3>{t("assistantPreparation.knowledgeTitle")}</h3>
          <p className="v2-muted">{t("assistantPreparation.knowledgeHint")}</p>
          {kbError && (
            <Alert tone="error" action={<button type="button" className="v2-link" onClick={() => void refresh()}>{t("v2.common.retry")}</button>}>
              {kbError}
            </Alert>
          )}
          {kbs === null && !kbError && <p className="v2-muted">{t("common.loading")}</p>}
          {kbs?.length === 0 && !kbError && <p className="v2-muted">{t("assistantPreparation.noKbs")}</p>}
          {!gatewayReady && <Alert tone="warn">{t("assistantPreparation.gatewayMissing")}</Alert>}
          <div className="v2-assistant-options" data-testid="v2-assistant-prep-kb-options">
            {kbRows.map((kb) => {
              const selected = selectedKbs.includes(kb.kb_id);
              const available = availableKbIds.has(kb.kb_id) && gatewayReady
                && (!("status" in kb) || !kb.status || kb.status === "ACTIVE");
              const off = selectionDisabled || (!selected && !available);
              return (
                <label className={`v2-assistant-option${selected ? " on" : ""}${off ? " disabled" : ""}`} key={kb.kb_id}>
                  <input type="checkbox" checked={selected} disabled={off}
                    onChange={() => setSelectedKbs(toggle(selectedKbs, kb.kb_id))} />
                  <span className="b">
                    <b>{kb.name}</b>
                    {kb.description && <span className="v2-muted v2-assistant-clamp" title={kb.description}>{kb.description}</span>}
                    <span className="v2-muted mono">{kb.kb_id}</span>
                  </span>
                  <Tag tone={available ? "green" : "gray"}>
                    {available ? t("assistantPreparation.mountable")
                      : ("status" in kb && kb.status && kb.status !== "ACTIVE"
                        ? t(`knowledge.status.${kb.status.toLowerCase()}`, kb.status)
                        : t("assistantPreparation.unavailable"))}
                  </Tag>
                </label>
              );
            })}
          </div>
          {selectedKbs.length > 0 && (
            <div className="v2-stack" style={{ marginTop: 10 }}>
              {selectedKbs.map((kbId) => (
                <KbReadiness key={`${kbId}:${catalog.fetched_at}`} kbId={kbId}
                  name={kbRows.find((kb) => kb.kb_id === kbId)?.name ?? kbId} workspaceId={workspaceId} />
              ))}
            </div>
          )}
          <p className="v2-muted" style={{ fontSize: 12.5 }}>{t("assistantPreparation.indexHint")}</p>
          {!locked && (
            <div className="v2-row">
              <Button size="sm" disabled={busy || creating} onClick={() => setCreating(true)} testId="v2-assistant-prep-create-kb">
                {t("assistantPreparation.newKb")}
              </Button>
              <Link className="v2-btn sm" to="/v2/knowledge-bases" target="_blank" rel="noopener noreferrer">
                {t("assistantPreparation.manageKbs")}
              </Link>
            </div>
          )}
        </div>

        <div id={PREP_SECTION.skill} className="v2-assistant-prep-sec">
          <h3>{t("assistantPreparation.skillsTitle")}</h3>
          <p className="v2-muted">{t("assistantPreparation.skillsHint")}</p>
          {skillRows.length === 0 && catalog.warnings.length === 0 && <p className="v2-muted">{t("assistantPreparation.noSkills")}</p>}
          <div className="v2-assistant-options">
            {skillRows.map((skill) => {
              const selected = selectedSkills.includes(skill.key);
              const available = availableSkillKeys.has(skill.key);
              const off = selectionDisabled || (!selected && !available);
              return (
                <label className={`v2-assistant-option${selected ? " on" : ""}${off ? " disabled" : ""}`} key={skill.key}>
                  <input type="checkbox" checked={selected} disabled={off}
                    onChange={() => setSelectedSkills(toggle(selectedSkills, skill.key))} />
                  <span className="b">
                    <b>{skill.name}</b>
                    <span className="v2-muted v2-assistant-clamp" title={skill.description}>{skill.description}</span>
                  </span>
                  {!available && <Tag tone="orange">{t("assistantPreparation.unavailable")}</Tag>}
                </label>
              );
            })}
          </div>
          {!locked && (
            <>
              <div className="v2-row" style={{ marginTop: 10 }}>
                <Button size="sm" disabled={busy} onClick={() => void refresh()}>{t("assistantPreparation.refresh")}</Button>
                <Link className="v2-btn sm" to="/registry?view=register&type=AGENT_SKILLS" target="_blank" rel="noopener noreferrer">
                  {t("assistantPreparation.createSkill")}
                </Link>
              </div>
              <p className="v2-muted" style={{ fontSize: 12.5 }}>{t("assistantPreparation.registryHint")}</p>
            </>
          )}
        </div>

        <div id={PREP_SECTION.tool} className="v2-assistant-prep-sec">
          <h3>{t("assistantPreparation.toolsTitle")}</h3>
          <p className="v2-muted">{t("assistantPreparation.toolsHint")}</p>
          {toolRows.length === 0 && catalog.warnings.length === 0 && <p className="v2-muted">{t("assistantPreparation.noTools")}</p>}
          <div className="v2-assistant-options" data-testid="v2-assistant-prep-tool-options">
            {toolRows.map((tool) => {
              const selected = selectedTools.includes(tool.key);
              const known = tool.runtime_tools;
              const off = selectionDisabled || (!selected && (!tool.attachable || selectedTools.length >= MAX_TOOLS));
              return (
                <label className={`v2-assistant-option${selected ? " on" : ""}${off ? " disabled" : ""}`} key={tool.key}>
                  <input type="checkbox" checked={selected} aria-label={tool.name} disabled={off}
                    onChange={() => setSelectedTools((current) => toggle(current, tool.key))} />
                  <span className="b">
                    <b>{tool.name}</b>
                    {tool.description && <span className="v2-muted v2-assistant-clamp" title={tool.description}>{tool.description}</span>}
                    {!tool.attachable && <span className="v2-muted">{tool.reason || t("assistantPreparation.toolUnavailable")}</span>}
                    {known != null ? (
                      <details onClick={(e) => e.stopPropagation()}>
                        <summary>{t("assistantPreparation.callableNames", { count: known.length })}</summary>
                        {known.length > 0
                          ? <ul>{known.map((name) => <li key={name}><code>{name}</code></li>)}</ul>
                          : <div>{t("assistantPreparation.noCallableTools")}</div>}
                      </details>
                    ) : tool.attachable ? (
                      <span className="v2-muted">{t("assistantPreparation.toolDiscoveryUnknown")}</span>
                    ) : null}
                  </span>
                  <Tag tone={tool.attachable ? "green" : "orange"}>
                    {t(tool.attachable ? "assistantPreparation.mountable" : "assistantPreparation.unavailable")}
                  </Tag>
                </label>
              );
            })}
          </div>
          <p className="v2-muted" style={{ fontSize: 12.5 }}>
            {t("assistantPreparation.toolsLimit", { count: selectedTools.length, max: MAX_TOOLS })}
          </p>
          {!locked && (
            <>
              <div className="v2-row">
                <Button size="sm" disabled={busy} onClick={() => void refresh()}>{t("assistantPreparation.refresh")}</Button>
                <Link className="v2-btn sm" to="/registry?view=register&type=MCP" target="_blank" rel="noopener noreferrer">
                  {t("assistantPreparation.createTool")}
                </Link>
              </div>
              <p className="v2-muted" style={{ fontSize: 12.5 }}>{t("assistantPreparation.toolRegistryHint")}</p>
            </>
          )}
          <Alert>{t("assistantPreparation.toolEvaluationHint")}</Alert>
        </div>

        {catalog.warnings.length > 0 && (
          <Alert tone="warn">
            {t("assistantPreparation.catalogWarning")}
            <ul className="v2-list">{catalog.warnings.map((w, i) => <li key={i}>{w}</li>)}</ul>
          </Alert>
        )}
        {actionError && <Alert tone="error">{actionError}</Alert>}
        {notice && <Alert tone="success">{notice}</Alert>}
        {!locked && (
          <div className="v2-assistant-save">
            <span className="v2-muted">{t(changed ? "assistantPreparation.unsaved" : "assistantPreparation.savedHint")}</span>
            {changed && (
              <Button
                disabled={busy}
                onClick={() => {
                  setSelectedKbs(prep?.knowledge_bases ?? []);
                  setSelectedSkills(prep?.skills ?? []);
                  setSelectedTools(savedTools);
                  setActionError(null);
                  setNotice(null);
                }}
              >
                {t("common.cancel")}
              </Button>
            )}
            <Button
              kind="primary"
              disabled={busy || !changed || invalidSelection}
              title={!busy && changed && invalidSelection ? t("assistantPreparation.selectionUnavailable") : undefined}
              onClick={() => void save()}
              testId="v2-assistant-prep-save"
            >
              {t(working ? "assistantPreparation.saving" : "assistantPreparation.save")}
            </Button>
          </div>
        )}
      </div>
      {!locked && creating && (
        <KnowledgeBaseCreateDrawer
          workspaceId={workspaceId}
          onCreated={() => { if (alive.current) void refresh(); }}
          onClose={() => setCreating(false)}
        />
      )}
    </section>
  );
}

/** Read-only: selecting an existing KB never starts ingestion. */
function KbReadiness({ kbId, name, workspaceId }: { kbId: string; name: string; workspaceId: string }) {
  const { t } = useTranslation();
  const [detail, setDetail] = useState<AssistantKbDetail | null>(null);
  const [failed, setFailed] = useState(false);
  useEffect(() => {
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout>;
    const read = async () => {
      try {
        const current = await api.getKnowledgeBase(kbId, workspaceId);
        if (cancelled) return;
        setDetail(current);
        setFailed(false);
        if (current.status === "CREATING" || current.data_sources.some((source) =>
          source.status === "CREATING" || source.ingestion_jobs?.some((job) =>
            ["STARTING", "IN_PROGRESS", "STOPPING"].includes(job.status)))) {
          timer = setTimeout(() => void read(), 5000);
        }
      } catch {
        if (!cancelled) setFailed(true);
      }
    };
    void read();
    return () => { cancelled = true; clearTimeout(timer); };
  }, [kbId, workspaceId]);
  const { key } = kbReadiness(detail, failed);
  const tone = key === "indexComplete" ? "green" : key === "indexFailed" ? "red" : key === "indexPending" ? "blue" : "gray";
  return (
    <div className="v2-row" style={{ fontSize: 13 }} data-testid={`v2-assistant-kb-readiness-${kbId}`}>
      <b>{name}</b>
      <Tag tone={tone}>{t(`assistantPreparation.${key}`)}</Tag>
      <Link to={`/knowledge-bases?view=detail&kb=${encodeURIComponent(kbId)}`} target="_blank" rel="noopener noreferrer">
        {t("assistantPreparation.openKb")}
      </Link>
    </div>
  );
}
