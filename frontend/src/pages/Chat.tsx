import type { CSSProperties } from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import {
  Btn,
  Chip,
  ConfirmDialog,
  LoadError,
  Markdown,
  Panel,
  StaleLink,
  useToast,
  ViewHead,
} from "../components";
import { AttachmentHint, MessageAttachments, PendingAttachments } from "../components/chat/Attachments";
import type { PendingAttachment } from "../components/chat/attachments";
import {
  attachmentMediaType,
  attachmentMetadata,
  encodeAttachment,
  validateAttachments,
} from "../components/chat/attachments";
import type {
  AgentInfo,
  ChatAttachmentMetadata,
  ChatHistoryMessage,
  ChatRequest,
  ChatSessionInfo,
} from "../lib/api";
import { api, errorMessage, localizedMessage, responseMessage } from "../lib/api";
import { chatEligible, isHarnessAgent, sseEvents } from "../lib/chat";

interface Message {
  kind: "user" | "agent" | "tool" | "memory" | "error";
  text: string;
  name?: string;
  streaming?: boolean;
  attachments?: ChatAttachmentMetadata[];
}

interface MemorySummary {
  event_count: number;
  records: { namespace: string; text: string }[];
  /** Compound `<agent_id>__<human>` partition the summary was read from — the
   *  id the Memory console keys on, so the deep link needs it verbatim. */
  actor_id?: string;
}

interface TraceSpan {
  name: string;
  category: "model" | "tool" | "memory" | "policy" | "runtime" | "other";
  start_ms: number;
  duration_ms: number | null;
}

interface TraceInfo {
  span_count: number;
  spans: TraceSpan[];
  cloudwatch_url: string;
}

const SPAN_COLOR: Record<string, string> = {
  model: "var(--s1)",
  tool: "var(--s2)",
  memory: "var(--s3)",
  policy: "var(--s5)",
  runtime: "#69736C",
  other: "#3A453F",
};

interface KeyInfo {
  id: string;
  name: string;
  prefix: string;
  enabled: boolean;
  key?: string;
}

