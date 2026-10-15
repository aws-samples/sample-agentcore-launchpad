import { SendHorizontal } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { chatApi, errorMessage, localizedMessage } from "../../../lib/api";
import { budgetRefusalOf } from "../../../lib/budgets";
import { sseEvents } from "../../../lib/chat";
import { BudgetRefusalLink } from "../../BudgetBanner";
import { refreshMyUsageIfLimited } from "../../myUsage";
import { Alert, Button, Card } from "../../ui";
import { applyStreamEvent, awaitingReply, closeOpen, pushTurn, type Turn } from "./tryChatTurns";

/**
 * Inline try-chat on the agent detail page. A try is a real invocation over the
 * same `chatApi.stream` + `sseEvents` transport as the Chat console, so it opens
 * an ordinary ledger chat session (listed in Chat, memory scoped to this agent
 * and the signed-in member). One session per mount, reused across turns; the
 * in-flight stream is aborted on unmount. Tool rows are shown by name only; a
 * consent ask or policy denial points to the full Chat console.
 */
export function TryChat({ agentId }: { agentId: string }) {
  const { t } = useTranslation();
  const [turns, setTurns] = useState<Turn[]>([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const [sessionId, setSessionId] = useState<string | null>(null);
  const sessionRef = useRef<string | null>(null);
  const inFlight = useRef(false);
  const abortRef = useRef<AbortController | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  useEffect(() => () => abortRef.current?.abort(), []);

  useEffect(() => {
    bodyRef.current?.scrollTo({ top: bodyRef.current.scrollHeight });
  }, [turns, busy]);

  const copy = {
    localize: localizedMessage,
    sendFailed: t("chatPage.sendFailed"),
    needsChat: t("v2.agents.tryChat.needsChat"),
  };

  const send = async () => {
    const prompt = text.trim();
    if (!prompt || inFlight.current) return;
    inFlight.current = true;
    const controller = new AbortController();
    abortRef.current = controller;
    setText("");
    setBusy(true);
    setTurns((prev) => pushTurn(prev, { role: "user", text: prompt }));
    let completed = false;
    let failed = false;
    try {
      const res = await chatApi.stream(agentId, { prompt, session_id: sessionRef.current }, controller.signal);
      if (!res.body) throw new Error(t("chatPage.streamInterrupted"));
      for await (const { event, data } of sseEvents(res)) {
        if (event === "meta" && data.session_id) {
          sessionRef.current = data.session_id;
          setSessionId(data.session_id);
        }
        if (event === "error") failed = true;
        if (event === "done") completed = true;
        setTurns((prev) => applyStreamEvent(prev, event, data, copy));
      }
      if (!completed && !failed) throw new Error(t("chatPage.streamInterrupted"));
    } catch (err) {
      if (controller.signal.aborted) return; // unmounted: nobody is looking
      failed = true;
      const budget = budgetRefusalOf(err) ?? undefined;
      setTurns((prev) => pushTurn(prev, { role: "error", text: errorMessage(err), budget }));
    } finally {
      if (!controller.signal.aborted) {
        // keep the draft on failure, unless the member already typed a new one
        if (failed) setText((cur) => cur || prompt);
        setTurns(closeOpen);
        setBusy(false);
        refreshMyUsageIfLimited(); // the turn was charged: the 80 % banner may be due
      }
      inFlight.current = false;
    }
  };

  const chatUrl =
    `/v2/chat?agent=${encodeURIComponent(agentId)}` +
    (sessionId ? `&session=${encodeURIComponent(sessionId)}` : "");

  return (
    <Card title={t("v2.agents.tryChat.title")} sub={t("v2.agents.tryChat.sub")} testId="v2-agent-trychat">
      <div className="v2-trychat-body" ref={bodyRef} aria-live="polite" data-testid="v2-agent-trychat-body">
        {turns.length === 0 && <p className="v2-trychat-empty">{t("v2.agents.tryChat.empty")}</p>}
        {turns.map((turn, i) =>
          turn.role === "error" ? (
            <Alert key={i} tone="error" action={turn.budget ? <BudgetRefusalLink code={turn.budget} /> : undefined}>
              {turn.text}
            </Alert>
          ) : turn.role === "notice" ? (
            <Alert key={i} tone="warn">
              {turn.text}
            </Alert>
          ) : turn.role === "tool" ? (
            <div key={i} className="v2-trychat-tool">
              {t("v2.agents.tryChat.tool", { name: turn.text })}
            </div>
          ) : (
            <div key={i} className={`v2-trychat-msg ${turn.role}`}>
              {turn.text}
            </div>
          ),
        )}
        {awaitingReply(turns, busy) && (
          <div className="v2-trychat-msg agent pending">{t("v2.agents.tryChat.thinking")}</div>
        )}
      </div>
      <form
        className="v2-trychat-form"
        onSubmit={(e) => {
          e.preventDefault();
          void send();
        }}
      >
        <textarea
          className="v2-textarea"
          rows={2}
          value={text}
          maxLength={4000}
          placeholder={t("v2.agents.tryChat.placeholder")}
          aria-label={t("v2.agents.tryChat.placeholder")}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
              e.preventDefault();
              void send();
            }
          }}
          data-testid="v2-agent-trychat-input"
        />
        <Button kind="primary" type="submit" disabled={busy || !text.trim()} testId="v2-agent-trychat-send">
          <SendHorizontal size={14} /> {t("v2.agents.tryChat.send")}
        </Button>
      </form>
      <div className="v2-trychat-foot">
        <Link to={chatUrl} data-testid="v2-agent-trychat-open">
          {t("v2.agents.tryChat.openChat")}
        </Link>
      </div>
    </Card>
  );
}
