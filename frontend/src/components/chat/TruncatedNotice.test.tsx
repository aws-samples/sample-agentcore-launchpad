// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it } from "vitest";

import en from "../../locales/en/common.json";
import zh from "../../locales/zh-CN/common.json";
import { Thread } from "../../v2/pages/chat/Thread";
import { TruncatedNotice } from "./TruncatedNotice";

/**
 * N-SCN-bug4 follow-up (r8-a): the console surface of the runtime's `truncated` marker —
 * an inline notice under the chat bubble, a badge on a transcript turn; nothing otherwise.
 */

beforeAll(async () => {
  // jsdom has no Element.scrollTo; the Thread scrolls to its end on every render
  Object.defineProperty(HTMLElement.prototype, "scrollTo", { value: () => undefined, configurable: true });
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(async () => {
  cleanup();
  await i18next.changeLanguage("en");
});

describe("TruncatedNotice", () => {
  it("renders the inline notice with the agreed en copy", () => {
    render(<TruncatedNotice truncated className="x" />);
    const note = screen.getByTestId("bubble-truncated");
    expect(note.textContent).toBe("Response truncated at the model's token limit");
    expect(note.getAttribute("role")).toBe("note");
    expect(note.className).toBe("x");
  });

  it("renders the zh-CN copy", async () => {
    await i18next.changeLanguage("zh-CN");
    render(<TruncatedNotice truncated />);
    expect(screen.getByTestId("bubble-truncated").textContent).toBe("回复在模型 token 上限处被截断");
  });

  it("renders a badge whose tooltip is the full copy", () => {
    render(<TruncatedNotice truncated variant="badge" />);
    const badge = screen.getByTestId("turn-truncated");
    expect(badge.textContent).toBe("TRUNCATED");
    expect(badge.getAttribute("title")).toBe(en.chatPage.truncated);
  });

  it("renders nothing unless truncated is true", () => {
    const { container, rerender } = render(<TruncatedNotice truncated={false} />);
    expect(container.innerHTML).toBe("");
    rerender(<TruncatedNotice />);
    expect(container.innerHTML).toBe("");
  });
});

describe("V2 Thread agent bubble", () => {
  it("shows the notice only under a truncated answer", () => {
    render(
      <Thread
        messages={[
          { kind: "user", text: "compute" },
          { kind: "agent", text: "partial [response truncated at the model's token limit]", truncated: true },
          { kind: "agent", text: "complete answer" },
        ]}
        userLabel="you"
        agentLabel="hr"
        restoring={false}
      />,
    );
    expect(screen.getAllByTestId("bubble-truncated")).toHaveLength(1);
    const bubbles = screen.getByTestId("thread").querySelectorAll(".v2-chat-msg.agent");
    expect(bubbles[0].querySelector("[data-testid='bubble-truncated']")).not.toBeNull();
    expect(bubbles[1].querySelector("[data-testid='bubble-truncated']")).toBeNull();
  });
});
