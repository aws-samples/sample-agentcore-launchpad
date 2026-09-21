// Frontend contract regression: every API request is intercepted; no AWS calls.
// Usage: PLAYWRIGHT_MODULE=/path/to/playwright/index.mjs node frontend/scripts/chat_attachments_evidence.mjs [baseUrl]
import assert from "node:assert/strict";
const { chromium } = await import(process.env.PLAYWRIGHT_MODULE ?? "playwright");
const base = process.argv[2] ?? "http://localhost:5173";
const capability = {
  images: true, text: true, pdf: "native", reason_code: null,
  accept: [".png", ".jpg", ".txt", ".md", ".pdf", ".py"],
  max_files: 5, max_file_bytes: 1024, max_total_bytes: 4096,
};
const agent = (id, attachment_capability) => ({
  id, name: id, method: id === "harness" ? "harness" : "zip_runtime",
  spec: {}, status: "active", invoke_capability: { eligible: true },
  attachment_capability,
});
const agents = [
  agent("native", capability),
  agent("harness", { ...capability, images: false, pdf: "text", reason_code: "harness", accept: [".txt", ".md", ".pdf"] }),
  ...["republish", "custom", "model"].map((reason_code) =>
    agent(reason_code, { ...capability, images: false, pdf: "text", reason_code, accept: [".txt", ".pdf"] })),
  agent("legacy", undefined),
];
const imageBytes = Buffer.from("iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a/wAAAABJRU5ErkJggg==", "base64");
const file = (name, buffer = Buffer.from("附件 text"), mimeType = "text/plain") => ({ name, buffer, mimeType });
const json = (body, status = 200) => ({ status, contentType: "application/json", body: JSON.stringify(body) });
const frame = (event, data) => `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;
const browser = await chromium.launch({ headless: true });
const page = await browser.newPage({ viewport: { width: 1440, height: 1050 } });
const errors = [];
page.on("pageerror", (error) => errors.push(error.message));
let mode = "success";
let lastRequest;
let postCount = 0;
let release;
let historyFails = false;
const metadata = [{ name: "saved.pdf", media_type: "application/pdf", size: 120, delivery: "pdf_text" }];
try {
  await page.addInitScript(() => {
    if (!localStorage.getItem("i18nextLng")) localStorage.setItem("i18nextLng", "en");
  });
  await page.route("**/api/**", async (route) => {
    const request = route.request();
    const path = new URL(request.url()).pathname;
    if (path === "/api/auth/status") return route.fulfill(json({ auth_required: false, authenticated: true, role: "admin" }));
    if (path === "/api/workspaces") return route.fulfill(json({ workspaces: [], all_workspaces: true }));
    if (path === "/api/health") return route.fulfill(json({ status: "ok" }));
    if (path === "/api/agents") return route.fulfill(json({ agents }));
    if (path.endsWith("/sessions")) return route.fulfill(json({ sessions: [
      { session_id: "history-1", preview: "Saved conversation", turns: 1, ended_at: null },
    ] }));
    if (path.endsWith("/history")) return route.fulfill(historyFails
      ? json({ message: "history unavailable" }, 503)
      : json({ messages: [
        { role: "user", text: "", name: null, attachments: metadata },
        { role: "agent", text: "History reply", name: null },
      ] }));
    if (path.endsWith("/memory")) return route.fulfill(json({ event_count: 1, records: [] }));
    if (path === "/api/apikeys") return route.fulfill(json({ keys: [] }));
    if (path.startsWith("/api/chat/") && request.method() === "POST") {
      lastRequest = request.postDataJSON();
      postCount += 1;
      if (mode === "hold") await new Promise((resolve) => { release = resolve; });
      if (mode === "http") return route.fulfill(json({
        code: "chat.attachment_pdf_text_unavailable", message: "raw backend error",
      }, 422));
      if (mode === "stale") return route.fulfill({
        contentType: "text/event-stream",
        body: frame("meta", { session_id: "stale-session" }) +
          frame("error", { code: "chat.attachment_new_session_required", message: "raw backend error" }) +
          frame("done", {}),
      });
      const attachments = (lastRequest.attachments ?? []).map((attachment) => ({
        name: attachment.name, media_type: attachment.media_type,
        size: Buffer.from(attachment.data, "base64").length,
        delivery: attachment.media_type === "application/pdf" ? "pdf_text" : attachment.media_type.startsWith("image/") ? "native" : "text",
      }));
      return route.fulfill({
        contentType: "text/event-stream",
        body: frame("meta", { session_id: "sent-session", attachments }) +
          frame("tool", { name: "lookup" }) + frame("delta", { text: "Streaming " }) +
          frame("delta", { text: "reply" }) + (mode === "truncated" ? "" : frame("done", {})),
      });
    }
    return route.fulfill(json({}));
  });
  await page.goto(`${base}/chat`);
  const picker = page.getByTestId("attachment-input");
  const pending = page.getByTestId("pending-attachments");
  const input = page.getByTestId("chat-input");
  const send = page.getByRole("button", { name: "SEND ▸", exact: true });
  const newSession = page.getByRole("button", { name: "NEW SESSION", exact: true });
  const error = page.getByTestId("attachment-error");
  const waitIdle = () => page.waitForFunction(() => !document.querySelector('[data-testid="chat-input"]').disabled);
  const expectError = async (text) => {
    await error.filter({ hasText: text }).waitFor();
    assert.ok((await error.textContent()).includes(text));
  };
  await page.getByTestId("attachment-capability").waitFor();
  assert.equal(await send.isDisabled(), true);
  assert.equal(await send.getAttribute("title"), "Enter a message or attach a file.");
  const sendHintId = await send.getAttribute("aria-describedby");
  assert.equal(await page.locator(`[id="${sendHintId}"]`).textContent(), "Enter a message or attach a file.");

  await picker.setInputFiles(file("binary.exe", Buffer.from("MZ"), "application/octet-stream"));
  await expectError("unsupported format");
  await picker.setInputFiles(file("empty.txt", Buffer.alloc(0)));
  await expectError("empty");
  await picker.setInputFiles(file("large.txt", Buffer.alloc(1025, "x")));
  await expectError("exceeds 1 KiB");
  await picker.setInputFiles(Array.from({ length: 6 }, (_, i) => file(`${i}.txt`)));
  await expectError("up to 5");
  await picker.setInputFiles(Array.from({ length: 5 }, (_, i) => file(`${i}.txt`, Buffer.alloc(900, "x"))));
  await expectError("4 KiB in total");
  assert.equal(await pending.count(), 0);
  assert.equal(postCount, 0);
  console.log("PASS format, empty, count, per-file and total validation");

  // Multiple additions in one JS turn must validate the latest queued state.
  const batchDrops = async (count, size) => page.getByTestId("attachment-composer").evaluate((element, options) => {
    for (let i = 0; i < options.count; i += 1) {
      const transfer = new DataTransfer();
      transfer.items.add(new File(["x".repeat(options.size)], `${i}.txt`, { type: "text/plain" }));
      element.dispatchEvent(new DragEvent("drop", { dataTransfer: transfer, bubbles: true, cancelable: true }));
    }
  }, { count, size });
  await batchDrops(6, 1);
  await expectError("up to 5");
  assert.equal(await pending.locator("li").count(), 5);
  await newSession.click();
  await batchDrops(5, 900);
  await expectError("4 KiB in total");
  assert.equal(await pending.locator("li").count(), 4);
  await newSession.click();
  console.log("PASS batched additions respect cumulative count and total size");

  await picker.setInputFiles(file("photo.png", imageBytes, "image/png"));
  await pending.locator("img").waitFor();
  await page.waitForFunction(() => document.querySelector('[data-testid="pending-attachments"] img')?.naturalWidth === 1);
  const previewUrl = await pending.locator("img").getAttribute("src");
  await page.getByRole("button", { name: "Remove photo.png", exact: true }).click();
  await pending.waitFor({ state: "detached" });
  assert.equal(await page.evaluate(async (url) => {
    try { await fetch(url); return true; } catch { return false; }
  }, previewUrl), false, "removing a preview revokes its object URL");

  await page.getByTestId("attachment-composer").evaluate((element) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File(["dropped text"], "drop.txt", { type: "text/plain" }));
    element.dispatchEvent(new DragEvent("drop", { dataTransfer: transfer, bubbles: true, cancelable: true }));
  });
  await pending.getByText("drop.txt").waitFor();
  await input.evaluate((element, bytes) => {
    const transfer = new DataTransfer();
    transfer.items.add(new File([Uint8Array.from(bytes)], "pasted.png", { type: "image/png" }));
    element.dispatchEvent(new ClipboardEvent("paste", { clipboardData: transfer, bubbles: true, cancelable: true }));
  }, [...imageBytes]);
  await pending.getByText("pasted.png").waitFor();
  await newSession.click();
  await pending.waitFor({ state: "detached" });
  console.log("PASS image preview/revocation, drop, image paste and new-session reset");

  await picker.setInputFiles(file("unicode.txt"));
  mode = "hold";
  await send.click();
  await page.waitForFunction(() => document.querySelector('[data-testid="chat-input"]').disabled);
  assert.equal(await page.getByTestId("agent-select").isDisabled(), true);
  assert.equal(await newSession.isDisabled(), true);
  assert.equal(await send.getAttribute("title"), null);
  assert.equal(await send.getAttribute("aria-describedby"), null);
  while (!release) await new Promise((resolve) => setTimeout(resolve, 10));
  release();
  await waitIdle();
  await pending.waitFor({ state: "detached" });
  assert.equal(lastRequest.prompt, "");
  assert.equal(lastRequest.session_id, null);
  assert.equal(Buffer.from(lastRequest.attachments[0].data, "base64").toString(), "附件 text");
  await page.getByTestId("thread").getByText("Streaming reply", { exact: true }).waitFor();
  assert.equal(await page.getByTestId("message-attachments").locator("img").count(), 0);
  assert.equal((await page.getByTestId("thread").innerHTML()).includes(lastRequest.attachments[0].data), false);
  assert.ok((await page.getByTestId("message-attachments").textContent()).includes("Text"));
  await input.fill("follow up");
  mode = "success";
  await send.click();
  await waitIdle();
  assert.equal(lastRequest.session_id, "sent-session");
  assert.equal(lastRequest.attachments, undefined);
  console.log("PASS attachment-only base64 request, stream/tool UX, metadata-only transcript and text follow-up");

  await newSession.click();
  await picker.setInputFiles(file("review.pdf", Buffer.from("%PDF-1.7 test"), "application/pdf"));
  await input.fill("summarize");
  mode = "http";
  await send.click();
  await waitIdle();
  await expectError("without extractable text");
  assert.equal(await input.inputValue(), "summarize");
  assert.equal(await pending.locator("li").count(), 1);
  mode = "stale";
  await send.click();
  await waitIdle();
  await expectError("NEW SESSION");
  assert.equal(await pending.locator("li").count(), 1);
  assert.equal(await page.getByTestId("thread").locator(".memline").count(), 0);
  await newSession.click();
  await pending.waitFor({ state: "detached" });
  mode = "truncated";
  await picker.setInputFiles(file("retain.txt"));
  await send.click();
  await waitIdle();
  await expectError("stream ended unexpectedly");
  assert.equal(await pending.locator("li").count(), 1);
  assert.equal(await page.locator(".caret").count(), 0);
  console.log("PASS HTTP, stale-session SSE and interrupted-stream failures retain attachments and draft");

  await input.fill("Keep this draft until history loads");
  historyFails = true;
  await page.getByTestId("history-rail").getByRole("button", { name: /Saved conversation/ }).click();
  await waitIdle();
  assert.equal(await pending.locator("li").count(), 1);
  assert.equal(await input.inputValue(), "Keep this draft until history loads");
  historyFails = false;
  await page.getByTestId("history-rail").getByRole("button", { name: /Saved conversation/ }).click();
  await page.getByTestId("thread").getByText("History reply").waitFor();
  await pending.waitFor({ state: "detached" });
  assert.equal(await input.inputValue(), "");
  assert.ok((await page.getByTestId("message-attachments").textContent()).includes("Extracted PDF text"));
  await picker.setInputFiles(file("reset.txt"));
  await page.getByTestId("agent-select").selectOption("harness");
  await pending.waitFor({ state: "detached" });
  assert.ok((await page.getByTestId("attachment-capability-reason").textContent()).includes("This managed Harness"));
  await picker.setInputFiles(file("photo.png", imageBytes, "image/png"));
  await expectError("compatible agent for images");
  await picker.setInputFiles(file("review.pdf", Buffer.from("%PDF-1.7 test"), "application/pdf"));
  assert.equal(await send.isDisabled(), false);
  await page.getByTestId("agent-select").selectOption("legacy");
  await pending.waitFor({ state: "detached" });
  assert.equal(await page.getByTestId("attachment-picker").isDisabled(), true);
  assert.equal(await send.getAttribute("title"), "Enter a message to send.");
  await input.fill("legacy text");
  assert.equal(await send.isDisabled(), false);
  for (const [id, text] of [
    ["republish", "Republish the agent, then choose NEW SESSION"],
    ["custom", "native attachment contract"],
    ["model", "current model"],
  ]) {
    await page.getByTestId("agent-select").selectOption(id);
    assert.ok((await page.getByTestId("attachment-capability-reason").textContent()).includes(text));
  }
  console.log("PASS metadata history restore, agent switching, Harness guidance and old API fixtures");

  await page.evaluate(() => localStorage.setItem("i18nextLng", "zh-CN"));
  await page.reload();
  await page.getByTestId("agent-select").selectOption("republish");
  assert.ok((await page.getByTestId("attachment-capability-reason").textContent()).includes("再选择“新会话”"));
  await page.getByTestId("agent-select").selectOption("native");
  assert.equal(await page.getByTestId("attachment-capability-reason").count(), 0);
  assert.equal(await page.getByRole("button", { name: "发送 ▸", exact: true }).getAttribute("title"), "请输入消息或添加附件。");
  await picker.setInputFiles(file("中文.txt"));
  mode = "stale";
  await page.getByRole("button", { name: "发送 ▸", exact: true }).click();
  await waitIdle();
  await expectError("新会话");
  assert.equal((await error.textContent()).includes("raw backend error"), false);
  await page.setViewportSize({ width: 390, height: 844 });
  assert.equal(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth), true);
  if (process.env.CHAT_ATTACHMENT_SCREENSHOT) {
    await page.screenshot({ path: process.env.CHAT_ATTACHMENT_SCREENSHOT, fullPage: true });
  }
  assert.deepEqual(errors, []);
  console.log("PASS localized backend error, mobile layout and no browser exceptions");

  await page.goto(`${base}/chat?agent=missing-agent`);
  await page.getByTestId("agent-select").waitFor();
  const disabledSend = page.getByRole("button", { name: "发送 ▸", exact: true });
  assert.equal(await disabledSend.isDisabled(), true);
  assert.equal(await disabledSend.getAttribute("title"), "请先选择一个活动智能体。");
  console.log("PASS localized Send disabled reasons and no reason while busy");
} finally {
  release?.();
  await browser.close();
}
