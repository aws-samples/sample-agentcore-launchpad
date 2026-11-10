// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { MemoryRecord } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { LongTermTab } from "./LongTermTab";
import { recordState } from "./common";

/**
 * r14-a (N14-obs1): the long-term tab hides superseded records by default; the
 * "Show superseded" toggle re-lists with `include_superseded` and a State column.
 */

const ACTOR = "agent1__river";
const rec = (over: Partial<MemoryRecord>): MemoryRecord => ({
  record_id: "r1",
  text: "User works at Globex",
  structured: null,
  raw_text: "User works at Globex",
  strategy_id: "semantic",
  namespaces: [`/facts/${ACTOR}`],
  created_at: "2026-10-18T12:00:00Z",
  score: null,
  metadata: {},
  ...over,
});

const memoryRecords = vi.fn(async (params: { include_superseded?: boolean }) => ({
  namespace: `/facts/${ACTOR}`,
  next_token: null,
  items: params.include_superseded
    ? [rec({ active: true }), rec({ record_id: "r0", text: "User works at Acme", superseded_by: "r1", active: false })]
    : [rec({ active: true })],
}));

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      memoryNamespaces: vi.fn(async () => ({
        items: [
          {
            strategy_id: "semantic",
            strategy_name: "Semantic facts",
            strategy_type: "SEMANTIC",
            template: "/facts/{actorId}",
            namespace: `/facts/${ACTOR}`,
            resolvable: true,
            prefix: false,
          },
        ],
      })),
      memoryRecords: (params: { include_superseded?: boolean }) => memoryRecords(params),
    },
  };
});
vi.mock("../../hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../hooks")>()),
  useV2Toast: () => () => undefined,
}));

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

describe("LongTermTab show-superseded toggle (N14-obs1)", () => {
  it("lists active records only, then the superseded ones with their state", async () => {
    render(
      <LongTermTab
        actors={{ items: [], token: null, loading: false, error: null, loadMore: () => undefined, reload: () => undefined }}
        strategies={[]}
        actorId={ACTOR}
        strategyId="semantic"
        onSelectActor={() => undefined}
        onSelectStrategy={() => undefined}
      />,
    );
    await screen.findByText("User works at Globex");
    expect(memoryRecords).toHaveBeenLastCalledWith(expect.objectContaining({ include_superseded: false }));
    expect(screen.queryByText("User works at Acme")).toBeNull();
    expect(screen.queryByText("Superseded")).toBeNull();

    fireEvent.click(screen.getByTestId("v2-memory-show-superseded"));
    await screen.findByText("User works at Acme");
    expect(memoryRecords).toHaveBeenLastCalledWith(expect.objectContaining({ include_superseded: true }));
    await waitFor(() => expect(screen.getByText("Superseded")).toBeTruthy());
    expect(screen.getByText("Active")).toBeTruthy();
  });

  it("derives the state from the op-history fields; a legacy record is active", () => {
    expect(recordState(rec({}))).toBe("active");
    expect(recordState(rec({ superseded_by: "x" }))).toBe("superseded");
    expect(recordState(rec({ deleted_at: "2026-10-18T00:00:00Z" }))).toBe("deleted");
    expect(recordState(rec({ expired_at: "2026-10-18T00:00:00Z" }))).toBe("expired");
  });
});
