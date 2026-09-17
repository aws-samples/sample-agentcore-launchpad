import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Btn, Chip, LoadError, Panel } from "../../components";
import {
  api, ApiError, errorMessage,
  type AssistantCatalog, type AssistantCatalogTool, type AssistantConversationDetail,
  type AttachableKnowledgeBase,
} from "../../lib/api";
import { KnowledgeBaseCreate } from "./KnowledgeBaseCreate";
import { KnowledgeBaseReadiness } from "./KnowledgeBaseReadiness";

interface Props {
  conversation: AssistantConversationDetail;
  workspaceId: string;
  disabled: boolean;
  locked: boolean;
  onDirty: (dirty: boolean) => void;
  onUpdated: (detail: AssistantConversationDetail) => void;
  onCatalog: (catalog: AssistantCatalog) => void;
  onWorking: (working: boolean) => void;
  onDiscuss: (text: string) => void;
}

const sameKeys = (a: string[], b: string[]) =>
  a.length === b.length && a.every((key) => b.includes(key));

const MAX_TOOLS = 20;

export function PreparationPanel({
  conversation, workspaceId, disabled, locked, onDirty, onUpdated, onCatalog, onWorking, onDiscuss,
}: Props) {
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
  const availableSkillKeys = new Set(
    catalog.skills.filter((skill) => skill.content_digest).map((skill) => skill.key),
  );
  const availableToolKeys = new Set(
    catalog.tools.filter((tool) => tool.attachable).map((tool) => tool.key),
  );
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
    // Approval in another tab wins over a local draft: the locked view must show
    // the approved resources, never unsaved choices that cannot be applied.
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

  // Catalog reads stay pinned even when another tab changes the shared workspace.
  // Failures retain the previous successful rows and never erase saved selections.
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
      if (catalogResult.value.conversation)
        callbacks.current.onUpdated(catalogResult.value.conversation);
      else callbacks.current.onCatalog(catalogResult.value.catalog);
    }
    else setActionError(errorMessage(catalogResult.reason));
    setRefreshing(false);
    callbacks.current.onWorking(false);
  }, [conversation.id, workspaceId]);

  useEffect(() => {
    // Opening history is read-only. Refreshing the reviewed catalog is explicit:
    // it may create a new proposal revision when a binding changed.
    let cancelled = false;
    void api.listAttachableKnowledgeBases(workspaceId).then(
      (res) => { if (!cancelled) setKbs(res.items); },
      (err: unknown) => { if (!cancelled) setKbError(errorMessage(err)); },
    );
    return () => { cancelled = true; };
  }, [workspaceId]);

  const start = () => {
    setWorking(true);
    setActionError(null);
    setNotice(null);
    callbacks.current.onWorking(true);
  };
  const finish = () => {
    if (!alive.current) return;
    setWorking(false);
    callbacks.current.onWorking(false);
  };
  const recoverConflict = async (err: unknown) => {
    if (!alive.current) return;
    setActionError(describeError(err));
    if (err instanceof ApiError && (err.code.includes("stale") || err.code === "assistant.resources_locked")) {
      try {
        const detail = await api.assistantConversation(conversation.id, workspaceId);
        if (alive.current) callbacks.current.onUpdated(detail);
      } catch { /* retain the original conflict and offer a manual refresh */ }
    }
  };
  const save = async () => {
    if (selectionDisabled) return;
    start();
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
      await recoverConflict(err);
    } finally {
      finish();
    }
  };
  const toggle = (values: string[], value: string) =>
    values.includes(value) ? values.filter((item) => item !== value) : [...values, value];
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

  return (
    <Panel brk id="assistant-resources" title={t("assistantPreparation.title")} sub={t("assistantPreparation.subtitle")}
      data-testid="assistant-preparation"
      end={<Btn disabled={busy} onClick={() => void refresh()} data-testid="preparation-refresh">
        {t(refreshing ? "assistantPreparation.refreshing" : "assistantPreparation.refresh")}
      </Btn>}>
      <div className="assist-preparation">
        <div className="assist-resource-lifecycle" data-testid="preparation-lifecycle" role="status">
          <strong>{t(locked ? "assistantPreparation.lockedTitle" : "assistantPreparation.beforeCreateTitle")}</strong>
          <p>{t(locked ? "assistantPreparation.lockedHint" : "assistantPreparation.beforeCreateHint")}</p>
          {locked && <Link className="btn" to="/create" data-testid="preparation-edit-agent">
            {t("assistantPreparation.manageAgent")}
          </Link>}
        </div>
        {!locked && (prep?.requirements ?? []).length > 0 && (
          <div className="assist-prep-requirements" data-testid="preparation-requirements">
            <div className="dim">{t("assistantPreparation.requirements")}</div>
            {prep!.requirements.map((item) => (
              <div className="assist-prep-requirement" key={item.id}>
                <div className="row">
                  <b>{item.title}</b>
                  <Chip tone={item.required ? "warn" : "muted"}>
                    {t(item.required ? "assistantPreparation.required" : "assistantPreparation.optional")}
                  </Chip>
                </div>
                <p>{item.reason}</p>
                {item.materials.length > 0 && <ul>{item.materials.map((material, index) =>
                  <li key={index}>{material}</li>)}</ul>}
                <a href={item.kind === "knowledge_base" ? "#preparation-kbs"
                  : item.kind === "skill" ? "#preparation-skills"
                    : item.kind === "tool" ? "#preparation-tools" : "#assistant-input"}
                onClick={() => {
                  if (item.kind === "clarification")
                    onDiscuss(t("assistantPreparation.discussPrompt", { title: item.title }));
                }}>{t(item.kind !== "clarification"
                  ? "assistantPreparation.prepare" : "assistantPreparation.discuss")}</a>
              </div>
            ))}
          </div>
        )}
        <section id="preparation-kbs" aria-labelledby="preparation-kbs-title">
          <h3 id="preparation-kbs-title">{t("assistantPreparation.knowledgeTitle")}</h3>
          <p className="dim">{t("assistantPreparation.knowledgeHint")}</p>
          {kbError && <LoadError message={kbError} onRetry={() => void refresh()} inline />}
          {kbs === null && !kbError && <p>{t("common.loading")}</p>}
          {kbs?.length === 0 && !kbError && <p>{t("assistantPreparation.noKbs")}</p>}
          {!gatewayReady && <p className="note">{t("assistantPreparation.gatewayMissing")}</p>}
          <div className="assist-prep-options" data-testid="preparation-kb-options">
            {kbRows.map((kb) => {
              const selected = selectedKbs.includes(kb.kb_id);
              const available = availableKbIds.has(kb.kb_id) && gatewayReady
                && (!("status" in kb) || !kb.status || kb.status === "ACTIVE");
              return (
                <label className={`assist-prep-option${selected ? " selected" : ""}`} key={kb.kb_id}>
                  <input type="checkbox" checked={selected}
                    disabled={selectionDisabled || (!selected && !available)}
                    onChange={() => setSelectedKbs(toggle(selectedKbs, kb.kb_id))} />
                  <span><b>{kb.name}</b>
                    {kb.description && <span className="dim">{kb.description}</span>}
                    <span className="dim mono">{kb.kb_id}</span>
                  </span>
                  <Chip tone={available ? "good" : "muted"}>
                    {available ? t("assistantPreparation.mountable")
                      : ("status" in kb && kb.status && kb.status !== "ACTIVE"
                        ? t(`knowledge.status.${kb.status.toLowerCase()}`, kb.status)
                        : t("assistantPreparation.unavailable"))}
                  </Chip>
                </label>
              );
            })}
          </div>
          {selectedKbs.map((kbId) => <KnowledgeBaseReadiness
            key={`${kbId}:${catalog.fetched_at}`} kbId={kbId}
            name={kbRows.find((kb) => kb.kb_id === kbId)?.name ?? kbId}
            workspaceId={workspaceId} />)}
          <p className="dim">{t("assistantPreparation.indexHint")}</p>
          {!locked && <div className="row">
            <Btn disabled={busy || creating} onClick={() => setCreating(true)}
              data-testid="preparation-create-kb">{t("assistantPreparation.newKb")}</Btn>
            <Link className="btn" to="/knowledge-bases"
              target="_blank" rel="noopener noreferrer" data-testid="preparation-manage-kbs">
              {t("assistantPreparation.manageKbs")}
            </Link>
          </div>}
          {!locked && creating && <KnowledgeBaseCreate workspaceId={workspaceId}
            onCreated={() => { if (alive.current) void refresh(); }}
            onClose={() => setCreating(false)} />}
        </section>
        <section id="preparation-skills" aria-labelledby="preparation-skills-title">
          <h3 id="preparation-skills-title">{t("assistantPreparation.skillsTitle")}</h3>
          <p className="dim">{t("assistantPreparation.skillsHint")}</p>
          {skillRows.length === 0 && catalog.warnings.length === 0
            && <p>{t("assistantPreparation.noSkills")}</p>}
          <div className="assist-prep-options">
            {skillRows.map((skill) => {
              const selected = selectedSkills.includes(skill.key);
              const available = availableSkillKeys.has(skill.key);
              return (
                <label className={`assist-prep-option${selected ? " selected" : ""}`} key={skill.key}>
                  <input type="checkbox" checked={selected}
                    disabled={selectionDisabled || (!selected && !available)}
                    onChange={() => setSelectedSkills(toggle(selectedSkills, skill.key))} />
                  <span><b>{skill.name}</b><span className="dim">{skill.description}</span></span>
                  {!available && <Chip tone="warn">{t("assistantPreparation.unavailable")}</Chip>}
                </label>
              );
            })}
          </div>
          {!locked && <div className="row">
            <Btn disabled={busy} onClick={() => void refresh()}
              data-testid="preparation-refresh-skills">{t("assistantPreparation.refresh")}</Btn>
            <Link className="btn" to="/registry?view=register&type=AGENT_SKILLS"
              target="_blank" rel="noopener noreferrer" data-testid="preparation-create-skill">
              {t("assistantPreparation.createSkill")}
            </Link>
          </div>}
          {!locked && <p className="dim">{t("assistantPreparation.registryHint")}</p>}
        </section>
        <section id="preparation-tools" aria-labelledby="preparation-tools-title">
          <h3 id="preparation-tools-title">{t("assistantPreparation.toolsTitle")}</h3>
          <p className="dim">{t("assistantPreparation.toolsHint")}</p>
          {toolRows.length === 0 && catalog.warnings.length === 0
            && <p>{t("assistantPreparation.noTools")}</p>}
          <div className="assist-prep-options" data-testid="preparation-tool-options">
            {toolRows.map((tool) => {
              const selected = selectedTools.includes(tool.key);
              const knownTools = tool.runtime_tools;
              return (
                <div className="assist-prep-tool" key={tool.key}>
                  <label className={`assist-prep-option${selected ? " selected" : ""}`}>
                    <input type="checkbox" checked={selected} aria-label={tool.name}
                      disabled={selectionDisabled || (!selected && (!tool.attachable || selectedTools.length >= MAX_TOOLS))}
                      onChange={() => setSelectedTools((current) => toggle(current, tool.key))} />
                    <span><b>{tool.name}</b>
                      {tool.description && <span className="dim">{tool.description}</span>}
                      {!tool.attachable && <span className="dim">
                        {tool.reason || t("assistantPreparation.toolUnavailable")}
                      </span>}
                    </span>
                    <Chip tone={tool.attachable ? "good" : "warn"}>
                      {t(tool.attachable ? "assistantPreparation.mountable" : "assistantPreparation.unavailable")}
                    </Chip>
                  </label>
                  {knownTools != null ? (
                    <details className="assist-pre" data-testid="preparation-tool-names">
                      <summary>{t("assistantPreparation.callableNames", { count: knownTools.length })}</summary>
                      {knownTools.length > 0
                        ? <ul>{knownTools.map((name) => <li key={name}><code>{name}</code></li>)}</ul>
                        : <p>{t("assistantPreparation.noCallableTools")}</p>}
                    </details>
                  ) : tool.attachable ? (
                    <p className="note">{t("assistantPreparation.toolDiscoveryUnknown")}</p>
                  ) : null}
                </div>
              );
            })}
          </div>
          <p className="dim">{t("assistantPreparation.toolsLimit", { count: selectedTools.length, max: MAX_TOOLS })}</p>
          {!locked && <div className="row">
            <Btn disabled={busy} onClick={() => void refresh()}
              data-testid="preparation-refresh-tools">{t("assistantPreparation.refresh")}</Btn>
            <Link className="btn" to="/registry?view=register&type=MCP"
              target="_blank" rel="noopener noreferrer" data-testid="preparation-create-tool">
              {t("assistantPreparation.createTool")}
            </Link>
          </div>}
          {!locked && <p className="dim">{t("assistantPreparation.toolRegistryHint")}</p>}
          <p className="note" data-testid="preparation-tool-evaluation-hint">
            {t("assistantPreparation.toolEvaluationHint")}
          </p>
        </section>
        {catalog.warnings.length > 0 && <div className="note" role="status">
          {t("assistantPreparation.catalogWarning")}
          <ul>{catalog.warnings.map((warning, index) => <li key={index}>{warning}</li>)}</ul>
        </div>}
        {actionError && <div className="note assist-prep-error" role="alert">{actionError}</div>}
        {notice && <div className="note" role="status">{notice}</div>}
        {!locked && <div className="assist-prep-save">
          <span className="dim">{t(changed ? "assistantPreparation.unsaved" : "assistantPreparation.savedHint")}</span>
          {changed && <Btn disabled={busy} onClick={() => {
            setSelectedKbs(prep?.knowledge_bases ?? []);
            setSelectedSkills(prep?.skills ?? []);
            setSelectedTools(savedTools);
            setActionError(null);
            setNotice(null);
          }}>{t("common.cancel")}</Btn>}
          <Btn primary disabled={busy || !changed || invalidSelection}
            disabledReason={!busy && changed && invalidSelection
              ? t("assistantPreparation.selectionUnavailable") : undefined}
            onClick={() => void save()} data-testid="preparation-save">
            {t(working ? "assistantPreparation.saving" : "assistantPreparation.save")}
          </Btn>
        </div>}
      </div>
    </Panel>
  );
}