export function Chat() {
  const { t } = useTranslation();
  const toast = useToast();
  const { authRequired, username } = useAuth();
  const userLabel = (authRequired ? (username ?? "—") : "river").toUpperCase();
  // Cross-link entry (from Observability session detail): preselect the agent
  // and resume the session; unknown values degrade to the defaults gracefully.
  const [searchParams, setSearchParams] = useSearchParams();
  const linkedAgent = searchParams.get("agent");
  const linkedSession = searchParams.get("session");
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  // set when the agent list itself failed to load — "no active agents" is
  // only claimed after a 200 answered with none
  const [agentsError, setAgentsError] = useState<string | null>(null);
  const [agentId, setAgentId] = useState<string>("");
  const [messages, setMessages] = useState<Message[]>([]);
  const [input, setInput] = useState("");
  const [attachmentDraft, setAttachmentDraft] = useState<{
    files: PendingAttachment[];
    error: string | null;
  }>({ files: [], error: null });
  const { files: pendingFiles, error: attachmentError } = attachmentDraft;
  const setAttachmentError = (error: string | null) =>
    setAttachmentDraft((draft) => ({ ...draft, error }));
  const resetAttachments = () => setAttachmentDraft({ files: [], error: null });
  const [draggingFiles, setDraggingFiles] = useState(false);
  const fileInputRef = useRef<HTMLInputElement>(null);
  const sendInFlight = useRef(false);
  const historyRequest = useRef(0);
  const [restoring, setRestoring] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(linkedSession);
  const [busy, setBusy] = useState(false);
  const [memory, setMemory] = useState<MemorySummary | null>(null);
  const [sessions, setSessions] = useState<ChatSessionInfo[]>([]);
  // session id whose END SESSION request is in flight (one at a time)
  const [ending, setEnding] = useState<string | null>(null);
  const [trace, setTrace] = useState<TraceInfo | null>(null);
  const [traceBusy, setTraceBusy] = useState(false);
  const [keys, setKeys] = useState<KeyInfo[]>([]);
  const [newKey, setNewKey] = useState<KeyInfo | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const restoredRef = useRef(false);
  // "?agent=<id>" that no active agent matches: the notice names it and the
  // picker stays on its placeholder — never silently another agent's runtime.
  const [staleAgent, setStaleAgent] = useState<string | null>(null);
  const autoPickBlocked = useRef(false);

  const loadAgents = () =>
    api
      .listAgents()
      .then((res) => {
        const active = chatEligible(res.agents);
        setAgents(active);
        setAgentsError(null);
        const linked = linkedAgent && active.find((a) => a.id === linkedAgent);
        if (linked) {
          setAgentId(linked.id);
        } else if (linkedAgent) {
          // Linked agent unknown/inactive: say so, drop the linked session too
          // (a foreign session id is never posted to a different agent's
          // runtime), strip both params, and leave the picker unselected.
          setStaleAgent(linkedAgent);
          autoPickBlocked.current = true;
          if (linkedSession) setSessionId(null);
          setSearchParams({}, { replace: true });
        } else if (active.length && !agentId && !autoPickBlocked.current) {
          setAgentId(active[0].id);
        }
      })
      .catch((err: unknown) => setAgentsError(errorMessage(err)));

  useEffect(() => {
    void loadAgents();
    void loadKeys();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  useEffect(() => {
    threadRef.current?.scrollTo({ top: threadRef.current.scrollHeight });
  }, [messages]);

  const loadKeys = async () => {
    try {
      const res = await fetch("/api/apikeys");
      if (res.ok) setKeys(((await res.json()) as { keys: KeyInfo[] }).keys);
    } catch {
      /* offline */
    }
  };

  const refreshMemory = async (sid: string) => {
    try {
      const res = await fetch(`/api/chat/${agentId}/memory?session_id=${sid}`);
      if (res.ok) setMemory((await res.json()) as MemorySummary);
    } catch {
      /* memory rail is best-effort */
    }
  };

  const loadSessions = async (aid: string) => {
    try {
      setSessions((await api.listChatSessions(aid)).sessions);
    } catch {
      /* history rail is best-effort */
    }
  };

  useEffect(() => {
    if (agentId) void loadSessions(agentId);
    else setSessions([]);
  }, [agentId]);

  const restoreSession = async (sid: string) => {
    if (!agentId || busy) return;
    const requestId = ++historyRequest.current;
    setRestoring(true);
    try {
      const res = await fetch(
        `/api/chat/${agentId}/history?session_id=${encodeURIComponent(sid)}`,
      );
      if (!res.ok) return;
      const rows = ((await res.json()) as { messages: ChatHistoryMessage[] }).messages;
      if (requestId !== historyRequest.current) return;
      setMessages(
        rows.map((r): Message =>
          r.role === "user"
            ? { kind: "user", text: r.text, attachments: r.attachments }
            : r.role === "agent"
              ? { kind: "agent", text: r.text }
              : r.role === "tool"
                ? { kind: "tool", text: r.name ?? "tool", name: r.name ?? "tool" }
                : { kind: "error", text: r.text },
        ),
      );
      setSessionId(sid);
      setInput("");
      // A successful history selection discards the previous conversation's draft.
      resetAttachments();
      setDraggingFiles(false);
      setTrace(null);
      setSearchParams({ agent: agentId, session: sid }, { replace: true });
    } catch {
      /* history rail is best-effort */
    } finally {
      if (requestId === historyRequest.current) setRestoring(false);
    }
  };

  // Reload / deep-link with a session in the URL: back-fill the thread once
  // the agent is resolved, so the conversation is visible, not just resumable.
  useEffect(() => {
    if (!restoredRef.current && agentId && sessionId && messages.length === 0) {
      restoredRef.current = true;
      void restoreSession(sessionId);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [agentId, sessionId]);

  const send = async () => {
    const prompt = input.trim();
    if ((!prompt && !pendingFiles.length) || !agentId || busy || restoring || sendInFlight.current) return;
    const capability = agents.find((a) => a.id === agentId)?.attachment_capability;
    if (pendingFiles.length) {
      const invalid = validateAttachments(pendingFiles.map((f) => f.file), capability);
      if (invalid) {
        setAttachmentError(t(`chatPage.attachments.${invalid.key}`, { ...invalid }));
        return;
      }
    }
    sendInFlight.current = true;
    setInput("");
    setAttachmentError(null);
    setDraggingFiles(false);
    setBusy(true);
    const userMessage: Message = {
      kind: "user",
      text: prompt,
      attachments: capability ? pendingFiles.map((f) => attachmentMetadata(f, capability)) : undefined,
    };
    setMessages((m) => [...m, userMessage]);
    let failed = false;
    let completed = false;
    let activeSessionId = sessionId;
    try {
      const request: ChatRequest = { prompt, session_id: sessionId };
      if (pendingFiles.length) {
        request.attachments = await Promise.all(pendingFiles.map(encodeAttachment)).catch(() => {
          throw new Error(t("chatPage.attachments.readFailed"));
        });
      }
      const res = await fetch(`/api/chat/${agentId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(request),
      });
      // Encoded bytes are request-only; release them while the response streams.
      delete request.attachments;
      if (!res.ok) throw new Error(await responseMessage(res));
      if (!res.body) throw new Error(t("chatPage.streamInterrupted"));
      let agentIdxSet = false;
      for await (const { event, data: payload } of sseEvents(res)) {
        if (event === "meta") {
          if (payload.session_id) {
            activeSessionId = payload.session_id;
            setSessionId(payload.session_id);
            // keep the session in the URL so a reload restores this conversation
            setSearchParams(
              { agent: agentId, session: payload.session_id },
              { replace: true },
            );
          }
          if (payload.attachments) {
            setMessages((m) => m.map((msg) => msg === userMessage
              ? { ...msg, attachments: payload.attachments }
              : msg));
          }
        } else if (event === "tool") {
          setMessages((m) => [
            ...m,
            { kind: "tool", text: payload.name ?? "tool", name: payload.name },
          ]);
          agentIdxSet = false;
        } else if (event === "delta") {
          setMessages((m) => {
            const next = [...m];
            const last = next[next.length - 1];
            if (agentIdxSet && last?.kind === "agent") {
              next[next.length - 1] = { ...last, text: last.text + (payload.text ?? "") };
            } else {
              next.push({ kind: "agent", text: payload.text ?? "", streaming: true });
            }
            return next;
          });
          agentIdxSet = true;
        } else if (event === "error") {
          failed = true;
          const message = localizedMessage(payload.code ?? "", payload.message ?? t("chatPage.sendFailed"));
          setMessages((m) => [...m, { kind: "error", text: message }]);
          if (pendingFiles.length) setAttachmentError(message);
        } else if (event === "done") {
          completed = true;
          if (!failed) {
            setMessages((m) => [...m, { kind: "memory", text: t("chatPage.memorySaved") }]);
          }
        }
      }
      if (!completed && !failed) throw new Error(t("chatPage.streamInterrupted"));
      if (completed && !failed) resetAttachments();
    } catch (err) {
      failed = true;
      const message = errorMessage(err);
      setMessages((m) => [...m, { kind: "error", text: message }]);
      if (pendingFiles.length) setAttachmentError(message);
    } finally {
      if (failed) setInput(prompt);
      setMessages((m) => m.map((msg) => msg.streaming ? { ...msg, streaming: false } : msg));
      sendInFlight.current = false;
      setBusy(false);
      if (activeSessionId) void refreshMemory(activeSessionId);
      if (agentId) void loadSessions(agentId);
    }
  };

  useEffect(() => {
    // agentId in deps: on a deep-linked session the agent resolves after mount
    // and the memory rail must load once it does.
    if (sessionId && agentId && !busy) void refreshMemory(sessionId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [sessionId, busy, agentId]);

  const newSession = (aid: string = agentId) => {
    if (sendInFlight.current) return;
    historyRequest.current += 1;
    setRestoring(false);
    setInput("");
    resetAttachments();
    setDraggingFiles(false);
    setSessionId(null);
    setMessages([]);
    setMemory(null);
    setTrace(null);
    setSearchParams(aid ? { agent: aid } : {}, { replace: true });
  };

  // END SESSION: terminate the live AgentCore Runtime session (not just forget
  // the id, which is all NEW SESSION does). The ledger row stays replayable.
  const endSession = async (sid: string) => {
    if (!agentId || ending) return;
    setEnding(sid);
    try {
      const result = await api.stopChatSession(agentId, sid);
      toast(t(result.already_ended ? "chatPage.endedAlready" : "chatPage.ended"), "good");
      // the ended id must not receive the next prompt — same reset as NEW SESSION
      if (sid === sessionId) newSession();
      void loadSessions(agentId);
    } catch (err) {
      toast(errorMessage(err), "crit");
    } finally {
      setEnding(null);
    }
  };

  const loadTrace = async () => {
    if (!sessionId) return;
    setTraceBusy(true);
    try {
      const res = await fetch(`/api/traces/${sessionId}`);
      if (res.ok) setTrace((await res.json()) as TraceInfo);
    } finally {
      setTraceBusy(false);
    }
  };

  const createKey = async () => {
    const res = await fetch("/api/apikeys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: `console-${keys.length + 1}` }),
    });
    if (res.ok) {
      const created = (await res.json()) as KeyInfo;
      setNewKey(created);
      void loadKeys();
    }
  };

  const toggleKey = async (key: KeyInfo) => {
    await fetch(`/api/apikeys/${key.id}/${key.enabled ? "disable" : "enable"}`, {
      method: "POST",
    });
    void loadKeys();
  };

  const [confirmKeyDisable, setConfirmKeyDisable] = useState<KeyInfo | null>(null);
  const requestToggleKey = (key: KeyInfo) => {
    if (key.enabled) setConfirmKeyDisable(key);
    else void toggleKey(key);
  };

  const agent = agents.find((a) => a.id === agentId);
  const capability = agent?.attachment_capability;
  const attachmentsEnabled = Boolean(
    capability && (capability.images || capability.text || capability.pdf !== "unsupported"),
  );
  const composerDisabled = busy || restoring || !agentId;
  const sendDisabledReason = busy || restoring
    ? undefined
    : !agentId
      ? t("chatPage.sendDisabledNoAgent")
      : !input.trim() && !pendingFiles.length
        ? t(attachmentsEnabled ? "chatPage.sendDisabledEmpty" : "chatPage.sendDisabledEmptyText")
        : undefined;
  const addFiles = (files: File[]) => {
    if (composerDisabled || !files.length) return;
    const additions = files.map((file) => ({
      id: crypto.randomUUID(),
      file,
      mediaType: attachmentMediaType(file),
    }));
    // Validate and append atomically so batched drop/paste events see prior additions.
    setAttachmentDraft((draft) => {
      const invalid = validateAttachments([...draft.files.map((f) => f.file), ...files], capability);
      return invalid
        ? { ...draft, error: t(`chatPage.attachments.${invalid.key}`, { ...invalid }) }
        : { files: [...draft.files, ...additions], error: null };
    });
  };
  const harness = isHarnessAgent(agent);
  const currentEnded = Boolean(
    sessionId && sessions.find((s) => s.session_id === sessionId)?.ended_at,
  );
  // One predicate for `disabled` and its reason (see Btn.disabledReason).
  const endReason = (sid: string | null, ended: boolean): string | undefined =>
    !sid
      ? t("chatPage.endDisabledNoSession")
      : harness
        ? t("chatPage.endDisabledHarness")
        : ended
          ? t("chatPage.endDisabledEnded")
          : ending
            ? t("chatPage.endDisabledBusy")
            : busy
              ? t("chatPage.waitForReply")
              : undefined;
  const currentEndReason = endReason(sessionId, currentEnded);

  return (
    <section>
      <ViewHead kicker={t("chat.kicker")} title={t("chat.title")} meta={t("chat.metaLive")} />
      {staleAgent !== null && (
        <StaleLink
          kind={t("staleLink.kind.agent")}
          id={staleAgent}
          pickFrom="picker"
          onDismiss={() => setStaleAgent(null)}
        />
      )}

      <div className="chat-grid">
        <Panel
          brk
          pad={false}
          title={
            (
              <select
                value={agentId}
                disabled={busy}
                onChange={(e) => {
                  autoPickBlocked.current = false;
                  setAgentId(e.target.value);
                  newSession(e.target.value);
                }}
                style={{
                  background: "transparent",
                  border: "1px solid var(--line-2)",
                  color: "var(--ink)",
                  font: "inherit",
                  padding: "3px 8px",
                }}
                data-testid="agent-select"
                aria-label={t("chatPage.agentSelectLabel")}
              >
                {agents.length === 0 && (
                  <option value="">
                    {agentsError ? t("common.loadFailedShort") : t("chatPage.noAgents")}
                  </option>
                )}
                {agents.length > 0 && agentId === "" && (
                  <option value="" disabled>
                    {t("chatPage.pickAgent")}
                  </option>
                )}
                {agents.map((a) => (
                  <option key={a.id} value={a.id} style={{ background: "var(--panel)" }}>
                    {a.name}
                  </option>
                ))}
              </select>
            ) as unknown as string
          }
          sub={agent ? agent.method.toUpperCase() : undefined}
          end={
            <>
              {sessionId && (
                <Chip tone="muted" className="mono">
                  session {sessionId.slice(0, 8)}…
                </Chip>
              )}
              <Chip tone="aqua" icon="◈">
                {t("chatPage.memoryOn")}
              </Chip>
              <Btn disabled={busy} onClick={() => newSession()}>{t("chatPage.newSession")}</Btn>
              <Btn
                disabled={currentEndReason !== undefined}
                disabledReason={currentEndReason}
                onClick={() => sessionId && void endSession(sessionId)}
                data-testid="end-session"
              >
                {ending && ending === sessionId ? "…" : t("chatPage.endSession")}
              </Btn>
            </>
          }
          style={{ "--i": 0 } as CSSProperties}
        >
          {agent && <AttachmentHint capability={capability} />}
          <div className="thread" ref={threadRef} data-testid="thread">
            {agentsError && agents.length === 0 && (
              <LoadError
                message={agentsError}
                onRetry={() => void loadAgents()}
                data-testid="chat-agents-load-error"
              />
            )}
            {messages.length === 0 && !(agentsError && agents.length === 0) && (
              <div className="empty">{t("chatPage.emptyThread")}</div>
            )}
            {messages.map((msg, i) =>
              msg.kind === "user" ? (
                <div key={i} className="msg user">
                  <div className="who">{userLabel}</div>
                  <div className="bub">
                    {msg.text}
                    <MessageAttachments files={msg.attachments} />
                  </div>
                </div>
              ) : msg.kind === "agent" ? (
                <div key={i} className="msg agent">
                  <div className="who">
                    {agent?.name.toUpperCase() ?? "AGENT"}
                    {msg.streaming ? " · STREAMING" : ""}
                  </div>
                  <div className="bub">
                    <Markdown text={msg.text} />
                    {msg.streaming && <span className="caret" />}
                  </div>
                </div>
              ) : msg.kind === "tool" ? (
                <div key={i} className="toolcard">
                  <span className="tc-ic">⇄</span>
                  {msg.name}
                  <Chip tone="good" icon="✓" style={{ marginLeft: "auto" }}>
                    {t("chatPage.toolCalled")}
                  </Chip>
                </div>
              ) : msg.kind === "memory" ? (
                <div key={i} className="memline">
                  <i>◈</i> {msg.text}
                </div>
              ) : (
                <div key={i} className="note" style={{ borderColor: "var(--crit)" }}>
                  <span className="i" style={{ color: "var(--crit)" }}>
                    [✕]
                  </span>
                  <span className="mono">{msg.text}</span>
                </div>
              ),
            )}
          </div>
          <div
            className={`chat-composer${draggingFiles ? " dragging" : ""}`}
            data-testid="attachment-composer"
            onDragOver={(e) => {
              if (!e.dataTransfer.types.includes("Files")) return;
              e.preventDefault();
              e.dataTransfer.dropEffect = composerDisabled ? "none" : "copy";
              if (!composerDisabled) setDraggingFiles(true);
            }}
            onDragLeave={(e) => {
              if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDraggingFiles(false);
            }}
            onDrop={(e) => {
              if (!e.dataTransfer.types.includes("Files")) return;
              e.preventDefault();
              setDraggingFiles(false);
              addFiles(Array.from(e.dataTransfer.files));
            }}
            onPaste={(e) => {
              const images = Array.from(e.clipboardData.files).filter((file) => file.type.startsWith("image/"));
              if (!images.length) return;
              e.preventDefault();
              addFiles(images);
            }}
          >
            <PendingAttachments
              files={pendingFiles}
              disabled={composerDisabled}
              onRemove={(id) => {
                setAttachmentDraft((draft) => ({
                  files: draft.files.filter((file) => file.id !== id),
                  error: null,
                }));
              }}
            />
            {attachmentError && (
              <div className="chat-attachment-error" role="alert" data-testid="attachment-error">
                {attachmentError}
              </div>
            )}
            <div className="chatbar">
              <input
                ref={fileInputRef}
                type="file"
                hidden
                multiple
                accept={capability?.accept.join(",")}
                disabled={composerDisabled || !attachmentsEnabled}
                onChange={(e) => {
                  addFiles(Array.from(e.target.files ?? []));
                  e.target.value = "";
                }}
                aria-label={t("chatPage.attachments.add")}
                data-testid="attachment-input"
              />
              <Btn
                disabled={composerDisabled || !attachmentsEnabled}
                title={t(attachmentsEnabled ? "chatPage.attachments.add" : "chatPage.attachments.unavailable")}
                aria-describedby="chat-attachment-hint"
                onClick={() => fileInputRef.current?.click()}
                data-testid="attachment-picker"
              >
                + {t("chatPage.attachments.add")}
              </Btn>
              <input
                className="input"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && !e.nativeEvent.isComposing && void send()}
                placeholder={agent ? t("chatPage.placeholder", { name: agent.name }) : "…"}
                disabled={composerDisabled}
                aria-label={t("chatPage.messageLabel")}
                data-testid="chat-input"
              />
              <Btn
                primary
                disabled={busy || restoring || sendDisabledReason !== undefined}
                disabledReason={sendDisabledReason}
                onClick={() => void send()}
              >
                {t("chatPage.send")} ▸
              </Btn>
            </div>
            {attachmentsEnabled && (
              <div className="chat-attachment-help">{t("chatPage.attachments.dropHint")}</div>
            )}
          </div>
        </Panel>

        <div>
          <Panel
            title={t("chatPage.historyTitle")}
            sub={sessions.length ? String(sessions.length) : undefined}
            pad={false}
            style={{ "--i": 1 } as CSSProperties}
          >
            <div style={{ maxHeight: 200, overflowY: "auto" }} data-testid="history-rail">
              {sessions.length === 0 && (
                <div className="empty">{t("chatPage.historyEmpty")}</div>
              )}
              {sessions.map((s) => {
                const rowReason = endReason(s.session_id, Boolean(s.ended_at));
                return (
                  <div
                    className="histrow-line"
                    key={s.session_id}
                    data-ended={s.ended_at ? "true" : undefined}
                  >
                    <button
                      type="button"
                      className={`histrow${s.session_id === sessionId ? " on" : ""}`}
                      disabled={busy}
                      onClick={() => void restoreSession(s.session_id)}
                    >
                      <span className="hp">
                        {s.preview || `${s.session_id.slice(0, 20)}…`}
                      </span>
                      <span className="hm mono">
                        {t("chatPage.historyTurns", { count: s.turns })} ·{" "}
                        {(s.last_at ?? "").slice(5, 16).replace("T", " ")}
                        {s.ended_at && ` · ${t("chatPage.historyEnded")}`}
                      </span>
                    </button>
                    <div className="histrow-act">
                      <Btn
                        disabled={rowReason !== undefined}
                        disabledReason={rowReason}
                        onClick={() => void endSession(s.session_id)}
                        data-testid="history-end-session"
                      >
                        {ending === s.session_id ? "…" : t("chatPage.historyEnd")}
                      </Btn>
                    </div>
                  </div>
                );
              })}
            </div>
          </Panel>
          <div style={{ height: 14 }} />
          <Panel
            title={t("chatPage.traceTitle")}
            sub={sessionId ? `${sessionId.slice(0, 12)}…` : "aws/spans"}
            end={
              <>
                {sessionId && (
                  <Link
                    to={`/observability?session=${encodeURIComponent(sessionId)}`}
                    target="_blank"
                    rel="noreferrer"
                    className="chip amber"
                    style={{ textDecoration: "none" }}
                    data-testid="open-in-obs"
                  >
                    {t("chatPage.openInObs")} ↗
                  </Link>
                )}
                {trace && (
                  <a
                    href={trace.cloudwatch_url}
                    target="_blank"
                    rel="noreferrer"
                    className="chip muted"
                    style={{ textDecoration: "none" }}
                  >
                    CLOUDWATCH ↗
                  </a>
                )}
                <Btn disabled={!sessionId || traceBusy} onClick={() => void loadTrace()}>
                  {traceBusy ? "…" : `⟳ ${t("chatPage.traceLoad")}`}
                </Btn>
              </>
            }
            pad={false}
            style={{ "--i": 1 } as CSSProperties}
          >
            {trace && trace.span_count > 0 ? (
              <>
                <div className="tl" data-testid="trace-rows">
                  {(() => {
                    const spans = trace.spans.slice(0, 12);
                    const total = Math.max(
                      ...spans.map((s) => (s.start_ms ?? 0) + (s.duration_ms ?? 0)),
                      1,
                    );
                    return spans.map((span, i) => (
                      <div className="trow" key={i}>
                        <span className="tn">{span.name}</span>
                        <div className="track">
                          <div
                            className="span"
                            style={{
                              left: `${((span.start_ms ?? 0) / total) * 100}%`,
                              width: `${Math.max(((span.duration_ms ?? 0) / total) * 100, 0.8)}%`,
                              background: SPAN_COLOR[span.category] ?? SPAN_COLOR.other,
                            }}
                          />
                        </div>
                        <span className="ms">{Math.round(span.duration_ms ?? 0)}ms</span>
                      </div>
                    ));
                  })()}
                </div>
                <div className="pbody" style={{ paddingTop: 4, borderTop: "1px solid var(--grid)" }}>
                  <div className="legend" style={{ flexWrap: "wrap", gap: 8 }}>
                    {(["model", "tool", "memory", "policy"] as const).map((cat) => (
                      <span className="li" key={cat}>
                        <span className="sw" style={{ background: SPAN_COLOR[cat] }} />
                        {cat}
                      </span>
                    ))}
                  </div>
                </div>
              </>
            ) : (
              <div className="empty">
                {sessionId ? t("chatPage.traceEmpty") : t("chatPage.tracePlaceholder")}
              </div>
            )}
          </Panel>
          <div style={{ height: 14 }} />
          <Panel
            title={t("chatPage.memoryTitle")}
            style={{ "--i": 2 } as CSSProperties}
            end={
              sessionId &&
              memory?.actor_id && (
                <Link
                  to={`/memory?view=short-term&actor=${encodeURIComponent(
                    memory.actor_id,
                  )}&session=${encodeURIComponent(sessionId)}`}
                  target="_blank"
                  rel="noreferrer"
                  className="chip amber"
                  style={{ textDecoration: "none" }}
                  data-testid="open-in-memory"
                >
                  {t("chatPage.openInMemory")} ↗
                </Link>
              )
            }
          >
            <div className="kv">
              <span className="k">{t("chatPage.shortTermEvents")}</span>
              <span className="v">{memory?.event_count ?? 0}</span>
            </div>
            <div className="kv">
              <span className="k">{t("chatPage.longTermRecords")}</span>
              <span className="v">{memory?.records.length ?? 0}</span>
            </div>
            {memory && memory.records.length > 0 && (
              <div className="code" style={{ marginTop: 10, maxHeight: 140, overflowY: "auto" }}>
                {memory.records.map((r, i) => (
                  <div key={i}>
                    <span className="cm">{r.namespace}</span>
                    {"\n"}“{r.text}”{"\n"}
                  </div>
                ))}
              </div>
            )}
          </Panel>
          <div style={{ height: 14 }} />
          <Panel title={t("chatPage.apiTitle")} style={{ "--i": 3 } as CSSProperties}>
            <div className="code">
              {`curl -N -X POST \\
  ${window.location.origin}/v1/agents/${agentId || "<id>"}/invoke-stream \\
  -H "x-api-key: lp_live_…" \\
  -d '{"prompt":"…","session_id":${sessionId ? `"${sessionId.slice(0, 8)}…"` : "null"}}'`}
            </div>
          </Panel>
          <div style={{ height: 14 }} />
          <Panel
            title={t("chatPage.keysTitle")}
            end={<Btn onClick={() => void createKey()}>+ {t("chatPage.newKey")}</Btn>}
            style={{ "--i": 4 } as CSSProperties}
          >
            {newKey?.key && (
              <div className="note" style={{ marginBottom: 10 }}>
                <span className="i">[i]</span>
                <span className="mono" data-testid="new-key">
                  {t("chatPage.keyOnce")}: {newKey.key}
                </span>
              </div>
            )}
            {keys.length === 0 && <div className="empty">{t("chatPage.noKeys")}</div>}
            {keys.map((key) => (
              <div className="kv" key={key.id}>
                <span className="k mono">
                  {key.prefix} · {key.name}
                </span>
                <span className="v">
                  <button
                    type="button"
                    className={`selchip${key.enabled ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    onClick={() => requestToggleKey(key)}
                  >
                    {key.enabled ? t("chatPage.keyEnabled") : t("chatPage.keyDisabled")}
                  </button>
                </span>
              </div>
            ))}
          </Panel>
        </div>
      </div>

      <ConfirmDialog
        open={confirmKeyDisable !== null}
        title={t("chatPage.confirmDisableKey.title")}
        body={t("chatPage.confirmDisableKey.body", { name: confirmKeyDisable?.name ?? "" })}
        confirmLabel={t("chatPage.keyDisabled")}
        onConfirm={() => {
          if (confirmKeyDisable) void toggleKey(confirmKeyDisable);
          setConfirmKeyDisable(null);
        }}
        onCancel={() => setConfirmKeyDisable(null)}
      />
    </section>
  );
}
