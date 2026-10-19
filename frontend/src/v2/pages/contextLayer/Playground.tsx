import { MessagesSquare, RotateCcw, Send, Square } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { Markdown } from "../../../components/Markdown";
import {
  api,
  ApiError,
  type ContextLayerDoneEvent,
  type ContextLayerMode,
  type ContextLayerStatus,
  type ContextLayerStrategy,
  errorMessage,
  localizedMessage,
} from "../../../lib/api";
import { sseEvents } from "../../../lib/chat";
import { fmtDuration } from "../../format";
import type { Loaded } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader, Lamp, Segmented, Select, Spin, Tag } from "../../ui";
import { AuthCard } from "../chat/AuthCard";
import "../chat/chat.css";
import { CoaErrorNotice, ConsoleLink, GateNotice } from "./common";
import {
  answerOf,
  applyEvent,
  cellText,
  confidencePct,
  conversationId,
  failTurn,
  finishTurn,
  newTurn,
  numericColumn,
  type PlaygroundTurn,
  retryQuestion,
  tierLabel,
} from "./playgroundState";
import { namespaceLabel } from "./state";
import { useCoaLoad } from "./useCoaLoad";
import { coaConsoleUrl, coaErrorKind, type ContextView, moduleGate, stepSignal } from "./views";

const QUERY_MAX = 4000;
const STRATEGIES: ContextLayerStrategy[] = ["best", "ontop", "nl_to_sql", "ontop_first", "nl_to_sql_first"];

