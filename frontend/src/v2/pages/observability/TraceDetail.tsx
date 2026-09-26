import { RefreshCw } from "lucide-react";
import { useMemo, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import {
  api,
  ApiError,
  type ObsSpanMessage,
  type ObsSpanNode,
  type ObsTraceDetail,
  type V2Range,
} from "../../../lib/api";
import { fmtDuration, fmtNumber, fmtTime } from "../../format";
import { useLoad } from "../../hooks";
import { Alert, Button, Card, Descriptions, FlowHeader, LinkButton, Spin, Tag } from "../../ui";
import { approxCost, shortId, TRACE_ID_RE } from "./common";
import { CopyId } from "./SessionList";
import { TraceStatusTag } from "./TraceList";

type Category = ObsSpanNode["category"];
const CATEGORIES: Category[] = ["llm", "tool", "memory", "gateway", "http", "agent", "other"];

function flatten(nodes: ObsSpanNode[], out: ObsSpanNode[] = []): ObsSpanNode[] {
  for (const node of nodes) {
    out.push(node);
    flatten(node.children, out);
  }
  return out;
}

function MessageList({ label, messages }: { label: string; messages: ObsSpanMessage[] }) {
  return (
    <>
      <div className="v2-sub-title">{label}</div>
      <div className="v2-observability-msgs">
        {messages.map((message, i) => (
          <div className="v2-observability-msg" key={i}>
            <div className="role">
              {message.role ?? "—"}
              {message.finish_reason != null && <span className="v2-muted"> · {message.finish_reason}</span>}
            </div>
            {message.blocks.map((block, j) =>
              block.type === "text" ? (
                <div className="text" key={j}>
                  {block.text}
                </div>
              ) : block.type === "tool_use" ? (
                <div className="tool" key={j}>
                  ⇄ {block.name ?? "tool"}({block.input})
                </div>
              ) : block.type === "tool_result" ? (
                <div className="tool" key={j}>
                  ✓ {block.status ?? "result"} · {block.text}
                </div>
              ) : (
                <div className="text v2-muted" key={j}>
                  {block.text}
                </div>
              ),
            )}
          </div>
        ))}
      </div>
    </>
  );
}

function SpanPanel({ span, detail }: { span: ObsSpanNode; detail: ObsTraceDetail }) {
  const { t } = useTranslation();
  const [allAttrs, setAllAttrs] = useState(false);
  const flat = detail.spans.find((s) => s.span_id != null && s.span_id === span.span_id);
  const attributes = flat?.attributes ?? {};
  const messages = flat?.messages ?? null;
  const attrs = Object.entries(attributes);
  const toolAttrs = attrs.filter(([k]) => k.startsWith("gen_ai.tool.") && k !== "gen_ai.tool.name");
  const operation = (attributes["gen_ai.operation.name"] as string | undefined) ?? span.name;
  const provider =
    (attributes["gen_ai.system"] as string | undefined) ?? (attributes["gen_ai.provider.name"] as string | undefined) ?? null;
  const finish = Array.isArray(span.finish_reason) ? span.finish_reason.join(", ") : span.finish_reason;
  const httpStatus =
    (attributes["http.response.status_code"] as number | undefined) ??
    (attributes["http.status_code"] as number | undefined) ??
    null;
  const failed = span.status === "ERROR";
  const shown = allAttrs ? attrs : attrs.slice(0, 12);

  return (
    <Card
      title={t("v2.observability.span.title")}
      sub={`${operation} · ${span.kind ?? "—"}`}
      end={<span className={`v2-observability-cat ${span.category}`}>{t(`v2.spanCategory.${span.category}`)}</span>}
      testId="v2-obs-span"
    >
      <Descriptions
        one
        items={[
          { label: t("v2.observability.span.name"), value: <span className="mono">{span.name}</span> },
          ...(span.model != null ? [{ label: t("v2.observability.span.model"), value: <span className="mono">{span.model}</span> }] : []),
          ...(provider != null ? [{ label: t("v2.observability.span.provider"), value: provider }] : []),
          ...(finish != null ? [{ label: t("v2.observability.span.finish"), value: finish }] : []),
          { label: t("v2.observability.span.startOffset"), value: `+${fmtDuration(span.start_offset_ms)}` },
          { label: t("v2.observability.col.duration"), value: fmtDuration(span.duration_ms) },
          {
            label: t("v2.observability.col.status"),
            value: (
              <Tag tone={failed ? "red" : "green"}>
                {span.status}
                {httpStatus != null ? ` · http ${httpStatus}` : ""}
              </Tag>
            ),
          },
          ...(span.tool_name != null ? [{ label: t("v2.observability.span.tool"), value: <span className="mono">{span.tool_name}</span> }] : []),
          ...toolAttrs.slice(0, 4).map(([k, v]) => ({
            label: k.replace("gen_ai.tool.", ""),
            value: <span className="mono">{String(v).slice(0, 120)}</span>,
          })),
        ]}
      />
      {span.tokens != null && (
        <>
          <div className="v2-sub-title">{t("v2.observability.span.tokens")}</div>
          <Descriptions
            one
            items={[
              { label: t("v2.observability.chart.input"), value: fmtNumber(span.tokens.input) },
              { label: t("v2.observability.chart.output"), value: fmtNumber(span.tokens.output) },
              { label: t("v2.observability.span.cacheRw"), value: `${fmtNumber(span.tokens.cache_read)} / ${fmtNumber(span.tokens.cache_write)}` },
              { label: t("v2.observability.span.cost"), value: approxCost(span.est_cost_usd) },
            ]}
          />
        </>
      )}
      {messages?.input != null && messages.input.length > 0 && (
        <MessageList label={t("v2.observability.span.inputMessages", { count: messages.input.length })} messages={messages.input} />
      )}
      {messages?.output != null && messages.output.length > 0 && (
        <MessageList label={t("v2.observability.span.outputMessages", { count: messages.output.length })} messages={messages.output} />
      )}
      <div className="v2-sub-title v2-row">
        {t("v2.observability.span.attributes", { count: attrs.length })}
        {attrs.length > 12 && (
          <LinkButton onClick={() => setAllAttrs((v) => !v)}>
            {allAttrs ? t("v2.common.collapse") : t("v2.observability.span.showAll")}
          </LinkButton>
        )}
      </div>
      {attrs.length === 0 ? (
        <p className="v2-muted">{t("v2.observability.span.noAttributes")}</p>
      ) : (
        <pre className="v2-pre">{JSON.stringify(Object.fromEntries(shown), null, 2)}</pre>
      )}
    </Card>
  );
}

export function TraceDetail({ traceId, range }: { traceId: string; range: V2Range }) {
  const { t } = useTranslation();
  const [, setParams] = useSearchParams();
  const [force, setForce] = useState(0);
  const load = useLoad<ObsTraceDetail | "invalid">(
    () =>
      TRACE_ID_RE.test(traceId)
        ? api.obsTrace(traceId, range, force > 0).catch((err: unknown) => {
            if (err instanceof ApiError && err.code === "validation.invalid_request") return "invalid" as const;
            throw err;
          })
        : Promise.resolve("invalid" as const),
    `obs-trace:${traceId}:${range}:${force}`,
  );
  const [selected, setSelected] = useState<string | null>(null);

  const detail = load.data && load.data !== "invalid" ? load.data : null;
  const rows = useMemo(() => (detail ? flatten(detail.tree) : []), [detail]);
  const selectedSpan =
    rows.find((r) => r.span_id === selected) ?? rows.find((r) => r.category === "llm") ?? rows[0] ?? null;

  const rangeParam: Record<string, string> = range !== "24h" ? { range } : {};
  const back = () => setParams({ tab: "traces", ...rangeParam });
  const openSession = (id: string) => setParams({ tab: "sessions", view: "session", id, ...rangeParam });
  const meta = detail?.meta;
  const notFound = load.data === "invalid" || (meta != null && meta.span_count === 0);

  return (
    <>
      <FlowHeader
        title={
          <span className="v2-row">
            {t("v2.observability.traceTitle", { id: shortId(traceId, 16) })}
            {meta && !notFound && <TraceStatusTag status={meta.status} durationMs={meta.duration_ms} />}
          </span>
        }
        onBack={back}
        end={
          <>
            {meta?.session_id && (
              <Button onClick={() => openSession(meta.session_id as string)} testId="v2-obs-trace-session">
                {t("v2.observability.openSession")}
              </Button>
            )}
            <Button onClick={() => setForce((n) => n + 1)} disabled={load.loading}>
              <RefreshCw size={14} aria-hidden="true" />
              {t("v2.common.refresh")}
            </Button>
          </>
        }
      />
      {load.loading && !load.data ? (
        <Spin />
      ) : load.error && !load.data ? (
        <Alert tone="error" action={<LinkButton onClick={load.reload}>{t("v2.common.retry")}</LinkButton>}>
          {t("obs.loadFailed", { msg: load.error })}
        </Alert>
      ) : notFound ? (
        <Card>
          <div className="v2-observability-empty" data-testid="v2-obs-trace-notfound">
            {t("v2.observability.traceNotFound")}
          </div>
        </Card>
      ) : detail && meta ? (
        <>
          {load.error && <Alert tone="error">{t("obs.loadFailed", { msg: load.error })}</Alert>}
          <Card title={t("v2.observability.summary")}>
            <Descriptions
              items={[
                {
                  label: "Trace ID",
                  value: (
                    <span className="v2-observability-idcell">
                      <span className="mono">{detail.trace_id}</span>
                      <CopyId id={detail.trace_id} />
                    </span>
                  ),
                },
                { label: "Agent", value: meta.agent || "—" },
                {
                  label: t("v2.observability.col.session"),
                  value: meta.session_id ? (
                    <LinkButton onClick={() => openSession(meta.session_id as string)} title={meta.session_id}>
                      <span className="mono">{shortId(meta.session_id, 28)}</span>
                    </LinkButton>
                  ) : (
                    "—"
                  ),
                },
                { label: t("v2.observability.startTime"), value: fmtTime(meta.start) },
                { label: t("v2.observability.rootOp"), value: meta.root_operation ?? "—" },
                { label: t("v2.observability.service"), value: meta.service ? <span className="mono">{meta.service}</span> : "—" },
                { label: t("v2.observability.col.duration"), value: fmtDuration(meta.duration_ms) },
                { label: t("v2.observability.col.spansLlm"), value: `${meta.span_count} / ${meta.llm_count}` },
                {
                  label: "Tokens",
                  value: t("v2.observability.tokensDetail", {
                    total: fmtNumber(meta.tokens.total),
                    input: fmtNumber(meta.tokens.input),
                    output: fmtNumber(meta.tokens.output),
                  }),
                },
                { label: t("v2.observability.span.cost"), value: approxCost(meta.est_cost_usd) },
              ]}
            />
          </Card>
          <div className="v2-observability-trace">
            <Card
              title={t("v2.observability.waterfall")}
              sub={t("v2.observability.waterfallSub", { count: rows.length, dur: fmtDuration(meta.duration_ms) })}
              testId="v2-obs-waterfall"
            >
              <div className="v2-observability-legend" style={{ marginTop: 0, marginBottom: 12 }}>
                {CATEGORIES.filter((c) => rows.some((r) => r.category === c)).map((c) => (
                  <span key={c}>
                    <i className={`v2-observability-dot ${c}`} />
                    {t(`v2.spanCategory.${c}`)}
                  </span>
                ))}
              </div>
              <div className="v2-observability-wf">
                <div className="h">{t("v2.observability.span.name")}</div>
                <div className="h">{t("v2.observability.timeline")}</div>
                <div className="h right">{t("v2.observability.col.duration")}</div>
                {rows.map((row, i) => {
                  const on = selectedSpan?.span_id === row.span_id ? " on" : "";
                  const pick = () => setSelected(row.span_id);
                  return (
                    <div className={`r${on}`} key={row.span_id ?? `${row.name}:${i}`} onClick={pick} role="button" tabIndex={0}
                      onKeyDown={(e) => (e.key === "Enter" || e.key === " ") && pick()}
                      data-testid="v2-obs-wf-row"
                    >
                      <span className="nm" style={{ paddingLeft: Math.min(row.depth, 8) * 14 }} title={row.name}>
                        <i className={`v2-observability-dot ${row.category}`} />
                        <span className="t">{row.tool_name ?? row.name}</span>
                        {row.status === "ERROR" && <Tag tone="red">ERROR</Tag>}
                      </span>
                      <span className="lane">
                        <span className="track">
                          <span
                            className={`bar ${row.category}`}
                            style={{ left: `${row.offset_pct}%`, width: `${Math.max(row.width_pct, 0.4)}%` }}
                          />
                        </span>
                      </span>
                      <span className="dur">{fmtDuration(row.duration_ms)}</span>
                    </div>
                  );
                })}
              </div>
            </Card>
            <div className="v2-observability-side">
              {selectedSpan && <SpanPanel key={selectedSpan.span_id ?? ""} span={selectedSpan} detail={detail} />}
            </div>
          </div>
        </>
      ) : null}
    </>
  );
}
