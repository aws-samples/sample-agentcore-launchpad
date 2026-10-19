import assert from "node:assert/strict";
import { describe, test } from "node:test";

import { AppServer } from "../src/appserver.mjs";
import { FAKE_APP_SERVER, waitFor } from "./helpers.mjs";

// An app-server that prints noise, then behaves like the fake.
const NOISY = `process.stdout.write("not json\\n\\n"); await import(${JSON.stringify(FAKE_APP_SERVER)});`;

function server(command, extra = {}) {
  const notifications = [];
  const app = new AppServer({
    command,
    cwd: process.cwd(),
    env: { ...process.env, FAKE_APP_SERVER_LOG: "" },
    onNotification: (method, params) => notifications.push({ method, params }),
    ...extra,
  });
  return { app, notifications };
}

describe("AppServer", () => {
  test("a binary that cannot start is appserver_failed, and the next ensure retries", async () => {
    const { app } = server(["/nonexistent/codex-binary", "app-server"]);
    await assert.rejects(app.ensure(), (e) => e.code === "appserver_failed");
    assert.equal(app.running, false);
    assert.equal(app.ready, null, "a failed start is not cached");
  });

  test("handshakes once, ignores non-JSON lines and answers requests", async () => {
    const { app, notifications } = server([process.execPath, "--input-type=module", "-e", NOISY]);
    try {
      await Promise.all([app.ensure(), app.ensure()]);
      const thread = await app.request("thread/start", { cwd: process.cwd() });
      assert.match(thread.thread.id, /^thr_/);
      await waitFor(() => notifications.length, { what: "thread/started" });
      assert.deepEqual(notifications.map((n) => n.method), ["thread/started"]);
      await assert.rejects(app.request("no/such", {}), (e) => e.code === "appserver_failed" && /unknown method/.test(e.message));
    } finally {
      app.stop();
    }
  });

  test("an exit rejects pending requests and reports it", async () => {
    let exited = null;
    const { app } = server([process.execPath, FAKE_APP_SERVER], { onExit: (code) => { exited = code; } });
    await app.ensure();
    await app.request("thread/start", {});
    const pending = app.request("turn/start", { threadId: "thr_1", input: [{ type: "text", text: "crash", text_elements: [] }] });
    await pending; // answered before the crash
    await new Promise((resolve) => app.proc.once("exit", resolve));
    assert.equal(exited, 3);
    assert.equal(app.running, false);
    await assert.rejects(app.request("thread/start", {}), (e) => e.code === "appserver_failed");
  });
});
