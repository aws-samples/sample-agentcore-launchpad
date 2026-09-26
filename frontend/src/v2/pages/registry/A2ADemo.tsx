import { type ReactNode, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type RegistryA2ADemoResult } from "../../../lib/api";
import { fmtDuration } from "../../format";
import { useLoad } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader, Tag } from "../../ui";

function Stage({
  index,
  title,
  state,
  children,
}: {
  index: number;
  title: string;
  state: "" | "running" | "succeeded";
  children: ReactNode;
}) {
  return (
    <div className={`v2-stage ${state}`} data-testid={`v2-registry-demo-stage-${index}`}>
      <span className="n">{index}</span>
      <div className="b" style={{ flex: 1 }}>
        <div className="t">{title}</div>
        <div className="v2-registry-stage-body">{children}</div>
      </div>
    </div>
  );
}

/**
 * `?view=a2a-demo` — ask the front-desk routing agent; its tools search the
 * Registry (DISCOVER), pick a specialist by card (SELECT), call it over A2A
 * (INVOKE) and answer (RESPOND). The trace is the agent's `a2a_trace` verbatim.
 */
export function A2ADemo() {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const agents = useLoad(async () => {
    const res = await api.listAgents();
    return res.agents.filter((a) => a.status === "active" && (a.method === "zip_runtime" || a.method === "studio"));
  }, "registry-demo-agents");
  const eligible = agents.data ?? [];
  const [picked, setPicked] = useState("");
  // the deployed routing agent is the natural default
  const agentId = picked || (eligible.find((a) => a.name.includes("front-desk")) ?? eligible[0])?.id || "";
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<RegistryA2ADemoResult | null>(null);

  const ask = async () => {
    setBusy(true);
    setError(null);
    setResult(null);
    try {
      setResult(await api.registryA2ADemo(agentId, question));
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const discovers = result?.trace.filter((e) => e.stage === "discover") ?? [];
  const invokes = result?.trace.filter((e) => e.stage === "invoke") ?? [];
  const dash = <span className="v2-muted">—</span>;

  return (
    <>
      <FlowHeader title={t("v2.registry.demo.title")} onBack={() => setParams({})} />
      <div className="v2-registry-layout reverse">
        <Card title={t("v2.registry.demo.ask")}>
          <div className="v2-form">
            <Field label={t("v2.registry.demo.agent")} required hint={t("v2.registry.demo.agentHint")}>
              <select
                className="v2-select"
                value={agentId}
                onChange={(e) => setPicked(e.target.value)}
                data-testid="v2-registry-demo-agent"
              >
                {eligible.length === 0 && <option value="">{agents.loading ? t("v2.common.loading") : t("v2.registry.demo.noAgents")}</option>}
                {eligible.map((a) => (
                  <option key={a.id} value={a.id}>
                    {a.name} · {a.method}
                  </option>
                ))}
              </select>
            </Field>
            <Field label={t("v2.registry.demo.question")} required>
              <textarea
                className="v2-textarea"
                rows={4}
                value={question}
                onChange={(e) => setQuestion(e.target.value)}
                placeholder={t("registry.a2aDemo.placeholder")}
                data-testid="v2-registry-demo-question"
              />
            </Field>
            <div>
              <Button
                kind="primary"
                disabled={busy || !agentId || !question.trim()}
                onClick={() => void ask()}
                testId="v2-registry-demo-ask"
              >
                {busy ? t("v2.registry.demo.asking") : t("v2.registry.demo.send")}
              </Button>
            </div>
            {error && <Alert tone="error">{error}</Alert>}
            <Alert>{t("registry.a2aDemo.govNote")}</Alert>
          </div>
        </Card>

        <Card title={t("v2.registry.demo.flow")} sub={t("v2.registry.demo.flowSub")}>
          <div className="v2-stack" style={{ gap: 12 }}>
            <Stage index={1} title={t("registry.a2aDemo.stage.discover")} state={busy ? "running" : discovers.length ? "succeeded" : ""}>
              {discovers.length === 0 && dash}
              {discovers.map((d, i) => (
                <div key={i} className="v2-stack" data-testid="v2-registry-demo-discover">
                  <span className="v2-muted mono">
                    “{d.query}” · {t("registry.a2aDemo.hits", { n: (d.hits ?? []).length })}
                  </span>
                  {(d.hits ?? []).map((h) => (
                    <div key={h.name}>
                      <span className="v2-row">
                        <b>{h.name}</b>
                        <Tag tone={h.transport === "a2a-jsonrpc" ? "green" : "gray"}>{h.transport ?? "—"}</Tag>
                      </span>
                      <span className="v2-muted">{(h.skills ?? []).map((s) => s.name).filter(Boolean).join(" · ")}</span>
                    </div>
                  ))}
                  {(d.hits ?? []).length === 0 && <span className="v2-muted">{t("registry.a2aDemo.noHits")}</span>}
                </div>
              ))}
            </Stage>
            <Stage index={2} title={t("registry.a2aDemo.stage.select")} state={invokes.length ? "succeeded" : ""}>
              {invokes.length === 0 && dash}
              {invokes.map((v, i) => (
                <div key={i} className="v2-row" data-testid="v2-registry-demo-select">
                  <Tag tone="blue">{v.target ?? "—"}</Tag>
                  <span className="v2-muted">{v.reason}</span>
                </div>
              ))}
            </Stage>
            <Stage index={3} title={t("registry.a2aDemo.stage.invoke")} state={invokes.length ? "succeeded" : ""}>
              {invokes.length === 0 && dash}
              {invokes.map((v, i) => (
                <div key={i} className="v2-stack" data-testid="v2-registry-demo-invoke">
                  <Tag tone={v.transport === "a2a-jsonrpc" ? "green" : "gray"}>{v.transport ?? "—"}</Tag>
                  <span className="v2-muted">{t("v2.registry.demo.request")}</span>
                  <pre className="v2-pre" style={{ maxHeight: 140 }}>{v.request_excerpt}</pre>
                  <span className="v2-muted">{t("v2.registry.demo.response")}</span>
                  <pre className="v2-pre" style={{ maxHeight: 140 }}>{v.response_excerpt}</pre>
                </div>
              ))}
            </Stage>
            <Stage index={4} title={t("registry.a2aDemo.stage.respond")} state={result ? "succeeded" : ""}>
              {result ? (
                <>
                  <div style={{ whiteSpace: "pre-wrap" }} data-testid="v2-registry-demo-answer">{result.answer}</div>
                  <span className="v2-muted mono">{fmtDuration(result.latency_ms)}</span>
                </>
              ) : (
                dash
              )}
            </Stage>
          </div>
        </Card>
      </div>
    </>
  );
}
