// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import type { ChatSessionInfo } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { SessionRail } from "./SessionRail";

/**
 * N-SCN-obs1: a session opened through the public /v1 API is listed like a console
 * one and carries a "Public API" badge; console rows (and rows from a backend that
 * predates the `source` field) carry none.
 */

const row = (over: Partial<ChatSessionInfo>): ChatSessionInfo => ({
  session_id: "s".repeat(33),
  actor_id: "river",
  turns: 1,
  last_at: "2026-10-14T00:00:00Z",
  ended_at: null,
  preview: "hello",
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

describe("SessionRail source badge (N-SCN-obs1)", () => {
  it("marks public-API sessions and leaves console / legacy rows unmarked", () => {
    render(
      <SessionRail
        sessions={[
          row({ session_id: "v1-" + "a".repeat(30), actor_id: "api", preview: "via v1", source: "public_api" }),
          row({ session_id: "c-" + "b".repeat(30), preview: "via console", source: "console" }),
          row({ session_id: "old-" + "c".repeat(29), preview: "legacy row" }),
        ]}
        currentId={null}
        busy={false}
        ending={null}
        endReason={() => undefined}
        onOpen={() => undefined}
        onEnd={() => undefined}
      />,
    );
    const badges = screen.getAllByTestId("session-source-public-api");
    expect(badges).toHaveLength(1);
    expect(badges[0].textContent).toBe("Public API");
    expect(badges[0].closest(".v2-chat-session")?.textContent).toContain("via v1");
    expect(screen.getByText("via console").closest(".v2-chat-session")?.textContent).not.toContain("Public API");
    expect(screen.getByText("legacy row").closest(".v2-chat-session")?.textContent).not.toContain("Public API");
  });
});
