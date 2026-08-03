import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useNavigate, useSearchParams } from "react-router-dom";
import type { Edge, Node } from "@xyflow/react";

import { useAuth } from "../auth/auth-context";
import {
  AdminRequired,
  Btn,
  Chip,
  ConfirmDialog,
  LaunchSequence,
  Panel,
  useToast,
  ViewHead,
} from "../components";
import type { AgentInfo, AgentSpecInput, DeploymentInfo, JobInfo } from "../lib/api";
import { api, ApiError } from "../lib/api";
import { ChatDrawer } from "../studio/ChatDrawer";
import { CodePanel } from "../studio/CodePanel";
import { FlowEditor } from "../studio/FlowEditor";
import { NodePalette } from "../studio/NodePalette";
import { PropertyPanel } from "../studio/PropertyPanel";
import { generateStrandsAgentCode } from "../studio/lib/code-generator";
import type { FlowData } from "../studio/lib/debug-client";
import { MANTLE_PROVIDER } from "../studio/lib/models";
import { SampleGallery } from "../studio/SampleGallery";
import type { SampleFlow } from "../studio/lib/sample-flows";

const DRAFT_KEY = "launchpad_studio_draft";
const NAME_RE = /^[a-z][a-z0-9-]{2,47}$/;
const MAX_CODE = 200000;
const FALLBACK_PROMPT = "Strands Studio generated agent";

interface StudioFlow {
  nodes: Node[];
  edges: Edge[];
  graphMode: boolean;
}

// Lifted code state: while `source==='template'` the code tracks the canvas;
// once an AI fix is applied (`source==='ai'`) the code is locked against the
// canvas and `flowStale` flags that the graph has since drifted from it.
type CodeSource = "template" | "ai";
interface CodeState {
  code: string;
  source: CodeSource;
  flowStale: boolean;
}

type Drawer = "code" | "chat" | null;

// Mirror of the generator's findConnectedAgent: the execution agent is the
// agent/orchestrator/swarm reached from an input node, else the first one.
function executionAgent(nodes: Node[], edges: Edge[]): Node | null {
  const isExec = (n: Node) =>
    n.type === "agent" || n.type === "orchestrator-agent" || n.type === "swarm";
  for (const input of nodes.filter((n) => n.type === "input")) {
    for (const edge of edges.filter((e) => e.source === input.id)) {
      const target = nodes.find((n) => n.id === edge.target);
      if (target && isExec(target)) return target;
    }
  }
  return nodes.find(isExec) ?? null;
}

