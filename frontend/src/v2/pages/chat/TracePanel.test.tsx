// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { ChatTraceInfo } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { TracePanel } from "./TracePanel";
import { TRACE_ROWS } from "./traceMeta";

/**
 * N-CHAT-obs2: the Chat Inspector's TRACE tab (upstream parity). Pull-based rail over
 * `GET /api/traces/{session_id}`: placeholder without a session, idle / empty copy with
 * one, span rows with category + duration, the "+N more" line past the row cap, and the
 * partition-aware CloudWatch deep link exactly as the backend built it.
 */

const SID = "s".repeat(33);
const CN_URL =
  "https://cn-northwest-1.console.amazonaws.cn/cloudwatch/home?region=cn-northwest-1#logsV2:logs-insights$3FqueryDetail$3Dx";

const info = (over: Partial<ChatTraceInfo> = {}): ChatTraceInfo => ({
  session_id: SID,
  span_count: 2,
  spans: [
    { name: "POST /invocations", category: "runtime", start_ms: 0, duration_ms: 2000 },
    { name: "chat claude-sonnet", category: "model", start_ms: 200, duration_ms: 800.4 },
  ],
  cloudwatch_url: CN_URL,
  unavailable_reason: null,
  ...over,
});

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(cleanup);

describe("TracePanel (N-CHAT-obs2)", () => {
  it("without a session: placeholder, LOAD TRACE disabled, no links", () => {
    render(<TracePanel sessionId={null} trace={null} busy={false} onLoad={() => undefined} />);
    expect(screen.getByTestId("trace-empty").textContent).toBe(
      "Send a message — the trace rail loads this session's spans",
    );
    expect((screen.getByTestId("trace-load") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.queryByTestId("open-in-obs")).toBeNull();
    expect(screen.queryByTestId("trace-cloudwatch")).toBeNull();
  });

  it("with a session but nothing loaded: idle copy, the button loads, Observability link present", () => {
    const onLoad = vi.fn();
    render(<TracePanel sessionId={SID} trace={null} busy={false} onLoad={onLoad} />);
    expect(screen.getByTestId("trace-empty").textContent).toBe("Load the trace to see this session's spans.");
    expect(screen.getByTestId("open-in-obs").getAttribute("href")).toBe(
      `/v2/observability?tab=sessions&view=session&id=${SID}`,
    );
    fireEvent.click(screen.getByTestId("trace-load"));
    expect(onLoad).toHaveBeenCalledTimes(1);
  });

  it("while loading: the button is disabled and reads Loading", () => {
    render(<TracePanel sessionId={SID} trace={null} busy onLoad={() => undefined} />);
    const button = screen.getByTestId("trace-load") as HTMLButtonElement;
    expect(button.disabled).toBe(true);
    expect(button.textContent).toContain("Loading…");
  });

  it("an empty answer: the ~1 min copy, and the CloudWatch link still opens the query", () => {
    render(
      <TracePanel sessionId={SID} trace={info({ span_count: 0, spans: [] })} busy={false} onLoad={() => undefined} />,
    );
    expect(screen.getByTestId("trace-empty").textContent).toBe(
      "No spans yet — traces land in about a minute; reload.",
    );
    expect(screen.queryByTestId("trace-rows")).toBeNull();
    expect(screen.getByTestId("trace-cloudwatch").getAttribute("href")).toBe(CN_URL);
  });

  it("rows: name, category, rounded duration; the CloudWatch link is the backend's URL verbatim", () => {
    render(<TracePanel sessionId={SID} trace={info()} busy={false} onLoad={() => undefined} />);
    const rows = screen.getAllByTestId("trace-row");
    expect(rows).toHaveLength(2);
    expect(rows[0].getAttribute("data-category")).toBe("runtime");
    expect(rows[0].textContent).toContain("POST /invocations");
    expect(rows[0].textContent).toContain("2000ms");
    expect(rows[1].getAttribute("data-category")).toBe("model");
    expect(rows[1].textContent).toContain("800ms");
    const link = screen.getByTestId("trace-cloudwatch");
    expect(link.getAttribute("href")).toBe(CN_URL);
    expect(link.getAttribute("target")).toBe("_blank");
    // the legend names every category the rail colours
    expect(screen.getByText("Policy")).toBeTruthy();
    expect(screen.queryByTestId("trace-empty")).toBeNull();
  });

  it("past the row cap: only TRACE_ROWS rows and a +N more line", () => {
    const spans = Array.from({ length: TRACE_ROWS + 5 }, (_, i) => ({
      name: `span-${i}`,
      category: "tool" as const,
      start_ms: i * 10,
      duration_ms: 5,
    }));
    render(
      <TracePanel
        sessionId={SID}
        trace={info({ span_count: spans.length, spans })}
        busy={false}
        onLoad={() => undefined}
      />,
    );
    expect(screen.getAllByTestId("trace-row")).toHaveLength(TRACE_ROWS);
    expect(screen.getByText(/\+5 more spans/)).toBeTruthy();
  });

  it("a failed query is a warning on top of an empty rail; a load error offers retry", () => {
    const onLoad = vi.fn();
    render(
      <TracePanel
        sessionId={SID}
        trace={info({ span_count: 0, spans: [], unavailable_reason: "observability.query_failed" })}
        busy={false}
        error="HTTP 502"
        onLoad={onLoad}
      />,
    );
    expect(screen.getByTestId("trace-unavailable").textContent).toContain("observability.query_failed");
    expect(screen.getByTestId("trace-error").textContent).toContain("HTTP 502");
    fireEvent.click(screen.getByText("Retry"));
    expect(onLoad).toHaveBeenCalledTimes(1);
  });
});
