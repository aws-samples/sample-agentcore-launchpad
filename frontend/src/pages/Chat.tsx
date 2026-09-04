import type { CSSProperties } from "react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { useAuth } from "../auth/auth-context";
import { Btn, Chip, ConfirmDialog, LoadError, Markdown, Panel, ViewHead } from "../components";
import type { AgentInfo } from "../lib/api";
import { api, errorMessage } from "../lib/api";

interface Message {
  kind: "user" | "agent" | "tool" | "memory" | "error";
  text: string;
  name?: string;
  streaming?: boolean;
}

interface MemorySummary {
  event_count: number;
  records: { namespace: string; text: string }[];
  /** Compound `<agent_id>__<human>` partition the summary was read from — the
   *  id the Memory console keys on, so the deep link needs it verbatim. */
  actor_id?: string;
}

interface SessionItem {
  session_id: string;
  turns: number;
  last_at: string | null;
  preview: string;
}

interface HistoryRow {
  role: string;
  text: string;
  name: string | null;
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

async function* sseEvents(res: Response): AsyncGenerator<{ event: string; data: never }> {
  const reader = res.body!.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const frames = buffer.split("\n\n");
    buffer = frames.pop() ?? "";
    for (const frame of frames) {
      let event = "message";
      let data = "";
      for (const line of frame.split("\n")) {
        if (line.startsWith("event:")) event = line.slice(6).trim();
        if (line.startsWith("data:")) data += line.slice(5).trim();
      }
      if (data) yield { event, data: JSON.parse(data) as never };
    }
  }
}

export function Chat() {
  const { t } = useTranslation();
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
  const [sessionId, setSessionId] = useState<string | null>(linkedSession);
  const [busy, setBusy] = useState(false);
  const [memory, setMemory] = useState<MemorySummary | null>(null);
  const [sessions, setSessions] = useState<SessionItem[]>([]);
  const [trace, setTrace] = useState<TraceInfo | null>(null);
  const [traceBusy, setTraceBusy] = useState(false);
  const [keys, setKeys] = useState<KeyInfo[]>([]);
  const [newKey, setNewKey] = useState<KeyInfo | null>(null);
  const threadRef = useRef<HTMLDivElement>(null);
  const restoredRef = useRef(false);

  const loadAgents = () =>
    api
      .listAgents()
      .then((res) => {
        const active = res.agents.filter(
          (a) => a.status === "active" && a.invoke_capability.eligible,
        );
        setAgents(active);
        setAgentsError(null);
        const linked = linkedAgent && active.find((a) => a.id === linkedAgent);
        if (linked) {
          setAgentId(linked.id);
        } else {
          // Linked agent unknown/inactive: drop the linked session too, so a
          // foreign session id is never posted to a different agent's runtime.
          if (linkedSession) setSessionId(null);
          if (active.length && !agentId) setAgentId(active[0].id);
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
      const res = await fetch(`/api/chat/${aid}/sessions`);
      if (res.ok) setSessions(((await res.json()) as { sessions: SessionItem[] }).sessions);
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
    try {
      const res = await fetch(
        `/api/chat/${agentId}/history?session_id=${encodeURIComponent(sid)}`,
      );
      if (!res.ok) return;
      const rows = ((await res.json()) as { messages: HistoryRow[] }).messages;
      setMessages(
        rows.map((r): Message =>
          r.role === "user"
            ? { kind: "user", text: r.text }
            : r.role === "agent"
              ? { kind: "agent", text: r.text }
              : r.role === "tool"
                ? { kind: "tool", text: r.name ?? "tool", name: r.name ?? "tool" }
                : { kind: "error", text: r.text },
        ),
      );
      setSessionId(sid);
      setTrace(null);
      setSearchParams({ agent: agentId, session: sid }, { replace: true });
    } catch {
      /* history rail is best-effort */
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
    if (!prompt || !agentId || busy) return;
    setInput("");
    setBusy(true);
    setMessages((m) => [...m, { kind: "user", text: prompt }]);
    try {
      const res = await fetch(`/api/chat/${agentId}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ prompt, session_id: sessionId }),
      });
      if (!res.ok || !res.body) throw new Error(`http ${res.status}`);
      let agentIdxSet = false;
      for await (const { event, data } of sseEvents(res)) {
        const payload = data as {
          session_id?: string;
          text?: string;
          name?: string;
          message?: string;
        };
        if (event === "meta" && payload.session_id) {
          setSessionId(payload.session_id);
          // keep the session in the URL so a reload restores this conversation
          setSearchParams(
            { agent: agentId, session: payload.session_id },
            { replace: true },
          );
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
          setMessages((m) => [...m, { kind: "error", text: payload.message ?? "error" }]);
        } else if (event === "done") {
          setMessages((m) =>
            m.map((msg, i) => (i === m.length - 1 ? { ...msg, streaming: false } : msg)),
          );
          setMessages((m) => [
            ...m,
            { kind: "memory", text: t("chatPage.memorySaved") },
          ]);
        }
      }
    } catch (err) {
      setMessages((m) => [...m, { kind: "error", text: String(err) }]);
    } finally {
      setBusy(false);
      if (sessionId) void refreshMemory(sessionId);
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
    setSessionId(null);
    setMessages([]);
    setMemory(null);
    setTrace(null);
    setSearchParams(aid ? { agent: aid } : {}, { replace: true });
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

  return (
    <section>
      <ViewHead kicker={t("chat.kicker")} title={t("chat.title")} meta={t("chat.metaLive")} />

      <div className="chat-grid">
        <Panel
          brk
          pad={false}
          title={
            (
              <select
                value={agentId}
                onChange={(e) => {
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
                {agents.map((a) => (
                  <option key={a.id} value={a.id} style={{ background: "#141816" }}>
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
              <Btn onClick={() => newSession()}>{t("chatPage.newSession")}</Btn>
            </>
          }
          style={{ "--i": 0 } as CSSProperties}
        >
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
                  <div className="bub">{msg.text}</div>
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
          <div className="chatbar">
            <input
              className="input"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void send()}
              placeholder={agent ? t("chatPage.placeholder", { name: agent.name }) : "…"}
              disabled={busy || !agentId}
              data-testid="chat-input"
            />
            <Btn primary disabled={busy || !agentId} onClick={() => void send()}>
              {t("chatPage.send")} ▸
            </Btn>
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
              {sessions.map((s) => (
                <button
                  key={s.session_id}
                  type="button"
                  className={`histrow${s.session_id === sessionId ? " on" : ""}`}
                  onClick={() => void restoreSession(s.session_id)}
                >
                  <span className="hp">
                    {s.preview || `${s.session_id.slice(0, 20)}…`}
                  </span>
                  <span className="hm mono">
                    {t("chatPage.historyTurns", { count: s.turns })} ·{" "}
                    {(s.last_at ?? "").slice(5, 16).replace("T", " ")}
                  </span>
                </button>
              ))}
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