export function CreateAgentStudio() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const toast = useToast();
  const navigate = useNavigate();
  const [params] = useSearchParams();
  const editAgentId = params.get("agent");
  const editing = !!editAgentId;

  const [nodes, setNodes] = useState<Node[]>([]);
  const [edges, setEdges] = useState<Edge[]>([]);
  const [graphMode, setGraphMode] = useState(false);
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [drawer, setDrawer] = useState<Drawer>(null);
  const [showSamples, setShowSamples] = useState(false);
  const [pendingSample, setPendingSample] = useState<SampleFlow | null>(null);

  // Lifted code state (template-generated vs AI-fixed) — drives the code drawer,
  // the local-debug run/chat drawers, and the publish body.
  const [codeState, setCodeState] = useState<CodeState>({
    code: "",
    source: "template",
    flowStale: false,
  });
  // Keep the region mounted once opened so the chat session survives tab
  // switches and drawer close (the backend session is in-memory).
  const drawerOpenedRef = useRef(false);
  if (drawer) drawerOpenedRef.current = true;

  // Edit-mode state
  const [editAgent, setEditAgent] = useState<AgentInfo | null>(null);
  const [noFlowNotice, setNoFlowNotice] = useState(false);
  const [readonlyCode, setReadonlyCode] = useState<string | null>(null);

  // Publish / launch state
  const [publishOpen, setPublishOpen] = useState(false);
  const [publishName, setPublishName] = useState("");
  const [publishErr, setPublishErr] = useState<string | null>(null);
  const [confirmClear, setConfirmClear] = useState(false);
  const [launch, setLaunch] = useState<{ agentId: string; jobId: string } | null>(null);
  const [deployment, setDeployment] = useState<DeploymentInfo | null>(null);
  const [job, setJob] = useState<JobInfo | null>(null);
  const [agentStatus, setAgentStatus] = useState("deploying");

  const selectedNode = selectedId ? (nodes.find((n) => n.id === selectedId) ?? null) : null;

  // Live code generation drives the drawer + the publish summary/guards.
  const genResult = useMemo(
    () => generateStrandsAgentCode(nodes, edges, graphMode),
    [nodes, edges, graphMode],
  );
  const fullCode = genResult.imports.join("\n") + "\n\n" + genResult.code;

  // ── lifted code-state machine (canvas ↔ template ↔ AI-fixed) ──
  const codeSourceRef = useRef<CodeSource>(codeState.source);
  codeSourceRef.current = codeState.source;
  useEffect(() => {
    if (codeSourceRef.current === "template") {
      setCodeState({ code: fullCode, source: "template", flowStale: false });
    } else {
      // AI-fixed code is locked against the canvas; flag drift, don't overwrite.
      setCodeState((prev) => (prev.flowStale ? prev : { ...prev, flowStale: true }));
    }
  }, [fullCode]);

  // Applied by the AI-fix flow (execution/chat drawers). Always applies here —
  // there is no manual editing in the native canvas, so nothing to overwrite.
  const applyFixedCode = useCallback((code: string): boolean => {
    setCodeState((prev) => ({ code, source: "ai", flowStale: prev.flowStale }));
    return true;
  }, []);

  const regenerateFromFlow = () => {
    setCodeState({ code: fullCode, source: "template", flowStale: false });
  };

  // OpenAI and Mantle both import `openai` at the top level (only in the
  // [openai] extra); the extra also pulls the Bedrock token generator Mantle
  // auth needs. Caching / thinking / skills need no extra requirement.
  const extraReqs = useMemo(
    () =>
      nodes.some((n) => {
        const p = (n.data as { modelProvider?: string })?.modelProvider;
        return p === "OpenAI" || p === MANTLE_PROVIDER;
      })
        ? ["strands-agents[openai]"]
        : [],
    [nodes],
  );

  // Generated code reads OPENAI_API_KEY / BEDROCK_API_KEY from the runtime env.
  // Map the first non-empty apiKey per provider onto spec.env; flag when a node
  // needs a key but none was entered (publish is still allowed — the key may be
  // provisioned another way).
  //
  // Mantle nodes never count as missing a key: without one, codegen emits
  // `bedrock_mantle_config` and the SDK mints a bearer token from the runtime
  // execution role per request. Only OpenAI nodes genuinely need a secret.
  const { env: publishEnv, missingApiKey } = useMemo(() => {
    let openaiKey = "";
    let bedrockKey = "";
    let needsOpenaiKey = false;
    for (const n of nodes) {
      const d = n.data as { modelProvider?: string; apiKey?: string };
      const key = (d?.apiKey ?? "").trim();
      if (d?.modelProvider === "OpenAI") {
        needsOpenaiKey = true;
        if (key && !openaiKey) openaiKey = key;
      } else if (d?.modelProvider === MANTLE_PROVIDER) {
        if (key && !bedrockKey) bedrockKey = key;
      }
    }
    const env: Record<string, string> = {};
    if (openaiKey) env.OPENAI_API_KEY = openaiKey;
    if (bedrockKey) env.BEDROCK_API_KEY = bedrockKey;
    return { env, missingApiKey: needsOpenaiKey && !openaiKey };
  }, [nodes]);

  // Flow graph + API keys handed to the local-debug drawers (run/chat/fix).
  const flowData = useMemo<FlowData>(
    () => ({
      nodes: nodes as unknown as Record<string, unknown>[],
      edges: edges as unknown as Record<string, unknown>[],
    }),
    [nodes, edges],
  );
  const debugApiKeys = useMemo(
    () => ({ openai_api_key: publishEnv.OPENAI_API_KEY, bedrock_api_key: publishEnv.BEDROCK_API_KEY }),
    [publishEnv],
  );

  // ── edit mode: load the stored flow (or fall back for external-app agents) ──
  useEffect(() => {
    if (!editAgentId) return;
    let cancelled = false;
    api
      .getAgent(editAgentId)
      .then((agent) => {
        if (cancelled) return;
        if (agent.method !== "studio") {
          toast(t("studio.toast.notStudioAgent"));
          navigate("/create");
          return;
        }
        setEditAgent(agent);
        setPublishName(agent.name);
        const spec = (agent.spec ?? {}) as { studio_flow?: StudioFlow; code?: string };
        const flow = spec.studio_flow;
        if (flow && Array.isArray(flow.nodes) && flow.nodes.length > 0) {
          setNodes(flow.nodes);
          setEdges(Array.isArray(flow.edges) ? flow.edges : []);
          setGraphMode(!!flow.graphMode);
        } else {
          // Studio agent published by the standalone app: no canvas graph stored.
          setNoFlowNotice(true);
          if (typeof spec.code === "string") {
            setReadonlyCode(spec.code);
            setDrawer("code");
          }
        }
      })
      .catch(() => {
        if (cancelled) return;
        toast(t("studio.toast.loadFailed"));
        navigate("/create");
      });
    return () => {
      cancelled = true;
    };
  }, [editAgentId, navigate, toast, t]);

  // ── new-agent mode: restore then autosave a localStorage draft ──
  const restoredRef = useRef(false);
  useEffect(() => {
    if (editing || restoredRef.current) return;
    restoredRef.current = true;
    try {
      const raw = localStorage.getItem(DRAFT_KEY);
      if (!raw) return;
      const draft = JSON.parse(raw) as Partial<StudioFlow>;
      if (Array.isArray(draft.nodes)) setNodes(draft.nodes);
      if (Array.isArray(draft.edges)) setEdges(draft.edges);
      setGraphMode(!!draft.graphMode);
    } catch {
      /* corrupt draft — start clean */
    }
  }, [editing]);

  useEffect(() => {
    if (editing) return;
    const timer = setTimeout(() => {
      try {
        localStorage.setItem(DRAFT_KEY, JSON.stringify({ nodes, edges, graphMode }));
      } catch {
        /* storage full/disabled — draft is best-effort */
      }
    }, 500);
    return () => clearTimeout(timer);
  }, [nodes, edges, graphMode, editing]);

  // ── deploy polling (identical shape to the wizard's step 3) ──
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
          t("studio.toast.publishFailed", {
            stage: failedStage?.name ?? "deploy",
            msg: (failedStage?.detail ?? "").slice(0, 120),
          }),
        );
      }
      setAgentStatus(agent.status);
    } catch {
      /* transient poll errors retry on the next tick */
    }
  }, [launch, toast, t]);

  useEffect(() => {
    if (!launch) return;
    void poll();
    if (agentStatus === "active" || agentStatus === "failed") return;
    const timer = setInterval(() => void poll(), 2000);
    return () => clearInterval(timer);
  }, [launch, agentStatus, poll]);

  const onUpdateNode = useCallback((nodeId: string, data: Record<string, unknown>) => {
    // PropertyPanel already merges {...node.data, [field]: value}, so replace.
    setNodes((prev) => prev.map((n) => (n.id === nodeId ? { ...n, data } : n)));
  }, []);

  const clearCanvas = () => {
    setNodes([]);
    setEdges([]);
    setSelectedId(null);
    setConfirmClear(false);
    try {
      localStorage.removeItem(DRAFT_KEY);
    } catch {
      /* ignore */
    }
  };

  const applySample = (sample: SampleFlow) => {
    setNodes(sample.nodes);
    setEdges(sample.edges);
    setGraphMode(sample.graphMode);
    setSelectedId(null);
    setShowSamples(false);
    setPendingSample(null);
    toast(t("studio.samples.loaded", { name: sample.name }));
  };

  const onLoadSample = (sample: SampleFlow) => {
    // Replacing a non-empty canvas is destructive — confirm first.
    if (nodes.length > 0 || edges.length > 0) {
      setPendingSample(sample);
    } else {
      applySample(sample);
    }
  };

  // AI-fixed code is validated by the fix pipeline, so the generation-error
  // gate only applies while the code still tracks the canvas (source template).
  const publishBlockedByErrors = codeState.source === "template" && genResult.errors.length > 0;

  const openPublish = () => {
    if (publishBlockedByErrors) {
      toast(t("studio.toast.fixErrors"));
      setDrawer("code");
      return;
    }
    setPublishErr(null);
    setPublishOpen(true);
  };

  const doPublish = async () => {
    setPublishErr(null);
    if (publishBlockedByErrors) {
      setPublishErr(t("studio.publish.errHasErrors"));
      return;
    }
    if (codeState.code.length > MAX_CODE) {
      setPublishErr(
        t("studio.publish.errTooLarge", {
          size: codeState.code.length.toLocaleString(),
          limit: MAX_CODE.toLocaleString(),
        }),
      );
      return;
    }
    const exec = executionAgent(nodes, edges);
    const rawPrompt = (exec?.data as { systemPrompt?: string })?.systemPrompt;
    const systemPrompt = (rawPrompt?.trim() ? rawPrompt : FALLBACK_PROMPT).slice(0, 20000);
    const name = editing ? (editAgent?.name ?? publishName) : publishName;
    const spec: AgentSpecInput = {
      name,
      method: "studio",
      system_prompt: systemPrompt,
      code: codeState.code,
      memory: { short_term: false, long_term: false },
      studio_flow: { nodes, edges, graphMode },
      ...(extraReqs.length ? { requirements: extraReqs } : {}),
      ...(Object.keys(publishEnv).length ? { env: publishEnv } : {}),
    };
    try {
      const res =
        editing && editAgent
          ? await api.redeployAgent(editAgent.id, spec)
          : await api.createAgent(spec);
      failureToasted.current = false;
      if (!editing) {
        try {
          localStorage.removeItem(DRAFT_KEY);
        } catch {
          /* ignore */
        }
      }
      setPublishOpen(false);
      setLaunch({ agentId: res.agent.id, jobId: res.job_id });
      setAgentStatus("deploying");
    } catch (err) {
      setPublishErr(err instanceof ApiError ? err.message : String(err));
    }
  };

  const nameValid = editing || NAME_RE.test(publishName);
  const canPublish = nodes.length > 0;
  const showReadonly = noFlowNotice && nodes.length === 0 && readonlyCode !== null;
  // Local chat needs a flow that generates valid code (or an already-applied fix).
  const canChatLocally = !showReadonly && nodes.length > 0 && !publishBlockedByErrors;

  // ── launch view (replaces the canvas once a publish is in flight) ──
  if (launch) {
    return (
      <section>
        <ViewHead
          kicker={t("studio.head.kicker")}
          title={
            editing
              ? t("studio.head.republishingTitle", { name: editAgent?.name ?? "" })
              : t("studio.head.publishingTitle")
          }
          meta={t("studio.head.publishingMeta")}
        />
        <LaunchSequence
          deployment={deployment}
          job={job}
          agentStatus={agentStatus}
          detailsMode={false}
          onRestart={() => navigate("/create")}
        />
        {agentStatus === "active" && (
          <>
            <div style={{ height: 14 }} />
            <Panel title={t("studio.published.title")} sub={t("studio.published.sub")}>
              <div style={{ display: "flex", gap: 10 }}>
                <Link className="btn primary" to={`/chat?agent=${launch.agentId}`}>
                  {t("studio.published.openChat")} ▸
                </Link>
                <Link className="btn" to="/create">
                  {t("studio.published.backToAgents")}
                </Link>
              </div>
            </Panel>
          </>
        )}
      </section>
    );
  }

  // The canvas exists to publish agents, and local debug is administrator-only
  // too (it runs generated code on the server), so gate the whole surface.
  if (!isAdmin) {
    return (
      <AdminRequired kicker={t("studio.head.kicker")} title={t("studio.head.title")} />
    );
  }

  return (
    <section>
      <ViewHead
        kicker={t("studio.head.kicker")}
        title={
          editing
            ? t("studio.head.titleEdit", {
                name: editAgent?.name ?? t("studio.head.editFallback"),
              })
            : t("studio.head.title")
        }
        meta={t("studio.head.meta")}
      />

      <div
        style={{
          display: "flex",
          alignItems: "center",
          gap: 10,
          marginBottom: 14,
          flexWrap: "wrap",
        }}
      >
        <Link className="btn" to="/create">
          ◂ {t("studio.toolbar.agents")}
        </Link>
        {editing && (
          <Chip tone="muted" icon="⟳">
            {t("studio.toolbar.rePublishChip", { name: editAgent?.name ?? "" })}
          </Chip>
        )}
        <div style={{ marginLeft: "auto", display: "flex", gap: 10, flexWrap: "wrap" }}>
          <Btn onClick={() => setShowSamples(true)}>▦ {t("studio.toolbar.samples")}</Btn>
          <Btn onClick={() => setDrawer((v) => (v === "code" ? null : "code"))}>
            {drawer === "code" ? t("studio.toolbar.hideCode") : t("studio.toolbar.generateCode")}
          </Btn>
          {!showReadonly && (
            <Btn
              onClick={() => setDrawer("chat")}
              disabled={!canChatLocally}
              title={canChatLocally ? undefined : t("studio.toolbar.debugDisabled")}
            >
              ◈ {t("studio.toolbar.localChat")}
            </Btn>
          )}
          {codeState.source === "ai" && (
            <Btn onClick={regenerateFromFlow} title={t("studio.toolbar.regenHint")}>
              ⟲ {t("studio.toolbar.regen")}
            </Btn>
          )}
          <Btn onClick={() => setConfirmClear(true)} disabled={nodes.length === 0 && edges.length === 0}>
            {t("studio.toolbar.clearCanvas")}
          </Btn>
          <Btn primary onClick={openPublish} disabled={!canPublish}>
            ▲ {editing ? t("studio.toolbar.rePublish") : t("studio.toolbar.publish")}
          </Btn>
        </div>
      </div>

      {noFlowNotice && (
        <div className="note" style={{ borderColor: "var(--amber)", marginBottom: 14 }}>
          <span className="i" style={{ color: "var(--amber)" }}>
            [i]
          </span>
          <span>{t("studio.noFlow.notice")}</span>
        </div>
      )}

      <div className={`studio-layout${selectedNode ? "" : " no-prop"}`}>
        <NodePalette />
        <div className="studio-canvas">
          <FlowEditor
            nodes={nodes}
            onNodesChange={setNodes}
            edges={edges}
            onEdgesChange={setEdges}
            graphMode={graphMode}
            onGraphModeChange={setGraphMode}
            onNodeSelect={(node) => setSelectedId(node?.id ?? null)}
            onInvalidConnection={(message) => toast(message)}
          />
        </div>
        {selectedNode && (
          <PropertyPanel
            selectedNode={selectedNode}
            onClose={() => setSelectedId(null)}
            onUpdateNode={onUpdateNode}
            nodes={nodes}
            edges={edges}
          />
        )}
      </div>

      {showReadonly
        ? drawer === "code" && (
            <>
              <div style={{ height: 14 }} />
              <Panel title={t("studio.readonly.title")} sub={t("studio.readonly.sub")}>
                <pre className="code" style={{ maxHeight: 420, overflow: "auto", margin: 0 }}>
                  {readonlyCode}
                </pre>
              </Panel>
            </>
          )
        : (drawer !== null || drawerOpenedRef.current) && (
            <>
              <div style={{ height: 14 }} />
              <div className="studio-debug" style={{ display: drawer ? undefined : "none" }}>
                <div className="studio-debug-tabs">
                  <button
                    className={`studio-debug-tab${drawer === "code" ? " on" : ""}`}
                    onClick={() => setDrawer("code")}
                  >
                    {t("studio.debug.tabCode")}
                  </button>
                  <button
                    className={`studio-debug-tab${drawer === "chat" ? " on" : ""}`}
                    onClick={() => setDrawer("chat")}
                  >
                    {t("studio.debug.tabChat")}
                  </button>
                  <button
                    className="studio-debug-x"
                    onClick={() => setDrawer(null)}
                    title={t("common.close")}
                  >
                    ✕
                  </button>
                </div>
                <div className="studio-debug-pane" hidden={drawer !== "code"}>
                  <CodePanel
                    code={codeState.code}
                    errors={genResult.errors}
                    source={codeState.source}
                    flowStale={codeState.flowStale}
                  />
                </div>
                <div className="studio-debug-pane" hidden={drawer !== "chat"}>
                  <ChatDrawer
                    active={drawer === "chat"}
                    code={codeState.code}
                    flowData={flowData}
                    graphMode={graphMode}
                    apiKeys={debugApiKeys}
                    onApplyFixedCode={applyFixedCode}
                  />
                </div>
              </div>
            </>
          )}

      {publishOpen && (
        <div className="confirm-backdrop" onClick={() => setPublishOpen(false)}>
          <div
            className="confirm-box"
            role="dialog"
            aria-modal="true"
            aria-label={t("studio.publish.title")}
            style={{ maxWidth: 520, width: "92%" }}
            onClick={(e) => e.stopPropagation()}
          >
            <div className="confirm-title">
              ▲ {editing ? t("studio.publish.rePublishTitle") : t("studio.publish.title")}
            </div>

            {editing ? (
              <div className="field">
                <label>{t("studio.publish.nameLabel")}</label>
                <input className="input mono" value={editAgent?.name ?? publishName} disabled />
              </div>
            ) : (
              <div className="field">
                <label htmlFor="studio-name">{t("studio.publish.agentName")}</label>
                <input
                  id="studio-name"
                  className="input mono"
                  value={publishName}
                  onChange={(e) => setPublishName(e.target.value)}
                  placeholder={t("studio.publish.namePlaceholder")}
                  autoFocus
                />
                {publishName.length > 0 && !nameValid && (
                  <div className="studio-warn" style={{ color: "var(--crit)" }}>
                    {t("studio.publish.nameRule")}
                  </div>
                )}
              </div>
            )}

            <div className="kv">
              <span className="k">{t("studio.publish.generatedCode")}</span>
              <span className="v">
                {t("studio.publish.codeStat", {
                  chars: codeState.code.length.toLocaleString(),
                  lines: codeState.code.split("\n").length,
                })}
              </span>
            </div>
            <div className="kv">
              <span className="k">{t("studio.publish.extraReqs")}</span>
              <span className="v">
                {extraReqs.length ? extraReqs.join(", ") : t("studio.publish.none")}
              </span>
            </div>
            <div className="kv">
              <span className="k">{t("studio.publish.memory")}</span>
              <span className="v">{t("studio.publish.memoryValue")}</span>
            </div>

            {codeState.source === "ai" && (
              <div
                className="note"
                style={{
                  borderColor: codeState.flowStale ? "var(--amber)" : "var(--s1)",
                  marginTop: 12,
                }}
              >
                <span
                  className="i"
                  style={{ color: codeState.flowStale ? "var(--amber)" : "var(--s1)" }}
                >
                  [✦]
                </span>
                <span>
                  {codeState.flowStale
                    ? t("studio.publish.aiFixedStale")
                    : t("studio.publish.aiFixed")}
                </span>
              </div>
            )}

            {missingApiKey && (
              <div className="note" style={{ borderColor: "var(--amber)", marginTop: 12 }}>
                <span className="i" style={{ color: "var(--amber)" }}>
                  [i]
                </span>
                <span>{t("studio.publish.missingApiKey")}</span>
              </div>
            )}

            {publishErr && (
              <div className="note" style={{ borderColor: "var(--crit)", marginTop: 12 }}>
                <span className="i" style={{ color: "var(--crit)" }}>
                  [✕]
                </span>
                <span>{publishErr}</span>
              </div>
            )}

            <div className="confirm-actions" style={{ marginTop: 16 }}>
              <Btn onClick={() => setPublishOpen(false)}>{t("common.cancel")}</Btn>
              <Btn primary disabled={!nameValid} onClick={() => void doPublish()}>
                ▲ {editing ? t("studio.toolbar.rePublish") : t("studio.toolbar.publish")}
              </Btn>
            </div>
          </div>
        </div>
      )}

      <ConfirmDialog
        open={confirmClear}
        title={t("studio.confirmClear.title")}
        body={t("studio.confirmClear.body")}
        confirmLabel={t("studio.confirmClear.confirm")}
        onConfirm={clearCanvas}
        onCancel={() => setConfirmClear(false)}
      />

      {showSamples && (
        <SampleGallery onClose={() => setShowSamples(false)} onLoadSample={onLoadSample} />
      )}

      <ConfirmDialog
        open={!!pendingSample}
        title={t("studio.samples.replaceTitle")}
        body={t("studio.samples.replaceBody")}
        confirmLabel={t("studio.samples.replaceConfirm")}
        onConfirm={() => pendingSample && applySample(pendingSample)}
        onCancel={() => setPendingSample(null)}
      />
    </section>
  );
}
