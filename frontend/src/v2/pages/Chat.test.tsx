// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../auth/auth-context";
import type { AgentInfo } from "../../lib/api";
import en from "../../locales/en/common.json";
import { ToastContext } from "../hooks";
import { V2Chat } from "./Chat";

/**
 * N17-obs114: a runtime that completed with EMPTY text used to stream `meta → done` with no
 * delta and no error, so the Chat page showed the user bubble and nothing else — no agent
 * bubble, no error row. The hub now ends such a turn on `error {code: agent.empty_answer}`;
 * this pins that the page renders that event as an error row (the `apiErrors` copy).
 */

const agent: AgentInfo = {
  id: "a1", name: "hr-notice-byoc", method: "byoc", status: "active", arn: null, resource_id: null,
  version: null, created_by: "u-ada", created_by_name: "ada", error: null, spec: {},
  invoke_capability: { eligible: true, reason: null, reason_code: null },
} as AgentInfo;

const frame = (event: string, data: unknown, seq: number) =>
  `id: ${seq}\nevent: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

function sse(frames: string[]): Response {
  const encoder = new TextEncoder();
  const stream = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const f of frames) controller.enqueue(encoder.encode(f));
      controller.close();
    },
  });
  return new Response(stream, { headers: { "content-type": "text/event-stream" } });
}

const SID = "s".repeat(40);
let streamFrames: string[] = [];

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listAgents: () => Promise.resolve({ agents: [agent] }),
      listChatSessions: () => Promise.resolve({ sessions: [] }),
    },
    chatApi: {
      ...actual.chatApi,
      stream: async () => sse(streamFrames),
      memory: () => Promise.reject(new Error("no memory in this test")),
    },
  };
});

const auth: AuthContextValue = {
  authRequired: true, username: "ada", userId: "u-ada", role: "member", email: null,
  accountExpiresAt: null, isAdmin: false, can: () => true, logout: async () => undefined,
};

function mount() {
  return render(
    <ToastContext.Provider value={() => undefined}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={["/v2/chat?agent=a1"]}>
          <V2Chat />
        </MemoryRouter>
      </AuthContext.Provider>
    </ToastContext.Provider>,
  );
}

beforeAll(async () => {
  // jsdom has no Element.scrollTo; the Thread scrolls to the newest row on every change
  Element.prototype.scrollTo = () => undefined;
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(() => {
  cleanup();
  streamFrames = [];
});

describe("V2Chat — the empty-answer turn (N17-obs114)", () => {
  it("renders an error row when the hub ends the turn on error agent.empty_answer", async () => {
    streamFrames = [
      frame("meta", { session_id: SID, agent: "hr-notice-byoc", mode: "stream", turn: 1 }, 1),
      frame("error", {
        code: "agent.empty_answer",
        message: "the runtime completed without an answer",
        detail: { reason: "empty_answer" },
      }, 2),
    ];
    mount();
    const input = (await screen.findByTestId("chat-input")) as HTMLTextAreaElement;
    await waitFor(() => expect(input.disabled).toBe(false));
    fireEvent.change(input, { target: { value: "A puzzle" } });
    fireEvent.click(screen.getByTestId("chat-send"));

    await waitFor(() => expect(document.querySelector(".v2-chat-error")).not.toBeNull());
    const row = document.querySelector(".v2-chat-error") as HTMLElement;
    expect(row.textContent).toContain(en.apiErrors["agent.empty_answer"]);
    // the user bubble stays (and the failed prompt is restored into the composer)
    expect(screen.getAllByText("A puzzle").length).toBeGreaterThanOrEqual(1);
    expect((screen.getByTestId("chat-input") as HTMLTextAreaElement).value).toBe("A puzzle");
    // the composer is usable again — the turn ended
    await waitFor(() =>
      expect((screen.getByTestId("chat-input") as HTMLTextAreaElement).disabled).toBe(false));
  });

  it("keeps rendering a completed turn as an agent bubble (control)", async () => {
    streamFrames = [
      frame("meta", { session_id: SID, agent: "hr-notice-byoc", mode: "stream", turn: 1 }, 1),
      frame("delta", { text: "Hello from the model" }, 2),
      frame("done", { latency_ms: 5 }, 3),
    ];
    mount();
    const input = (await screen.findByTestId("chat-input")) as HTMLTextAreaElement;
    await waitFor(() => expect(input.disabled).toBe(false));
    fireEvent.change(input, { target: { value: "Hi" } });
    fireEvent.click(screen.getByTestId("chat-send"));

    await screen.findByText("Hello from the model");
    expect(document.querySelector(".v2-chat-error")).toBeNull();
  });
});

describe("V2Chat — a turn refused because one is still running (per-user policy carrier)", () => {
  it("explains policy.session_busy in the error row", async () => {
    streamFrames = [
      frame("meta", { session_id: SID, agent: "hr-notice-byoc", mode: "stream", turn: 1 }, 1),
      frame("error", { code: "policy.session_busy", message: "a turn is already running",
        detail: { agent_id: "a1" } }, 2),
    ];
    mount();
    const input = (await screen.findByTestId("chat-input")) as HTMLTextAreaElement;
    await waitFor(() => expect(input.disabled).toBe(false));
    fireEvent.change(input, { target: { value: "Payout for E-1001" } });
    fireEvent.click(screen.getByTestId("chat-send"));
    await waitFor(() => expect(document.querySelector(".v2-chat-error")).not.toBeNull());
    const row = document.querySelector(".v2-chat-error") as HTMLElement;
    expect(row.textContent).toContain(en.apiErrors["policy.session_busy"]);
  });
});
