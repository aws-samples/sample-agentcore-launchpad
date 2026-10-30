import { ExternalLink, RefreshCw } from "lucide-react";
import { useTranslation } from "react-i18next";

import type { ChatTraceInfo } from "../../../lib/api";
import { Alert, Button, LinkButton } from "../../ui";
import { classicObservabilityToV2 } from "../observability/classicUrl";
import { SPAN_COLOR, TRACE_ROWS } from "./traceMeta";

/**
 * Inspector → TRACE: this session's spans as a compact waterfall (name, category
 * colour, duration), a LOAD TRACE button (spans land in CloudWatch about a minute after
 * the turn, so the rail is pull-based), and two deep links — the session detail of the
 * Observability page and the partition-aware CloudWatch Logs Insights query the
 * backend ran (`cloudwatch_url`).
 */
export function TracePanel({
  sessionId,
  trace,
  busy,
  error,
  onLoad,
}: {
  sessionId: string | null;
  trace: ChatTraceInfo | null;
  busy: boolean;
  /** Load failure (an HTTP error, not an empty result) — shown with a retry. */
  error?: string | null;
  onLoad: () => void;
}) {
  const { t } = useTranslation();
  const spans = trace?.spans.slice(0, TRACE_ROWS) ?? [];
  const total = Math.max(...spans.map((s) => (s.start_ms ?? 0) + (s.duration_ms ?? 0)), 1);
  const hidden = trace ? trace.span_count - spans.length : 0;
  return (
    <div className="v2-stack" data-testid="trace-panel">
      <div className="v2-row">
        <Button size="sm" disabled={!sessionId || busy} onClick={onLoad} testId="trace-load">
          <RefreshCw size={13} aria-hidden="true" />
          {busy ? t("v2.common.loading") : t("v2.chat.traceLoad")}
        </Button>
        {sessionId && (
          <a
            className="v2-chat-extlink"
            href={classicObservabilityToV2(`?session=${encodeURIComponent(sessionId)}`)}
            target="_blank"
            rel="noreferrer"
            data-testid="open-in-obs"
          >
            {t("v2.chat.openInObs")}
            <ExternalLink size={12} aria-hidden="true" />
          </a>
        )}
        {trace?.cloudwatch_url && (
          <a
            className="v2-chat-extlink"
            href={trace.cloudwatch_url}
            target="_blank"
            rel="noreferrer"
            data-testid="trace-cloudwatch"
          >
            CloudWatch
            <ExternalLink size={12} aria-hidden="true" />
          </a>
        )}
      </div>
      {sessionId && <div className="v2-muted mono v2-chat-sid">session {sessionId}</div>}
      {error && (
        <div data-testid="trace-error">
          <Alert tone="error" action={<LinkButton onClick={onLoad}>{t("v2.common.retry")}</LinkButton>}>
            {error}
          </Alert>
        </div>
      )}
      {trace?.unavailable_reason && (
        <div data-testid="trace-unavailable">
          <Alert tone="warn">{t("v2.chat.traceUnavailable", { code: trace.unavailable_reason })}</Alert>
        </div>
      )}
      {trace && trace.span_count > 0 ? (
        <>
          <div data-testid="trace-rows">
            {spans.map((span, i) => (
              <div className="v2-span" key={i} data-testid="trace-row" data-category={span.category}>
                <span className="name" title={`${span.name} · ${t(`v2.chat.spanCat.${span.category}`)}`}>
                  {span.name}
                </span>
                <span className="track">
                  <span
                    style={{
                      left: `${((span.start_ms ?? 0) / total) * 100}%`,
                      width: `${Math.max(((span.duration_ms ?? 0) / total) * 100, 0.8)}%`,
                      background: SPAN_COLOR[span.category] ?? SPAN_COLOR.other,
                    }}
                  />
                </span>
                <span className="dur">{Math.round(span.duration_ms ?? 0)}ms</span>
              </div>
            ))}
          </div>
          {hidden > 0 && <div className="v2-muted">{t("v2.chat.traceMore", { count: hidden })}</div>}
          <div className="v2-chat-legend">
            {(["model", "tool", "memory", "policy", "runtime"] as const).map((cat) => (
              <span key={cat}>
                <i style={{ background: SPAN_COLOR[cat] }} />
                {t(`v2.chat.spanCat.${cat}`)}
              </span>
            ))}
          </div>
        </>
      ) : (
        <div className="v2-chat-rail-empty" data-testid="trace-empty">
          {sessionId ? (trace ? t("v2.chat.traceEmpty") : t("v2.chat.traceIdle")) : t("v2.chat.tracePlaceholder")}
        </div>
      )}
    </div>
  );
}
