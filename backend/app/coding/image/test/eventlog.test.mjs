import assert from "node:assert/strict";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { after, before, describe, test } from "node:test";

import { EventLog } from "../src/eventlog.mjs";

let dir;
before(async () => { dir = await fsp.mkdtemp(path.join(os.tmpdir(), "coding-eventlog-")); });
after(async () => { await fsp.rm(dir, { recursive: true, force: true }); });

const file = (name) => path.join(dir, name, "events.jsonl");

// One turn's worth of notifications: item A completes, item B never does.
function seedTurn(log) {
  log.append("turn/started", { turn: { id: "t1" } });
  log.append("item/agentMessage/delta", { turnId: "t1", itemId: "A", delta: "He" });
  log.append("item/agentMessage/delta", { turnId: "t1", itemId: "A", delta: "llo" });
  log.append("item/commandExecution/outputDelta", { turnId: "t1", itemId: "B", delta: "out\n" });
  log.append("turn/diff/updated", { turnId: "t1", diff: "first" });
  log.append("account/rateLimits/updated", {});
  log.append("item/completed", { turnId: "t1", item: { id: "A", type: "agentMessage", text: "Hello" } });
  log.append("turn/diff/updated", { turnId: "t1", diff: "final" });
  log.append("turn/completed", { turn: { id: "t1", status: "completed" } });
}

describe("EventLog", () => {
  test("assigns increasing seqs and persists them across a reload", () => {
    const log = new EventLog(file("persist")).load();
    const a = log.append("x/one", { n: 1 });
    const b = log.append("x/two", { n: 2 });
    assert.equal(a.seq, 1);
    assert.equal(b.seq, 2);
    assert.ok(typeof a.ts === "number");
    log.flushSync();
    const again = new EventLog(file("persist")).load();
    assert.equal(again.seq, 2);
    assert.deepEqual(again.records.map((r) => r.method), ["x/one", "x/two"]);
    assert.equal(again.append("x/three").seq, 3, "the counter continues after a reboot");
  });

  test("batches appends to disk and flushes on its own", async () => {
    const log = new EventLog(file("batched")).load();
    log.append("x/one");
    assert.equal(fs.existsSync(log.file), false, "nothing is written synchronously");
    await new Promise((r) => setTimeout(r, 250));
    assert.equal(fs.readFileSync(log.file, "utf8").trim().split("\n").length, 1);
  });

  test("skips a torn last line left by a crash", () => {
    const f = file("torn");
    fs.mkdirSync(path.dirname(f), { recursive: true });
    fs.writeFileSync(f, `${JSON.stringify({ seq: 1, ts: 1, method: "a", params: {} })}\n{"seq": 2, "me`);
    const log = new EventLog(f).load();
    assert.equal(log.seq, 1);
    assert.equal(log.append("b").seq, 2);
  });

  test("read pages by seq with a forward-only cursor", () => {
    const log = new EventLog(file("paging")).load();
    for (let i = 0; i < 10; i += 1) log.append("x/n", { i });
    const first = log.read({ after: 0, limit: 4 });
    assert.deepEqual(first.events.map((e) => e.seq), [1, 2, 3, 4]);
    assert.equal(first.next_after, 4);
    const rest = log.read({ after: first.next_after, limit: 100 });
    assert.deepEqual(rest.events.map((e) => e.seq), [5, 6, 7, 8, 9, 10]);
    assert.equal(rest.next_after, 10);
    assert.deepEqual(log.read({ after: 10 }).events, []);
    assert.equal(log.read({ after: 99 }).next_after, 99, "a cursor never moves backwards");
  });

  test("compact reads drop completed items' deltas, superseded diffs and rate limits", () => {
    const log = new EventLog(file("compact")).load();
    seedTurn(log);
    const full = log.read({ after: 0, limit: 100 });
    assert.equal(full.events.length, 9);
    const compact = log.read({ after: 0, limit: 100, compact: true });
    assert.deepEqual(compact.events.map((e) => e.method), [
      "turn/started",
      "item/commandExecution/outputDelta", // item B has no item/completed: kept
      "item/completed",
      "turn/diff/updated",
      "turn/completed",
    ]);
    assert.equal(compact.events.find((e) => e.method === "turn/diff/updated").params.diff, "final");
    assert.equal(compact.next_after, 9, "skipped records still advance the cursor");
  });

  test("compactFile rewrites the file to the compact view and keeps seqs", () => {
    const log = new EventLog(file("compact-file")).load();
    seedTurn(log);
    const before = log.read({ after: 0, limit: 100, compact: true }).events;
    const { dropped, kept } = log.compactFile();
    assert.equal(dropped, 4);
    assert.equal(kept, 5);
    const reloaded = new EventLog(log.file).load();
    assert.deepEqual(reloaded.records, before);
    assert.equal(reloaded.seq, 9);
    assert.equal(reloaded.append("next").seq, 10, "compaction never reuses a seq");
    assert.deepEqual(log.compactFile(), { dropped: 0, kept: 5 }, "idempotent");
  });

  test("an append after compaction is not lost", async () => {
    const log = new EventLog(file("after-compact")).load();
    seedTurn(log);
    log.compactFile();
    log.append("late/event");
    log.flushSync();
    const reloaded = new EventLog(log.file).load();
    assert.equal(reloaded.records.at(-1).method, "late/event");
  });

  test("wait resolves on the next append and times out otherwise", async () => {
    const log = new EventLog(file("wait")).load();
    log.append("x/one");
    assert.equal(await log.wait(0, 1000), true, "already past the cursor");
    const t0 = Date.now();
    assert.equal(await log.wait(1, 60), false);
    assert.ok(Date.now() - t0 >= 50);
    const pending = log.wait(1, 5000);
    setTimeout(() => log.append("x/two"), 20);
    const woke = Date.now();
    assert.equal(await pending, true);
    assert.ok(Date.now() - woke < 1000, "woken by the append, not the timeout");
    assert.equal(log.waiters.size, 0);
  });

  test("bump jumps the counter so lost seqs are never reissued", () => {
    const log = new EventLog(file("bump")).load();
    log.append("a");
    log.bump(1000);
    assert.equal(log.append("b").seq, 1002);
  });
});
