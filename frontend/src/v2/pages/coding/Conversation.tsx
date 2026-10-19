import {
  Brain,
  ChevronRight,
  CircleCheck,
  CircleDot,
  Circle,
  FileCode,
  Info,
  ListChecks,
  Loader2,
  SquareTerminal,
  Wrench,
} from "lucide-react";
import { Fragment, memo, useEffect, useRef } from "react";
import { useTranslation } from "react-i18next";

import { Markdown } from "../../../components/Markdown";
import { fmtDuration } from "../../format";
import { Alert, LinkButton, Tag } from "../../ui";
import type { ConversationNotice, ConversationState, FileChangeView, ItemView, TurnView } from "./codingEvents";
import { liveTurn, turnPrompt } from "./codingEvents";
import { TurnStatusTag } from "./common";
import { fmtTokens, usageBreakdown } from "./format";

/**
 * The conversation with the coding agent, rendered from the reduced event log: the
 * prompt, then every item in arrival order — streamed text (Markdown, caret), reasoning
 * summaries (open while streaming), commands with live output and exit code, file
 * changes (click → the Changes tab), MCP tool calls, plans, errors — and a per-turn
 * footer (model, effort, duration, tokens). Follows the tail unless scrolled up.
 */
export function Conversation({
  view,
  onOpenChange,
}: {
  view: ConversationState;
  onOpenChange: (path: string, turnId: string) => void;
}) {
  const { t } = useTranslation();
  const box = useRef<HTMLDivElement>(null);
  const stick = useRef(true);

  useEffect(() => {
    const el = box.current;
    if (el && stick.current) el.scrollTop = el.scrollHeight;
  }, [view]);

  const notices = (at: number) => view.notices.filter((n) => n.at === at).map((n) => <NoticeRow key={n.id} notice={n} />);
  // a log region, busy while a turn streams: assistive tech reads the settled turn
  // instead of every delta and output chunk
  const streaming = liveTurn(view) !== null;

  return (
    <div
      className="v2-coding-thread"
      ref={box}
      onScroll={(e) => {
        const el = e.currentTarget;
        stick.current = el.scrollHeight - el.scrollTop - el.clientHeight < 80;
      }}
      role="log"
      aria-live="polite"
      aria-busy={streaming}
      aria-label={t("v2.coding.conversation.title")}
      data-testid="v2-coding-thread"
    >
      {view.turns.length === 0 && view.notices.length === 0 && (
        <div className="v2-coding-empty">
          <SquareTerminal size={36} aria-hidden="true" />
          <div>{t("v2.coding.conversation.empty")}</div>
        </div>
      )}
      {view.turns.map((turn, i) => (
        <Fragment key={turn.turnId}>
          {notices(i)}
          <TurnBlock turn={turn} onOpenChange={onOpenChange} />
        </Fragment>
      ))}
      {notices(view.turns.length)}
    </div>
  );
}

// memoized: the reducer keeps untouched turns and items by identity, so a streamed delta
// re-renders (and re-parses the Markdown of) only what it changed
const TurnBlock = memo(function TurnBlock({
  turn,
  onOpenChange,
}: {
  turn: TurnView;
  onOpenChange: (path: string, turnId: string) => void;
}) {
  const { t } = useTranslation();
  const live = turn.status === "inProgress";
  const prompt = turnPrompt(turn);
  const items = turn.items.filter((item) => item.type !== "userMessage");
  return (
    <div className="v2-coding-turn" data-testid="v2-coding-turn" data-status={turn.status}>
      {prompt && (
        <div className="v2-coding-user">
          <div className="v2-coding-who">{t("v2.coding.conversation.you")}</div>
          <div className="v2-coding-bub">{prompt}</div>
        </div>
      )}
      {turn.plan && turn.plan.steps.length > 0 && <PlanSteps plan={turn.plan} />}
      {items.map((item) => (
        <ItemRow key={item.id} item={item} live={live} turnId={turn.turnId} onOpenChange={onOpenChange} />
      ))}
      {live && !items.some((item) => item.type === "agentMessage" || item.type === "commandExecution") && (
        <div className="v2-coding-working">
          <Loader2 size={13} className="v2-coding-spin" aria-hidden="true" />
          {t("v2.coding.conversation.working")}
        </div>
      )}
      {turn.error && turn.status !== "inProgress" && <Alert tone="error">{turn.error}</Alert>}
      <TurnFooter turn={turn} />
    </div>
  );
});

