// Seq-numbered, append-only event log on session storage (`.launchpad/events.jsonl`).
//
// Every app-server notification (and every launchpad/* event) becomes one record
// {seq, ts, method, params}. Readers fetch by seq, so a reload, a reconnect or a
// backend restart never loses or reorders anything. Records are kept in memory
// (the file is the durable copy) and appended to disk in small batches; a
// `compactFile()` at turn end drops the deltas of completed items, which bounds
// the log to the item count rather than the token count. All disk writes are
// synchronous and batched (one append per ~100 ms), so a flush can never interleave
// with the compaction rename.
import fs from "node:fs";
import path from "node:path";

// Streaming notifications that carry a fragment of one item (params.itemId). Once
// the item has an `item/completed`, the completed item carries the whole text,
// output or patch, so the fragments are redundant for a history replay.
export const DELTA_METHODS = new Set([
  "item/agentMessage/delta",
  "item/reasoning/summaryTextDelta",
  "item/reasoning/summaryPartAdded",
  "item/reasoning/textDelta",
  "item/commandExecution/outputDelta",
  "item/fileChange/outputDelta",
  "item/plan/delta",
]);
// Notifications a history replay never needs.
export const NOISY_METHODS = new Set(["account/rateLimits/updated"]);
export const TURN_DIFF = "turn/diff/updated";

const FLUSH_DELAY_MS = 100;

export class EventLog {
  constructor(file) {
    this.file = file;
    this.records = [];
    this.seq = 0;
    this.pending = [];
    this.flushTimer = null;
    this.waiters = new Set();
    this.completedItems = new Set();
    this.lastDiffSeq = new Map(); // turnId -> seq of the newest turn/diff/updated
  }

  // Load an existing log (a restarted microVM, a restored snapshot). A torn last
  // line from a crash mid-append is skipped.
  load() {
    this.records = [];
    this.completedItems.clear();
    this.lastDiffSeq.clear();
    this.seq = 0;
    if (fs.existsSync(this.file)) {
      for (const line of fs.readFileSync(this.file, "utf8").split("\n")) {
        if (!line.trim()) continue;
        let rec;
        try { rec = JSON.parse(line); } catch { continue; }
        if (typeof rec?.seq !== "number") continue;
        this._index(rec);
        this.records.push(rec);
        if (rec.seq > this.seq) this.seq = rec.seq;
      }
    }
    return this;
  }

  _index(rec) {
    if (rec.method === "item/completed" && rec.params?.item?.id) {
      this.completedItems.add(rec.params.item.id);
    } else if (rec.method === TURN_DIFF && rec.params?.turnId) {
      this.lastDiffSeq.set(rec.params.turnId, rec.seq);
    }
  }

  // Jump the counter forward (used after a snapshot restore: the records issued
  // after that snapshot are lost, and their seqs must never be reused).
  bump(gap) {
    this.seq += gap;
  }

  append(method, params = {}) {
    const rec = { seq: ++this.seq, ts: Date.now(), method, params: params ?? {} };
    this._index(rec);
    this.records.push(rec);
    this.pending.push(JSON.stringify(rec) + "\n");
    if (!this.flushTimer) {
      this.flushTimer = setTimeout(() => {
        this.flushTimer = null;
        try { this.flushSync(); } catch (e) { console.error("event log flush failed", e); }
      }, FLUSH_DELAY_MS);
      this.flushTimer.unref?.();
    }
    const waiters = [...this.waiters];
    this.waiters.clear();
    for (const wake of waiters) wake();
    return rec;
  }

  flushSync() {
    if (this.flushTimer) {
      clearTimeout(this.flushTimer);
      this.flushTimer = null;
    }
    if (!this.pending.length) return;
    const chunk = this.pending.join("");
    this.pending = [];
    fs.mkdirSync(path.dirname(this.file), { recursive: true });
    fs.appendFileSync(this.file, chunk);
  }

  // True when a compact read drops `rec`.
  _redundant(rec) {
    if (NOISY_METHODS.has(rec.method)) return true;
    if (DELTA_METHODS.has(rec.method)) return this.completedItems.has(rec.params?.itemId);
    if (rec.method === TURN_DIFF) {
      const newest = this.lastDiffSeq.get(rec.params?.turnId);
      return newest !== undefined && newest !== rec.seq;
    }
    return false;
  }

  // Records with seq > after, at most `limit`. `next_after` is the cursor to send
  // next time: the last seq this read looked at, so records a compact read skipped
  // are not scanned again (and never moves a cursor backwards).
  read({ after = 0, limit = 500, compact = false } = {}) {
    const out = [];
    let cursor = after;
    let i = this._firstIndexAfter(after);
    for (; i < this.records.length && out.length < limit; i += 1) {
      const rec = this.records[i];
      cursor = rec.seq;
      if (compact && this._redundant(rec)) continue;
      out.push(rec);
    }
    if (i >= this.records.length) cursor = Math.max(cursor, this.seq);
    return { events: out, next_after: cursor };
  }

  _firstIndexAfter(after) {
    // records are seq-ordered: binary search for the first seq > after
    let lo = 0;
    let hi = this.records.length;
    while (lo < hi) {
      const mid = (lo + hi) >> 1;
      if (this.records[mid].seq <= after) lo = mid + 1;
      else hi = mid;
    }
    return lo;
  }

  // Resolve when a record with seq > after exists, or after `ms`.
  wait(after, ms) {
    const last = this.records.at(-1);
    if (last && last.seq > after) return Promise.resolve(true);
    if (ms <= 0) return Promise.resolve(false);
    return new Promise((resolve) => {
      const wake = () => { clearTimeout(timer); resolve(true); };
      const timer = setTimeout(() => { this.waiters.delete(wake); resolve(false); }, ms);
      this.waiters.add(wake);
    });
  }

  // Rewrite the file without the records a compact read would drop. Seqs are kept,
  // so every reader's cursor stays valid. Synchronous on purpose: no append can
  // interleave between the rewrite and the rename (a batched flush landing on the
  // old file would be lost with it). Runs once per turn.
  compactFile() {
    this.flushSync();
    const kept = this.records.filter((rec) => !this._redundant(rec));
    const dropped = this.records.length - kept.length;
    if (!dropped) return { dropped: 0, kept: kept.length };
    const tmp = `${this.file}.compact-${process.pid}`;
    fs.writeFileSync(tmp, kept.map((r) => JSON.stringify(r) + "\n").join(""));
    fs.renameSync(tmp, this.file);
    this.records = kept;
    return { dropped, kept: kept.length };
  }
}
