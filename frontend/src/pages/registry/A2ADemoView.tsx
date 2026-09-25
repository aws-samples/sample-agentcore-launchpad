import type { CSSProperties, ReactNode } from "react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Chip, Panel, ViewHead } from "../../components";
import type { AgentInfo } from "../../lib/api";
import { api } from "../../lib/api";

// One DISCOVER or INVOKE record appended by the front-desk agent's tools —
// the backend passes the agent's `a2a_trace` payload field through verbatim.
interface TraceEntry {
  stage: "discover" | "invoke";
  query?: string;
  hits?: {
    name?: string;
    description?: string;
    transport?: string;
    skills?: { name?: string; description?: string; tags?: string[] }[];
  }[];
  target?: string;
  transport?: string;
  reason?: string;
  request_excerpt?: string;
  response_excerpt?: string;
}

interface DemoResult {
  answer: string;
  trace: TraceEntry[];
  latency_ms: number;
}

function StageCard({ index, title, active, done, children }: {
  index: number; title: string; active: boolean; done: boolean; children: ReactNode;
}) {
  return (
    <div
      style={{
        border: "1px solid var(--line)",
        borderLeft: `3px solid ${active ? "var(--warn)" : done ? "var(--good)" : "var(--line)"}`,
        borderRadius: 4, padding: "10px 12px", marginBottom: 10,
      }}
    >
      <div
        className="mono"
        style={{ fontSize: 11, fontWeight: 700, letterSpacing: ".08em", marginBottom: 8,
                 color: done ? "var(--good)" : active ? "var(--warn)" : "var(--ink-3)" }}
      >
        {String(index).padStart(2, "0")} · {title}{done ? " ✓" : ""}
      </div>
      {children}
    </div>
  );
}