function TurnFooter({ turn }: { turn: TurnView }) {
  const { t } = useTranslation();
  const usage = turn.usage;
  return (
    <div className="v2-coding-turn-foot" data-testid="v2-coding-turn-foot">
      <TurnStatusTag status={turn.status} />
      {turn.model && <span className="mono">{turn.model}</span>}
      {turn.effort && <span>{t("v2.coding.conversation.effortOf", { effort: t(`v2.coding.effort.${turn.effort}`, turn.effort) })}</span>}
      {turn.durationMs !== null && <span>{fmtDuration(turn.durationMs)}</span>}
      {usage && (
        <span
          title={usageBreakdown(usage, {
            input: t("v2.coding.usage.input"),
            cached: t("v2.coding.usage.cached"),
            cacheWrite: t("v2.coding.usage.cacheWrite"),
            output: t("v2.coding.usage.output"),
            reasoning: t("v2.coding.usage.reasoning"),
          })}
        >
          {t("v2.coding.conversation.tokens", { value: fmtTokens(usage.total) })}
        </span>
      )}
    </div>
  );
}

function PlanSteps({ plan }: { plan: NonNullable<TurnView["plan"]> }) {
  const { t } = useTranslation();
  return (
    <div className="v2-coding-plan" data-testid="v2-coding-plan">
      <div className="v2-coding-block-head">
        <ListChecks size={14} aria-hidden="true" />
        {t("v2.coding.conversation.plan")}
      </div>
      {plan.explanation && <div className="v2-muted">{plan.explanation}</div>}
      <ul>
        {plan.steps.map((step, i) => {
          const Icon = step.status === "completed" ? CircleCheck : step.status === "inProgress" || step.status === "in_progress" ? CircleDot : Circle;
          return (
            <li key={i} data-status={step.status}>
              <Icon size={13} aria-hidden="true" />
              <span>{step.step}</span>
            </li>
          );
        })}
      </ul>
    </div>
  );
}

const ItemRow = memo(function ItemRow({
  item,
  live,
  turnId,
  onOpenChange,
}: {
  item: ItemView;
  live: boolean;
  turnId: string;
  onOpenChange: (path: string, turnId: string) => void;
}) {
  const { t } = useTranslation();
  switch (item.type) {
    case "agentMessage":
      return (
        <div className="v2-coding-msg">
          <Markdown text={item.text} />
          {item.streaming && live && <span className="v2-coding-caret" />}
        </div>
      );
    case "reasoning": {
      const text = (item.summary.some(Boolean) ? item.summary : item.content).filter(Boolean).join("\n\n");
      const streaming = item.streaming && live;
      if (!text && !streaming) return null;
      return (
        <details className="v2-coding-reasoning" open={streaming}>
          <summary>
            <ChevronRight size={12} className="v2-coding-chev" aria-hidden="true" />
            <Brain size={13} aria-hidden="true" />
            {t(streaming ? "v2.coding.conversation.reasoningLive" : "v2.coding.conversation.reasoning")}
          </summary>
          {text ? <Markdown text={text} /> : <span className="v2-muted">…</span>}
        </details>
      );
    }
    case "commandExecution":
      return <CommandBlock item={item} live={live} />;
    case "fileChange":
      return (
        <FileChangeCard changes={item.changes} status={item.status} live={live} onOpen={(path) => onOpenChange(path, turnId)} />
      );
    case "mcpToolCall":
      return (
        <div className="v2-coding-tool" data-testid="v2-coding-tool">
          <Wrench size={13} aria-hidden="true" />
          <span className="mono">{item.server ? `${item.server} · ${item.tool}` : item.tool}</span>
          {item.status === "inProgress" && live ? (
            <Loader2 size={13} className="v2-coding-spin" aria-label={t("v2.coding.conversation.running")} />
          ) : (
            <Tag tone={item.status === "completed" ? "green" : item.status === "inProgress" ? "gray" : "red"}>
              {t(`v2.coding.itemStatus.${item.status}`)}
            </Tag>
          )}
          {item.durationMs !== null && <span className="v2-muted">{fmtDuration(item.durationMs)}</span>}
          {item.error && <span className="v2-coding-tool-err">{item.error}</span>}
        </div>
      );
    case "plan":
      return (
        <div className="v2-coding-plan">
          <div className="v2-coding-block-head">
            <ListChecks size={14} aria-hidden="true" />
            {t("v2.coding.conversation.plan")}
          </div>
          <Markdown text={item.text} />
        </div>
      );
    case "error":
      return (
        <div className="v2-coding-error">
          <Alert tone={item.willRetry ? "warn" : "error"}>
            {item.willRetry ? t("v2.coding.conversation.retrying", { message: item.message }) : item.message}
          </Alert>
        </div>
      );
    case "notice":
      return <NoticeRow notice={item} />;
    case "other":
      return (
        <div className="v2-coding-note">
          <Info size={13} aria-hidden="true" />
          <span>{t("v2.coding.conversation.other", { type: item.itemType })}</span>
          {item.label && <span className="mono">{item.label}</span>}
        </div>
      );
    default:
      return null;
  }
});

