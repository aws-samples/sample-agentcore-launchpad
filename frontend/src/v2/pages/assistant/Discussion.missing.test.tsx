// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { createRef } from "react";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import type { AssistantConversationDetail } from "../../../lib/api";
import { type AssistantLiveMessage, PROPOSAL_MISSING, PROPOSAL_MISSING_RECOVERED } from "../../../lib/assistant";
import en from "../../../locales/en/common.json";
import { DiscussionCard } from "./Discussion";

/**
 * N17-obs468 (live 2026-10-26, conversation 785829de): the architect said 「修订已提交」 twice
 * while no revision 2 existed. Every answered turn now says whether it produced a revision,
 * and a reply without a block carries a notice naming the unchanged revision.
 */

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(() => cleanup());

const conversation = (turnRevisions?: Record<string, number[]>) => ({
  id: "785829dea85f46ba97170b13692b016e", title: "travel", turn_in_progress: null,
  catalog: { fetched_at: null, tools: [], skills: [], knowledge_bases: [], warnings: [] },
  messages: [], proposals: [],
  ...(turnRevisions ? { turn_revisions: turnRevisions } : {}),
}) as unknown as AssistantConversationDetail;

const MESSAGES: AssistantLiveMessage[] = [
  { role: "user", text: "propose", turn: 1 },
  { role: "assistant", text: "Here is the design.", turn: 1 },
  { role: "user", text: "更新提案", turn: 2 },
  { role: "assistant", text: "修订已提交。", turn: 2 },
  { role: "error", name: PROPOSAL_MISSING, text: "x", turn: 2, missing: { base: 1, revision: null } },
];

function show(messages: AssistantLiveMessage[], turnRevisions?: Record<string, number[]>) {
  return render(
    <MemoryRouter>
      <DiscussionCard conversation={conversation(turnRevisions)} messages={messages} input="" onInput={() => undefined}
        busy={false} preparing={false} onSend={() => undefined} onRefreshCatalog={() => undefined}
        threadRef={createRef<HTMLDivElement>()} retryOf={null} onRetry={() => undefined} />
    </MemoryRouter>,
  );
}

describe("turn revision state (N17-obs468)", () => {
  it("each answered turn says whether it produced a revision", () => {
    show(MESSAGES, { 1: [1], 2: [] });
    const lines = screen.getAllByTestId("v2-assistant-turn-revision");
    expect(lines.map((l) => [l.getAttribute("data-turn"), l.textContent])).toEqual([
      ["1", "This turn produced revision r1"],
      ["2", "This turn did not change the proposal"],
    ]);
  });

  it("the notice names the revision that is still current", () => {
    show(MESSAGES, { 1: [1], 2: [] });
    const notice = screen.getByTestId("v2-assistant-proposal-missing");
    expect(notice.getAttribute("data-state")).toBe("missing");
    expect(notice.textContent).toContain("still r1");
  });

  it("a recovered re-ask says which revision it stored", () => {
    show([...MESSAGES.slice(0, 4),
      { role: "error", name: PROPOSAL_MISSING_RECOVERED, text: "x", turn: 2, missing: { base: 1, revision: 2 } },
      { role: "assistant", text: "已更新。", turn: 2 }], { 1: [1], 2: [2] });
    expect(screen.getByTestId("v2-assistant-proposal-missing").textContent).toContain("r2");
    expect(screen.getAllByTestId("v2-assistant-turn-revision")[1].textContent).toBe("This turn produced revision r2");
  });

  it("says nothing on an older backend without turn revisions", () => {
    show(MESSAGES);
    expect(screen.queryByTestId("v2-assistant-turn-revision")).toBeNull();
  });
});
