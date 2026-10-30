// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { AssistantConversationDetail, AssistantStatus } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { PreparationCard } from "./Preparation";

/**
 * N-ARCH-obs8: with an MCP server ticked while the assistant was still generating a
 * turn, "Add to current plan" stayed disabled with no explanation. The parent now
 * passes the reason; the button carries it as its tooltip and the panel shows it inline.
 */

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listAttachableKnowledgeBases: vi.fn(async () => ({ items: [] })),
      assistantRefreshCatalog: vi.fn(),
      assistantSavePreparation: vi.fn(),
      getKnowledgeBase: vi.fn(),
    },
  };
});
vi.mock("./KnowledgeBaseCreate", () => ({ KnowledgeBaseCreateDrawer: () => null }));

const conversation = (turnInProgress: number | null): AssistantConversationDetail =>
  ({
    id: "conv-1",
    turn_in_progress: turnInProgress,
    messages: [],
    proposals: [],
    preparation: { revision: 1, knowledge_bases: [], skills: [], tools: [], requirements: [] },
    catalog: {
      fetched_at: "2026-10-14T00:00:00Z",
      tools: [{ key: "mcp:office-facts", kind: "mcp", name: "office-facts", description: "", attachable: true }],
      skills: [],
      knowledge_bases: [],
      warnings: [],
    },
  }) as unknown as AssistantConversationDetail;

const capabilities = { knowledge_bases: true } as unknown as AssistantStatus["capabilities"];

function renderCard(conv: AssistantConversationDetail, disabled: boolean, disabledReason?: string) {
  return render(
    <MemoryRouter>
      <PreparationCard
        conversation={conv}
        workspaceId="ws"
        capabilities={capabilities}
        disabled={disabled}
        disabledReason={disabledReason}
        locked={false}
        onDirty={() => undefined}
        onUpdated={() => undefined}
        onCatalog={() => undefined}
        onWorking={() => undefined}
        onDiscuss={() => undefined}
      />
    </MemoryRouter>,
  );
}

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

describe("PreparationCard save-button reason (N-ARCH-obs8)", () => {
  it("names the in-progress turn when the parent disables the panel with a changed selection", () => {
    const reason = en.assistantPreparation.waitForTurn;
    renderCard(conversation(3), true, reason);
    fireEvent.click(screen.getByLabelText("office-facts"));
    const save = screen.getByTestId("v2-assistant-prep-save") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(save.getAttribute("title")).toBe(reason);
    expect(screen.getByTestId("v2-assistant-prep-save-reason").textContent).toBe(reason);
  });

  it("says the selection is already in the plan when nothing changed, and is silent once enabled", () => {
    renderCard(conversation(null), false);
    const save = screen.getByTestId("v2-assistant-prep-save") as HTMLButtonElement;
    expect(save.disabled).toBe(true);
    expect(save.getAttribute("title")).toBe(en.assistantPreparation.nothingToSave);
    expect(screen.queryByTestId("v2-assistant-prep-save-reason")).toBeNull();
    fireEvent.click(screen.getByLabelText("office-facts"));
    expect(save.disabled).toBe(false);
    expect(save.getAttribute("title")).toBeNull();
  });
});