function Rows({ result }: { result: ContextLayerDoneEvent }) {
  const { t } = useTranslation();
  if (!result.columns.length) return null;
  return (
    <div className="v2-cl-rows" data-testid="context-layer-play-rows">
      <div className="v2-table-wrap">
        <table className="v2-table dense">
          <thead>
            <tr>
              {result.columns.map((c) => (
                <th key={c} className={numericColumn(result.rows, c) ? "right" : undefined}>
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {result.rows.map((row, i) => (
              <tr key={i}>
                {result.columns.map((c) => (
                  <td key={c} className={numericColumn(result.rows, c) ? "right mono" : undefined}>
                    {cellText(row[c])}
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <span className="v2-muted">
        {result.rows_truncated
          ? t("v2.contextLayer.playground.rowsTruncated", { shown: result.rows.length, count: result.row_count })
          : t("v2.contextLayer.playground.rows", { count: result.row_count })}
      </span>
    </div>
  );
}

function TurnError({ turn, consoleUrl, onAuthorized }: { turn: PlaygroundTurn; consoleUrl: string | null; onAuthorized: () => void }) {
  const error = turn.error;
  if (!error) return null;
  const kind = coaErrorKind(error.code);
  if (kind === "authorize" || kind === "reauthorize" || kind === "forbidden") {
    return <CoaErrorNotice error={error} onAuthorized={onAuthorized} consoleUrl={consoleUrl} testId="context-layer-play-error" />;
  }
  const title = localizedMessage(error.code, error.message || error.code);
  return (
    <div data-testid="context-layer-play-error" data-kind={kind} data-code={error.code}>
      <Alert tone={error.code === "context_layer.no_result" || kind === "notFound" ? "warn" : "error"}>
        {title}
        {error.message && error.message !== title && <div className="v2-muted">{error.message}</div>}
      </Alert>
    </div>
  );
}

function Turn({
  turn,
  consoleUrl,
  busy,
  onRetry,
  onAuthorized,
}: {
  turn: PlaygroundTurn;
  consoleUrl: string | null;
  busy: boolean;
  onRetry: (question: string) => void;
  onAuthorized: () => void;
}) {
  const { t } = useTranslation();
  const streaming = turn.phase === "streaming";
  const answer = answerOf(turn);
  const result = turn.result;
  const tier = tierLabel(result?.tier);
  const confidence = confidencePct(result?.confidence);
  return (
    <>
      <div className="v2-chat-msg user">
        <div className="v2-chat-who">{t("v2.contextLayer.playground.you")}</div>
        <div className="v2-chat-bub">
          <div className="v2-chat-text">{turn.question}</div>
        </div>
      </div>
      <div className="v2-chat-msg agent v2-cl-answer" data-testid="context-layer-play-turn" data-phase={turn.phase}>
        <div className="v2-chat-who">
          COA
          {streaming && (
            <Tag tone="blue" dot>
              {t("v2.contextLayer.playground.streaming")}
            </Tag>
          )}
          {tier && (
            <Tag tone="outline" title={t("v2.contextLayer.playground.tierHint")}>
              <span data-testid="context-layer-play-tier">{tier}</span>
            </Tag>
          )}
          {confidence != null && (
            <Tag tone={confidence >= 70 ? "green" : confidence >= 40 ? "orange" : "red"} title={result?.rationale ?? undefined}>
              {t("v2.contextLayer.playground.confidence", { pct: confidence })}
            </Tag>
          )}
          {result?.total_ms != null && <span className="v2-muted">{fmtDuration(result.total_ms)}</span>}
          {result?.partial && <Tag tone="orange">{t("v2.contextLayer.playground.partial")}</Tag>}
          {result?.guardrail_blocked && <Tag tone="red">{t("v2.contextLayer.playground.guardrail")}</Tag>}
          {turn.phase === "stopped" && <Tag tone="gray">{t("v2.contextLayer.playground.stopped")}</Tag>}
        </div>
        {turn.auth && (
          <AuthCard
            ask={turn.auth}
            retryPrompt={retryQuestion(turn)}
            retryDisabled={busy}
            onRetry={(question) => {
              onAuthorized();
              onRetry(question);
            }}
          />
        )}
        <TurnError turn={turn} consoleUrl={consoleUrl} onAuthorized={onAuthorized} />
        {(answer || streaming) && (
          <div className="v2-chat-bub" data-testid="context-layer-play-answer">
            {answer ? <Markdown text={answer} /> : <span className="v2-muted">{t("v2.contextLayer.playground.thinking")}</span>}
            {streaming && <span className="v2-chat-caret" />}
          </div>
        )}
        {result && <Rows result={result} />}
        {(result?.citations.length ?? 0) > 0 && (
          <details className="v2-cl-sql" data-testid="context-layer-play-citations">
            <summary>{t("v2.contextLayer.playground.citations", { count: result?.citations.length ?? 0 })}</summary>
            <ul className="v2-cl-citations">
              {result?.citations.map((c, i) => (
                <li key={i}>
                  <span className="mono">{c.name ?? "—"}</span>
                  {c.text && <span className="v2-muted">{c.text}</span>}
                </li>
              ))}
            </ul>
          </details>
        )}
        {turn.steps.length > 0 && (
          <details className="v2-cl-steps" data-testid="context-layer-play-steps" open={streaming}>
            <summary>{t("v2.contextLayer.playground.steps", { count: turn.steps.length })}</summary>
            <ol>
              {turn.steps.map((step, i) => (
                <li key={i} data-signal={stepSignal(step.status)}>
                  <Lamp signal={stepSignal(step.status)} title={step.status} />
                  <span className="mono">{step.name}</span>
                  <span className="v2-muted">{step.status}</span>
                  {step.duration_ms != null && <span className="v2-muted">{fmtDuration(step.duration_ms)}</span>}
                  {step.tool && <Tag tone="outline">{step.tool}</Tag>}
                  {step.detail && <span className="d">{step.detail}</span>}
                </li>
              ))}
            </ol>
          </details>
        )}
      </div>
    </>
  );
}

/**
 * `?view=playground[&ns=<uuid>]` — ask COA questions about one namespace, as the
 * member. The answer streams from COA's Serve runtime (steps, then the answer,
 * tier, confidence and result rows); COA's conversation id carries over between
 * turns until "New conversation". Missing consent shows Chat's consent card.
 */
export function Playground({
  ns,
  status,
  go,
}: {
  ns: string | null;
  status: Loaded<ContextLayerStatus>;
  go: (view: ContextView, replace?: boolean) => void;
}) {
  const { t } = useTranslation();
  const gate = moduleGate(status);
  const consoleUrl = status.data?.console_url ?? null;
  const namespaces = useCoaLoad(() => api.listContextLayerNamespaces(), "context-layer-play-namespaces", gate === "ready");
  const [turns, setTurns] = useState<PlaygroundTurn[]>([]);
  const [query, setQuery] = useState("");
  const [mode, setMode] = useState<ContextLayerMode>("standard");
  const [strategy, setStrategy] = useState<"" | ContextLayerStrategy>("");
  const [busy, setBusy] = useState(false);
  const abort = useRef<AbortController | null>(null);
  const nextId = useRef(1);
  const thread = useRef<HTMLDivElement>(null);
  useEffect(() => () => abort.current?.abort(), []);
  useEffect(() => {
    thread.current?.scrollTo({ top: thread.current.scrollHeight });
  }, [turns]);

  const list = namespaces.data?.namespaces ?? [];
  const current = list.find((n) => n.namespace_id === ns) ?? null;
  const options = list.map((n) => ({ value: n.namespace_id, label: namespaceLabel(n) }));
  if (ns && !current) options.push({ value: ns, label: ns });
  const back = () => go(ns ? { view: "namespace", id: ns, tab: "sources" } : { view: "overview" });

  const ask = async (question: string) => {
    const text = question.trim();
    if (!ns || !text || busy) return;
    const id = nextId.current++;
    const controller = new AbortController();
    abort.current = controller;
    const sessionId = conversationId(turns);
    const update = (fn: (turn: PlaygroundTurn) => PlaygroundTurn) =>
      setTurns((prev) => prev.map((turn) => (turn.id === id ? fn(turn) : turn)));
    setTurns((prev) => [...prev, newTurn(id, text)]);
    setQuery("");
    setBusy(true);
    try {
      const res = await api.contextLayerPlayground(
        { namespace_id: ns, query: text, session_id: sessionId, mode, strategy: strategy || null },
        controller.signal,
      );
      for await (const frame of sseEvents<unknown>(res)) {
        update((turn) => applyEvent(turn, frame.event, frame.data));
      }
      update((turn) => finishTurn(turn, false));
    } catch (err) {
      if (controller.signal.aborted) update((turn) => finishTurn(turn, true));
      else if (err instanceof ApiError) update((turn) => failTurn(turn, err.code, err.message));
      else update((turn) => failTurn(turn, "network", errorMessage(err)));
    } finally {
      if (abort.current === controller) abort.current = null;
      setBusy(false);
    }
  };
  const stop = () => abort.current?.abort();
  const reset = () => {
    abort.current?.abort();
    setTurns([]);
  };

  if (gate !== "ready") {
    return (
      <>
        <FlowHeader title={t("v2.contextLayer.playground.title")} onBack={back} />
        <GateNotice gate={gate} status={status.data} error={status.error} onChanged={status.reload} onOverview={() => go({ view: "overview" })} />
      </>
    );
  }
  return (
    <>
      <FlowHeader
        title={t("v2.contextLayer.playground.title")}
        onBack={back}
        end={
          <>
            <ConsoleLink href={coaConsoleUrl(consoleUrl, "playground")} label={t("v2.contextLayer.playground.openCoa")} testId="context-layer-coa-playground" />
            <Button onClick={reset} disabled={turns.length === 0} testId="context-layer-play-reset">
              <RotateCcw size={14} aria-hidden="true" />
              {t("v2.contextLayer.playground.reset")}
            </Button>
          </>
        }
      />
      <Card testId="context-layer-playground">
        <div className="v2-form cols-2">
          <Field label={t("v2.contextLayer.wizard.namespace")}>
            {namespaces.loading && !namespaces.data ? (
              <Spin />
            ) : (
              <Select
                value={ns ?? ""}
                placeholder={t("v2.contextLayer.playground.pickNamespace")}
                options={options}
                onChange={(value) => go({ view: "playground", ns: value || null }, true)}
                testId="context-layer-play-namespace"
              />
            )}
          </Field>
          <Field label={t("v2.contextLayer.playground.mode")} hint={t("v2.contextLayer.playground.modeHint")}>
            <Segmented<ContextLayerMode>
              value={mode}
              onChange={setMode}
              ariaLabel={t("v2.contextLayer.playground.mode")}
              options={[
                { value: "standard", label: t("v2.contextLayer.playground.modeStandard") },
                { value: "deep-reasoning", label: t("v2.contextLayer.playground.modeDeep") },
              ]}
            />
          </Field>
          <Field label={t("v2.contextLayer.playground.strategy")} hint={t("v2.contextLayer.playground.strategyHint")}>
            <Select
              value={strategy}
              placeholder={t("v2.contextLayer.playground.strategyDefault")}
              options={STRATEGIES.map((value) => ({ value, label: value }))}
              onChange={(value) => setStrategy(value as "" | ContextLayerStrategy)}
              mono
              testId="context-layer-play-strategy"
            />
          </Field>
        </div>
        {namespaces.error && (
          <CoaErrorNotice error={namespaces.error} onAuthorized={status.reload} onRetry={namespaces.reload} consoleUrl={consoleUrl} />
        )}
        {!ns && !namespaces.error && <Alert>{t("v2.contextLayer.playground.needNamespace")}</Alert>}
        <div className="v2-cl-thread" ref={thread} data-testid="context-layer-play-thread">
          {turns.length === 0 ? (
            <div className="v2-chat-empty">
              <MessagesSquare size={32} aria-hidden="true" />
              <div>{t("v2.contextLayer.playground.empty")}</div>
            </div>
          ) : (
            turns.map((turn) => (
              <Turn key={turn.id} turn={turn} consoleUrl={consoleUrl} busy={busy} onRetry={(q) => void ask(q)} onAuthorized={status.reload} />
            ))
          )}
        </div>
        <div className="v2-cl-composer">
          <textarea
            className="v2-textarea"
            value={query}
            maxLength={QUERY_MAX}
            disabled={!ns}
            placeholder={t("v2.contextLayer.playground.placeholder")}
            onChange={(e) => setQuery(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                e.preventDefault();
                void ask(query);
              }
            }}
            data-testid="context-layer-play-input"
          />
          {busy ? (
            <Button onClick={stop} testId="context-layer-play-stop">
              <Square size={14} aria-hidden="true" />
              {t("v2.contextLayer.playground.stop")}
            </Button>
          ) : (
            <Button kind="primary" disabled={!ns || !query.trim()} onClick={() => void ask(query)} testId="context-layer-play-send">
              <Send size={14} aria-hidden="true" />
              {t("v2.contextLayer.playground.send")}
            </Button>
          )}
        </div>
        <p className="v2-muted v2-cl-hint">{t("v2.contextLayer.playground.hint")}</p>
      </Card>
    </>
  );
}