export function A2ADemoView({ onBack }: { onBack: () => void }) {
  const { t } = useTranslation();
  const [agents, setAgents] = useState<AgentInfo[]>([]);
  const [agentId, setAgentId] = useState("");
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<DemoResult | null>(null);

  useEffect(() => {
    void api.listAgents().then((res) => {
      const eligible = res.agents.filter(
        (a) => a.status === "active" && (a.method === "zip_runtime" || a.method === "studio"),
      );
      setAgents(eligible);
      // the deployed routing agent is the natural default
      const fd = eligible.find((a) => a.name.includes("front-desk")) ?? eligible[0];
      setAgentId((prev) => prev || (fd?.id ?? ""));
    }).catch(() => {});
  }, []);

  const ask = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      const res = await fetch("/api/registry/a2a-demo", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ agent_id: agentId, question }),
      });
      const body = (await res.json()) as DemoResult & { message?: string };
      if (!res.ok) {
        setError(body.message ?? `HTTP ${res.status}`);
        return;
      }
      setResult(body);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  const discovers = result?.trace.filter((e) => e.stage === "discover") ?? [];
  const invokes = result?.trace.filter((e) => e.stage === "invoke") ?? [];
  const excerpt: CSSProperties = {
    maxHeight: 120, overflow: "auto", whiteSpace: "pre-wrap", fontSize: 10.5, margin: 0,
  };

  return (
    <section>
      <ViewHead
        kicker={t("registry.a2aDemo.kicker")}
        title={t("registry.a2aDemo.title")}
        meta={t("registry.a2aDemo.meta")}
      />
      <Btn onClick={onBack} data-testid="a2a-demo-back">◂ {t("registry.a2aDemo.back")}</Btn>

      <div className="cfg-grid" style={{ marginTop: 12 }}>
        <Panel brk title={t("registry.a2aDemo.askTitle")} style={{ "--i": 0 } as CSSProperties}>
          <div className="field">
            <label htmlFor="a2a-demo-agent">{t("registry.a2aDemo.agent")}</label>
            <select
              id="a2a-demo-agent"
              className="input"
              value={agentId}
              data-testid="a2a-demo-agent"
              onChange={(e) => setAgentId(e.target.value)}
            >
              {agents.map((a) => (
                <option key={a.id} value={a.id} style={{ background: "var(--panel)" }}>
                  {a.name} · {a.method}
                </option>
              ))}
            </select>
          </div>
          <div className="field">
            <label htmlFor="a2a-demo-q">{t("registry.a2aDemo.question")}</label>
            <textarea
              id="a2a-demo-q"
              className="input"
              rows={3}
              value={question}
              data-testid="a2a-demo-question"
              onChange={(e) => setQuestion(e.target.value)}
              placeholder={t("registry.a2aDemo.placeholder")}
            />
          </div>
          <Btn
            primary
            disabled={busy || !agentId || !question.trim()}
            data-testid="a2a-demo-ask"
            onClick={() => void ask()}
          >
            {busy ? `◐ ${t("registry.a2aDemo.asking")}` : `▸ ${t("registry.a2aDemo.ask")}`}
          </Btn>
          {error && (
            <div className="note" style={{ borderColor: "var(--crit)", marginTop: 10 }}>
              <span className="i" style={{ color: "var(--crit)" }}>[✕]</span>
              <span className="mono" style={{ fontSize: 10.5 }}>{error}</span>
            </div>
          )}
          <div className="note" style={{ marginTop: 12 }}>
            <span className="i">[i]</span>
            <span>{t("registry.a2aDemo.govNote")}</span>
          </div>
        </Panel>

        <Panel brk title={t("registry.a2aDemo.flowTitle")} style={{ "--i": 1 } as CSSProperties}>
          <StageCard
            index={1} title={t("registry.a2aDemo.stage.discover")}
            active={busy} done={discovers.length > 0}
          >
            {discovers.length === 0 && <span className="dim">—</span>}
            {discovers.map((d, i) => (
              <div key={i} style={{ marginBottom: 8 }} data-testid="demo-discover">
                <div className="mono dim" style={{ fontSize: 10 }}>
                  ⌕ “{d.query}” · {t("registry.a2aDemo.hits", { n: (d.hits ?? []).length })}
                </div>
                {(d.hits ?? []).map((h) => (
                  <div key={h.name} style={{ margin: "6px 0" }}>
                    <span className="selchip on" style={{ marginRight: 5 }}>{h.name}</span>
                    <Chip tone={h.transport === "a2a-jsonrpc" ? "good" : "muted"}>
                      {h.transport}
                    </Chip>
                    <div className="dim" style={{ fontSize: 10.5, marginTop: 2 }}>
                      {(h.skills ?? []).map((s) => s.name).filter(Boolean).join(" · ")}
                    </div>
                  </div>
                ))}
                {(d.hits ?? []).length === 0 && (
                  <div className="dim" style={{ fontSize: 10.5 }}>
                    {t("registry.a2aDemo.noHits")}
                  </div>
                )}
              </div>
            ))}
          </StageCard>

          <StageCard
            index={2} title={t("registry.a2aDemo.stage.select")}
            active={false} done={invokes.length > 0}
          >
            {invokes.length === 0 && <span className="dim">—</span>}
            {invokes.map((v, i) => (
              <div key={i} style={{ marginBottom: 6 }} data-testid="demo-select">
                <span className="selchip on" style={{ marginRight: 6 }}>{v.target}</span>
                <span className="dim" style={{ fontSize: 10.5 }}>{v.reason}</span>
              </div>
            ))}
          </StageCard>

          <StageCard
            index={3} title={t("registry.a2aDemo.stage.invoke")}
            active={false} done={invokes.length > 0}
          >
            {invokes.length === 0 && <span className="dim">—</span>}
            {invokes.map((v, i) => (
              <div key={i} style={{ marginBottom: 8 }} data-testid="demo-invoke">
                <Chip tone={v.transport === "a2a-jsonrpc" ? "good" : "muted"}>
                  {v.transport}
                </Chip>
                <div className="mono dim" style={{ fontSize: 10, margin: "4px 0 2px" }}>
                  {t("registry.a2aDemo.request")}
                </div>
                <pre className="code" style={excerpt}>{v.request_excerpt}</pre>
                <div className="mono dim" style={{ fontSize: 10, margin: "4px 0 2px" }}>
                  {t("registry.a2aDemo.response")}
                </div>
                <pre className="code" style={excerpt}>{v.response_excerpt}</pre>
              </div>
            ))}
          </StageCard>

          <StageCard
            index={4} title={t("registry.a2aDemo.stage.respond")}
            active={false} done={!!result}
          >
            {result ? (
              <>
                <div style={{ whiteSpace: "pre-wrap", fontSize: 12 }} data-testid="demo-answer">
                  {result.answer}
                </div>
                <div className="mono dim" style={{ fontSize: 10, marginTop: 6 }}>
                  {result.latency_ms} ms
                </div>
              </>
            ) : (
              <span className="dim">—</span>
            )}
          </StageCard>
        </Panel>
      </div>
    </section>
  );
}