function NoticeRow({ notice }: { notice: Pick<ConversationNotice, "code" | "detail"> }) {
  const { t } = useTranslation();
  return (
    <div className="v2-coding-note" data-testid={`v2-coding-notice-${notice.code}`}>
      <Info size={13} aria-hidden="true" />
      <span>{t(`v2.coding.notice.${notice.code}`)}</span>
      {notice.detail && <span className="mono">{notice.detail}</span>}
    </div>
  );
}

function CommandBlock({ item, live }: { item: Extract<ItemView, { type: "commandExecution" }>; live: boolean }) {
  const { t } = useTranslation();
  const running = item.status === "inProgress" && live;
  const failed = item.status === "failed" || item.status === "declined" || (item.exitCode !== null && item.exitCode !== 0);
  const out = useRef<HTMLPreElement>(null);
  const follow = useRef(true);
  useEffect(() => {
    const el = out.current;
    if (el && follow.current) el.scrollTop = el.scrollHeight;
  }, [item.output]);
  return (
    <details className={failed ? "v2-coding-cmd failed" : "v2-coding-cmd"} open={running || failed} data-testid="v2-coding-cmd">
      <summary>
        <ChevronRight size={12} className="v2-coding-chev" aria-hidden="true" />
        <SquareTerminal size={13} aria-hidden="true" />
        <code className="v2-coding-cmd-line" title={item.cwd ? `${item.cwd}$ ${item.command}` : item.command}>
          {item.command || t("v2.coding.conversation.command")}
        </code>
        <span className="v2-coding-cmd-meta">
          {running ? (
            <Loader2 size={13} className="v2-coding-spin" aria-label={t("v2.coding.conversation.running")} />
          ) : item.exitCode !== null ? (
            <Tag tone={item.exitCode === 0 ? "green" : "red"}>{t("v2.coding.conversation.exit", { code: item.exitCode })}</Tag>
          ) : item.status !== "inProgress" ? (
            <Tag tone={item.status === "completed" ? "green" : "red"}>{t(`v2.coding.itemStatus.${item.status}`)}</Tag>
          ) : null}
          {item.durationMs !== null && <span className="v2-muted">{fmtDuration(item.durationMs)}</span>}
        </span>
      </summary>
      {item.output ? (
        <pre
          ref={out}
          className="v2-pre v2-coding-cmd-out"
          onScroll={(e) => {
            const el = e.currentTarget;
            follow.current = el.scrollHeight - el.scrollTop - el.clientHeight < 24;
          }}
          data-testid="v2-coding-cmd-output"
        >
          {item.truncated && <span className="v2-muted">{t("v2.coding.conversation.truncated")}{"\n"}</span>}
          {item.output}
        </pre>
      ) : (
        <div className="v2-muted v2-coding-cmd-empty">
          {running ? t("v2.coding.conversation.waitingOutput") : t("v2.coding.conversation.noOutput")}
        </div>
      )}
    </details>
  );
}

const KIND_TONE = { add: "green", update: "blue", delete: "red" } as const;

function FileChangeCard({
  changes,
  status,
  live,
  onOpen,
}: {
  changes: FileChangeView[];
  status: Extract<ItemView, { type: "fileChange" }>["status"];
  live: boolean;
  onOpen: (path: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="v2-coding-files" data-testid="v2-coding-filechange">
      <div className="v2-coding-block-head">
        <FileCode size={14} aria-hidden="true" />
        {t("v2.coding.conversation.fileChanges", { count: changes.length })}
        {status === "inProgress" && live && <Loader2 size={13} className="v2-coding-spin" aria-hidden="true" />}
        {(status === "failed" || status === "declined") && <Tag tone="red">{t(`v2.coding.itemStatus.${status}`)}</Tag>}
      </div>
      <ul>
        {changes.map((change, i) => (
          <li key={`${change.path}:${i}`}>
            <Tag tone={KIND_TONE[change.kind]}>{t(`v2.coding.changeKind.${change.kind}`)}</Tag>
            <LinkButton onClick={() => onOpen(change.movePath ?? change.path)} title={t("v2.coding.conversation.openDiff")}>
              <span className="mono">
                {change.path}
                {change.movePath ? ` → ${change.movePath}` : ""}
              </span>
            </LinkButton>
            <span className="v2-coding-stat add">+{change.added}</span>
            <span className="v2-coding-stat del">−{change.removed}</span>
          </li>
        ))}
      </ul>
    </div>
  );
}
