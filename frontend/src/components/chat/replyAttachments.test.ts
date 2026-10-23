import i18next from "i18next";
import { createElement } from "react";
import { renderToStaticMarkup } from "react-dom/server";
import { I18nextProvider } from "react-i18next";
import { beforeAll, describe, expect, it } from "vitest";

import type { ChatHistoryMessage, ChatReplyAttachment } from "../../lib/api";
import en from "../../locales/en/common.json";
import zhCN from "../../locales/zh-CN/common.json";
import { type ChatMessage, restoredMessage } from "../../v2/pages/chat/messages";
import { Thread } from "../../v2/pages/chat/Thread";
import { appendReplyAttachment, replyAttachmentOf } from "./replyAttachments";

const stored: ChatReplyAttachment = {
  id: "a1", name: "page.png", media_type: "image/png", size_bytes: 10, sha256: "0".repeat(64), status: "stored",
};
const dropped: ChatReplyAttachment = { name: "big.png", status: "dropped", reason_code: "too_large" };

describe("appendReplyAttachment", () => {
  it("joins the open answer bubble after its text", () => {
    const m: ChatMessage[] = [{ kind: "user", text: "shot" }, { kind: "agent", text: "Here", streaming: true }];
    const next = appendReplyAttachment(m, stored, true);
    expect(next).toHaveLength(2);
    expect(next[1]).toEqual({ kind: "agent", text: "Here", streaming: true, replyAttachments: [stored] });
  });

  it("opens a bubble when none is open (e.g. after a tool call)", () => {
    const m: ChatMessage[] = [{ kind: "tool", text: "screenshot_page", name: "screenshot_page" }];
    const next = appendReplyAttachment(m, stored, false);
    expect(next[1]).toEqual({ kind: "agent", text: "", streaming: true, replyAttachments: [stored] });
  });

  it("reaches past an auth card asked mid-answer", () => {
    const m: ChatMessage[] = [
      { kind: "agent", text: "a", streaming: true },
      { kind: "auth", text: "github", name: "repo" },
    ];
    const next = appendReplyAttachment(appendReplyAttachment(m, stored, true), dropped, true);
    expect(next[0].replyAttachments).toEqual([stored, dropped]);
    expect(next[1]).toBe(m[1]);
  });
});

describe("replyAttachmentOf", () => {
  it("keeps metadata and refuses malformed payloads", () => {
    expect(replyAttachmentOf({ ...stored })).toEqual(stored);
    expect(replyAttachmentOf({ name: "x.png", status: "stored" })).toBeNull();
    expect(replyAttachmentOf({ name: "x.png" })).toBeNull();
    expect(replyAttachmentOf({ name: "big.png", status: "dropped", reason_code: "too_large" })).toEqual({
      name: "big.png", status: "dropped", reason_code: "too_large",
      id: undefined, media_type: undefined, size_bytes: undefined, sha256: undefined,
    });
  });
});

describe("history backfill", () => {
  it("restores an agent answer's reply images and a user's sent files separately", () => {
    const agent: ChatHistoryMessage = { id: 9, role: "agent", text: "Here", name: null, attachments: [stored, dropped], verdict: null };
    expect(restoredMessage(agent, "agent-1")).toEqual({
      kind: "agent", text: "Here", id: 9, verdict: null, replyAttachments: [stored, dropped],
    });
    const plain: ChatHistoryMessage = { id: 10, role: "agent", text: "no images", name: null, attachments: [] };
    expect(restoredMessage(plain, "agent-1").replyAttachments).toBeUndefined();
    const user: ChatHistoryMessage = {
      role: "user", text: "", name: null,
      attachments: [{ name: "n.txt", media_type: "text/plain", size: 3, delivery: "text" }],
    };
    expect(restoredMessage(user, "agent-1")).toEqual({
      kind: "user", text: "", attachments: [{ name: "n.txt", media_type: "text/plain", size: 3, delivery: "text" }],
    });
  });
});

describe("Thread renders reply images", () => {
  const instances: Record<string, typeof i18next> = {};
  beforeAll(async () => {
    for (const lng of ["en", "zh-CN"]) {
      const inst = i18next.createInstance();
      await inst.init({
        lng, resources: { en: { common: en }, "zh-CN": { common: zhCN } },
        defaultNS: "common", interpolation: { escapeValue: false },
      });
      instances[lng] = inst;
    }
  });

  const render = (lng: string, messages: ChatMessage[]) =>
    renderToStaticMarkup(
      createElement(
        I18nextProvider,
        { i18n: instances[lng] },
        createElement(Thread, { messages, userLabel: "me", agentLabel: "shots", agentId: "agent-1", restoring: false }),
      ),
    );

  it("shows a stored image's loading state and a dropped image's reason, in both languages", () => {
    const messages: ChatMessage[] = [{ kind: "agent", text: "Here", replyAttachments: [stored, dropped] }];
    const html = render("en", messages);
    expect(html).toContain('data-testid="reply-attachments"');
    expect(html).toContain("Loading page.png");
    expect(html).toContain("An image from the agent (“big.png”) was not shown: it exceeds the size limit.");
    const zh = render("zh-CN", messages);
    expect(zh).toContain("正在加载 page.png");
    expect(zh).toContain("图片超过大小上限。");
  });

  it("renders an image-only answer without an empty markdown block", () => {
    const html = render("en", [{ kind: "agent", text: "", replyAttachments: [dropped] }]);
    expect(html).not.toContain('class="md"');
    expect(html).toContain("reply-attachment-dropped");
  });
});
