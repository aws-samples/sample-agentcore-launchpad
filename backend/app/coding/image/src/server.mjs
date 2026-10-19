// Launchpad coding runtime: the AgentCore HTTP contract (GET /ping, POST /invocations
// on :8080) in front of one `codex app-server` per microVM.
//
// One runtime session = one member's project. Every invocation is a short JSON
// action ({action, ...} in, {ok, action, boot_id, result|error} out — always HTTP 200,
// because AgentCore turns a container 5xx into an opaque RuntimeClientError). A turn
// runs asynchronously in this process: `start_turn` returns at once, the turn keeps
// /ping at HealthyBusy (so the 15-minute idle reaper leaves the microVM alone), and
// every app-server notification lands in a seq-numbered event log the backend reads
// with `events` long-polls. Session storage is mounted at the first invocation, not
// at process start, so nothing touches the workspace until then.
import crypto from "node:crypto";
import http from "node:http";
import { pathToFileURL } from "node:url";

import { AppServer } from "./appserver.mjs";
import { EventLog } from "./eventlog.mjs";
import { CodingError, ERROR_CODES, log } from "./util.mjs";
import { Workspace, layout } from "./workspace.mjs";

export const DEFAULT_EFFORTS = ["low", "medium", "high", "xhigh"];
export const MAX_PROMPT_BYTES = 32 * 1024;
// a 2 MB file save is at most ~2x that once JSON-escaped (UTF-8 body), with headroom
export const MAX_BODY_BYTES = 16 * 1024 * 1024;
export const MAX_WAIT_MS = 25000;
export const MAX_EVENTS_LIMIT = 2000;
// After a snapshot restore the records issued after that snapshot are gone; the
// counter jumps so their seqs are never reused for different records.
export const RESTORE_SEQ_GAP = 1_000_000;
const USAGE_CACHE_MS = 60000;
const USAGE_KEYS = ["totalTokens", "inputTokens", "cachedInputTokens", "cacheWriteInputTokens",
  "outputTokens", "reasoningOutputTokens"];

const csv = (value) => String(value || "").split(",").map((s) => s.trim()).filter(Boolean);

export function configFromEnv(env = process.env) {
  return {
    workspace: env.CODING_WORKSPACE || "/mnt/workspace",
    stateDir: env.CODING_STATE_DIR || "/var/lib/coding-state",
    depsDir: env.CODING_DEPS_DIR || "/var/lib/coding-deps",
    codexTemplate: env.CODEX_TEMPLATE || "/opt/codex-home",
    projectTemplates: env.CODING_TEMPLATES || "/opt/coding-templates",
    models: csv(env.CODING_MODELS),
    efforts: csv(env.CODING_EFFORTS).length ? csv(env.CODING_EFFORTS) : DEFAULT_EFFORTS,
    region: env.AWS_REGION || env.AWS_DEFAULT_REGION || "",
    version: env.CODING_RUNTIME_VERSION || "dev",
    appServerCommand: env.CODING_APP_SERVER_CMD ? JSON.parse(env.CODING_APP_SERVER_CMD) : ["codex", "app-server"],
  };
}

function zeroUsage() {
  return Object.fromEntries(USAGE_KEYS.map((k) => [k, 0]));
}

function usageMinus(a, b) {
  return Object.fromEntries(USAGE_KEYS.map((k) => [k, Math.max(0, Number(a?.[k] || 0) - Number(b?.[k] || 0))]));
}

function usageAdd(into, delta) {
  for (const k of USAGE_KEYS) into[k] = Number(into[k] || 0) + Number(delta[k] || 0);
  return into;
}

const intArg = (value, fallback, { min = 0, max = Number.MAX_SAFE_INTEGER } = {}) => {
  if (value === undefined || value === null) return fallback;
  const n = Number(value);
  if (!Number.isFinite(n)) throw new CodingError("bad_request", `expected a number, got ${JSON.stringify(value)}`);
  return Math.min(Math.max(Math.trunc(n), min), max);
};

export class CodingRuntime {
  constructor(config, { spawnFn } = {}) {
    this.cfg = config;
    this.paths = layout(config);
    this.ws = new Workspace(this.paths);
    this.bootId = crypto.randomUUID();
    this.startedAt = Date.now();
    this.busy = 0;
    this.statusChangedAt = Math.floor(Date.now() / 1000);
    this.ready = false;
    this.readyPromise = null;
    this.restored = false;
    this.eventLog = null;
    this.activeTurn = null;
    this.starting = false;
    this.loadedThreads = new Set();
    this.turnUsage = new Map(); // turnId -> accumulated TokenUsageBreakdown
    this.threadTotals = new Map(); // threadId -> last seen tokenUsage.total
    this.earlyCompletions = new Map(); // turnId -> turn, completed before turn/start answered
    this.usageCache = { at: 0, bytes: null, refreshing: null };
    const extraArgs = ["-c", `sqlite_home=${JSON.stringify(this.paths.stateDir)}`];
    if (config.region) {
      extraArgs.push("-c", `model_providers.amazon-bedrock-runtime.aws.region=${JSON.stringify(config.region)}`);
    }
    this.app = new AppServer({
      command: config.appServerCommand,
      extraArgs,
      cwd: this.paths.project,
      env: {
        ...process.env,
        CODEX_HOME: this.paths.codexHome,
        UV_PROJECT_ENVIRONMENT: process.env.UV_PROJECT_ENVIRONMENT || `${this.paths.depsDir}/venv`,
        UV_CACHE_DIR: process.env.UV_CACHE_DIR || `${this.paths.depsDir}/uv-cache`,
        npm_config_cache: process.env.npm_config_cache || `${this.paths.depsDir}/npm-cache`,
        PIP_CACHE_DIR: process.env.PIP_CACHE_DIR || `${this.paths.depsDir}/pip-cache`,
      },
      spawnFn,
      clientInfo: { name: "launchpad-coding", title: "Launchpad Coding", version: config.version },
      onNotification: (method, params) => this.onNotification(method, params),
      onServerRequest: (method) => this.eventLog?.append("launchpad/serverRequestDeclined", { method }),
      onExit: (code, signal) => this.onAppServerExit(code, signal),
    });
  }

  // ------------------------------------------------------------- busy / ping
  setBusy(delta) {
    const before = this.busy > 0;
    this.busy = Math.max(0, this.busy + delta);
    if (before !== this.busy > 0) this.statusChangedAt = Math.floor(Date.now() / 1000);
  }

  ping() {
    return { status: this.busy > 0 ? "HealthyBusy" : "Healthy", time_of_last_update: this.statusChangedAt };
  }

  // ------------------------------------------------------------- readiness
  // First invocation of a microVM: mount-time init. A workspace without the
  // `initialized` marker is restored from the snapshot the backend passed (version
  // update, 14-day expiry) or initialized fresh. Single-flight: concurrent first
  // invocations share one preparation; a failed one is retried by the next call.
  ensureReady(restoreUrl) {
    if (this.ready) return Promise.resolve();
    if (!this.readyPromise) {
      this.readyPromise = this._prepare(restoreUrl).then(
        () => { this.ready = true; },
        (err) => { this.readyPromise = null; throw err; },
      );
    }
    return this.readyPromise;
  }

  async _prepare(restoreUrl) {
    const ws = this.ws;
    await ws.prepareDirs();
    const initialized = ws.isInitialized();
    let restoredBytes = null;
    if (!initialized && typeof restoreUrl === "string" && restoreUrl) {
      this.setBusy(+1);
      try {
        ({ bytes: restoredBytes } = await ws.restore(restoreUrl));
      } finally {
        this.setBusy(-1);
      }
    }
    this.eventLog = new EventLog(this.paths.eventsFile).load();
    await ws.linkCodexHome();
    if (!initialized && restoredBytes === null) await ws.initFresh();
    else await ws.ensureDepLinks();
    if (!initialized) this.eventLog.advanceTo(Date.now() * 1000);
    if (restoredBytes !== null) {
      this.eventLog.bump(RESTORE_SEQ_GAP);
      this.eventLog.append("launchpad/restored", { bootId: this.bootId, bytes: restoredBytes });
      this.restored = true;
    }
    const state = ws.readState();
    if (state.activeTurn) {
      // the microVM that ran this turn is gone
      this.eventLog.append("launchpad/turnAbandoned", {
        turnId: state.activeTurn.turnId,
        threadId: state.activeTurn.threadId,
        reason: "runtime_restarted",
        usage: state.activeTurn.usage || null,
      });
      state.lastTurn = { ...state.activeTurn, status: "abandoned", completedAt: Date.now() };
      state.activeTurn = null;
      ws.writeState(state);
    }
    ws.markInitialized();
    this.eventLog.flushSync();
    log(`workspace ready boot=${this.bootId} restored=${restoredBytes !== null} fresh=${!initialized && restoredBytes === null}`);
  }

  // ------------------------------------------------------------- app-server events
  onNotification(method, params) {
    if (!this.eventLog) return;
    this.eventLog.append(method, params);
    if (method === "thread/tokenUsage/updated") this._trackUsage(params);
    else if (method === "turn/completed") this._onTurnCompleted(params);
  }

  // Turn usage = the sum of `total` increments seen during the turn. The first
  // notification of a thread in this process has no previous total: `total - last`
  // is the baseline (one model call per notification), which is right whether a
  // resumed thread's total restarts at 0 or carries the history.
  _trackUsage(params) {
    const total = params?.tokenUsage?.total;
    if (!total) return;
    const threadId = params.threadId;
    const previous = this.threadTotals.get(threadId) ?? usageMinus(total, params.tokenUsage.last);
    this.threadTotals.set(threadId, total);
    const turnId = params.turnId;
    if (!turnId) return;
    const acc = usageAdd(this.turnUsage.get(turnId) || zeroUsage(), usageMinus(total, previous));
    this.turnUsage.set(turnId, acc);
    if (this.activeTurn?.turnId === turnId) {
      this.activeTurn.usage = acc;
      this._persistActiveTurn();
    }
  }

  _persistActiveTurn() {
    const state = this.ws.readState();
    state.activeTurn = this.activeTurn ? { ...this.activeTurn } : null;
    this.ws.writeState(state);
  }

  _onTurnCompleted(params) {
    const turn = params?.turn;
    if (!turn?.id) return;
    if (this.activeTurn?.turnId === turn.id) this._finishTurn(turn.status || "completed", turn.error || null);
    else if (this.starting) this.earlyCompletions.set(turn.id, turn);
  }

  // Close the active turn: usage event, state, busy, log compaction. Synchronous up
  // to the busy flag, so `launchpad/turnUsage` lands right after `turn/completed`.
  _finishTurn(status, error, { abandonReason = null } = {}) {
    const turn = this.activeTurn;
    if (!turn) return;
    this.activeTurn = null;
    const usage = this.turnUsage.get(turn.turnId) || turn.usage || zeroUsage();
    this.turnUsage.delete(turn.turnId);
    if (abandonReason) {
      this.eventLog.append("launchpad/turnAbandoned", { turnId: turn.turnId, threadId: turn.threadId, reason: abandonReason, usage });
    } else {
      this.eventLog.append("launchpad/turnUsage", {
        turnId: turn.turnId, threadId: turn.threadId, model: turn.model, effort: turn.effort, status, usage,
      });
    }
    const state = this.ws.readState();
    state.activeTurn = null;
    state.lastTurn = {
      turnId: turn.turnId,
      threadId: turn.threadId,
      baseTree: turn.baseTree,
      model: turn.model,
      effort: turn.effort,
      startedAt: turn.startedAt,
      completedAt: Date.now(),
      status: abandonReason ? "abandoned" : status,
      error: error ? (error.message || String(error)) : null,
      usage,
    };
    this.ws.writeState(state);
    this.setBusy(-1);
    try {
      this.eventLog.compactFile();
    } catch (e) {
      log("event log compaction failed", e.message);
    }
  }

  onAppServerExit(code, signal) {
    this.loadedThreads.clear();
    this.threadTotals.clear();
    if (!this.eventLog) return;
    this.eventLog.append("launchpad/appServerExited", { code, signal });
    if (this.activeTurn) this._finishTurn("failed", null, { abandonReason: "app_server_exited" });
  }

  async _ensureThread(model) {
    await this.app.ensure();
    const state = this.ws.readState();
    const common = { cwd: this.paths.project, approvalPolicy: "never", sandbox: "danger-full-access", model };
    if (state.threadId) {
      if (this.loadedThreads.has(state.threadId)) return state.threadId;
      try {
        const r = await this.app.request("thread/resume", { threadId: state.threadId, ...common, excludeTurns: true });
        const id = r?.thread?.id || state.threadId;
        this.loadedThreads.add(id);
        if (id !== state.threadId) this.ws.writeState({ ...this.ws.readState(), threadId: id });
        return id;
      } catch (e) {
        if (!this.app.running) throw e;
        // the rollout is gone (or unreadable): continue the project in a new thread
        this.eventLog.append("launchpad/threadResumeFailed", { threadId: state.threadId, message: e.message });
      }
    }
    const r = await this.app.request("thread/start", common);
    const id = r?.thread?.id;
    if (!id) throw new CodingError("appserver_failed", "thread/start returned no thread id");
    this.loadedThreads.add(id);
    this.ws.writeState({ ...this.ws.readState(), threadId: id });
    return id;
  }

  _turnBase() {
    if (this.activeTurn) return this.activeTurn.baseTree || null;
    return this.ws.readState().lastTurn?.baseTree || null;
  }

  _turnInfo() {
    const t = this.activeTurn;
    return t ? { turn_id: t.turnId, thread_id: t.threadId, model: t.model, effort: t.effort, started_at: t.startedAt } : null;
  }

  _refuseDuringTurn() {
    if (this.activeTurn || this.starting) throw new CodingError("turn_running", "a turn is running in this project");
  }

  _usageBytes() {
    const cache = this.usageCache;
    if (Date.now() - cache.at > USAGE_CACHE_MS && !cache.refreshing) {
      cache.refreshing = this.ws.usageBytes()
        .then((bytes) => { cache.bytes = bytes; cache.at = Date.now(); })
        .catch(() => {})
        .finally(() => { cache.refreshing = null; });
    }
    return cache.bytes;
  }

  // ------------------------------------------------------------- actions
  async status() {
    const state = this.ws.readState();
    const last = state.lastTurn;
    return {
      boot_id: this.bootId,
      version: this.cfg.version,
      busy: this.busy > 0,
      active_turn: this._turnInfo(),
      thread_id: state.threadId || null,
      seq: this.eventLog.seq,
      usage_bytes: this._usageBytes(),
      restored: this.restored,
      last_turn: last ? {
        turn_id: last.turnId, status: last.status, usage: last.usage || null, error: last.error || null,
        model: last.model || null, effort: last.effort || null,
        started_at: last.startedAt || null, completed_at: last.completedAt || null,
      } : null,
      uptime_ms: Date.now() - this.startedAt,
    };
  }

  async startTurn({ prompt, model, effort }) {
    if (typeof prompt !== "string" || !prompt.trim()) throw new CodingError("bad_request", "prompt is required");
    if (Buffer.byteLength(prompt, "utf8") > MAX_PROMPT_BYTES) throw new CodingError("bad_request", "prompt is larger than 32 KB");
    if (typeof model !== "string" || !model) throw new CodingError("bad_request", "model is required");
    if (this.cfg.models.length && !this.cfg.models.includes(model)) {
      throw new CodingError("bad_request", `model ${model} is not enabled for the coding runtime`);
    }
    const chosenEffort = effort || "medium";
    if (!this.cfg.efforts.includes(chosenEffort)) {
      throw new CodingError("bad_request", `effort must be one of ${this.cfg.efforts.join(", ")}`);
    }
    this._refuseDuringTurn();
    this.starting = true;
    try {
      // the turn baseline is best-effort: a broken repo must not block the agent
      const baseTree = await this.ws.writeTree().catch((e) => {
        log("turn baseline failed", e.message);
        return null;
      });
      const threadId = await this._ensureThread(model);
      const seqBefore = this.eventLog.seq;
      const r = await this.app.request("turn/start", {
        threadId,
        input: [{ type: "text", text: prompt, text_elements: [] }],
        model,
        effort: chosenEffort,
        summary: "auto",
      });
      const turnId = r?.turn?.id;
      if (!turnId) throw new CodingError("appserver_failed", "turn/start returned no turn id");
      this.activeTurn = {
        turnId, threadId, model, effort: chosenEffort, baseTree, startedAt: Date.now(),
        usage: this.turnUsage.get(turnId) || zeroUsage(),
      };
      this.setBusy(+1);
      this._persistActiveTurn();
      const early = this.earlyCompletions.get(turnId);
      this.earlyCompletions.clear();
      if (early) this._finishTurn(early.status || "completed", early.error || null);
      return { thread_id: threadId, turn_id: turnId, seq: seqBefore };
    } finally {
      this.starting = false;
      this.earlyCompletions.clear();
    }
  }

  async interrupt() {
    const turn = this.activeTurn;
    if (!turn) return { interrupted: false };
    await this.app.request("turn/interrupt", { threadId: turn.threadId, turnId: turn.turnId });
    return { interrupted: true, turn_id: turn.turnId };
  }

  async readEvents({ after, wait_ms, limit, compact }) {
    const from = intArg(after, 0);
    const waitMs = intArg(wait_ms, 0, { max: MAX_WAIT_MS });
    const max = intArg(limit, 500, { min: 1, max: MAX_EVENTS_LIMIT });
    const asCompact = Boolean(compact);
    let page = this.eventLog.read({ after: from, limit: max, compact: asCompact });
    if (!page.events.length && waitMs > 0) {
      await this.eventLog.wait(page.next_after, waitMs);
      page = this.eventLog.read({ after: page.next_after, limit: max, compact: asCompact });
    }
    return {
      events: page.events,
      next_after: page.next_after,
      seq: this.eventLog.seq,
      cursor_ahead: from > this.eventLog.seq,
      active_turn: this._turnInfo(),
      boot_id: this.bootId,
    };
  }

  async readFile({ path, rev }) {
    if (rev !== undefined && rev !== null && !["worktree", "head", "turn"].includes(rev)) {
      throw new CodingError("bad_request", "rev must be 'head' or 'turn'");
    }
    if (rev === "turn") {
      const base = this._turnBase();
      if (!base) throw new CodingError("no_turn", "no turn has run in this project yet");
      return this.ws.readFile({ path, rev }, base);
    }
    return this.ws.readFile({ path, rev });
  }

  async writeFile(payload) {
    this._refuseDuringTurn();
    return this.ws.writeFile(payload);
  }

  async changes({ scope = "worktree" }) {
    return this.ws.changes(scope, this._turnBase());
  }

  async diff({ scope = "worktree" }) {
    return this.ws.diff(scope, this._turnBase());
  }

  async snapshot({ put_url }) {
    if (typeof put_url !== "string" || !put_url) throw new CodingError("bad_request", "put_url is required");
    this.eventLog.flushSync();
    this.setBusy(+1);
    try {
      return await this.ws.snapshot(put_url);
    } finally {
      this.setBusy(-1);
    }
  }

  async exportProject({ put_url }) {
    if (typeof put_url !== "string" || !put_url) throw new CodingError("bad_request", "put_url is required");
    this.setBusy(+1);
    try {
      return await this.ws.export(put_url);
    } finally {
      this.setBusy(-1);
    }
  }

  // ------------------------------------------------------------- dispatch
  async handle(payload) {
    const t0 = Date.now();
    const action = payload && typeof payload === "object" ? payload.action : undefined;
    try {
      const handler = typeof action === "string" && Object.hasOwn(ACTIONS, action) ? ACTIONS[action] : null;
      if (!handler) throw new CodingError("bad_request", `unknown action ${JSON.stringify(action)}`);
      await this.ensureReady(payload.restore_url);
      const result = await handler(this, payload);
      if (action !== "events") log(`action=${action} ok ms=${Date.now() - t0}`);
      return { ok: true, action, boot_id: this.bootId, result };
    } catch (err) {
      const known = err instanceof CodingError && ERROR_CODES.has(err.code);
      const error = known
        ? { code: err.code, message: err.message, ...(err.extra || {}) }
        : { code: "internal", message: String(err?.message || err) };
      log(`action=${action} error=${error.code} ms=${Date.now() - t0} ${error.message.slice(0, 300)}`);
      if (!known && err?.stack) log(err.stack);
      return { ok: false, action, boot_id: this.bootId, error };
    }
  }

  shutdown() {
    try { this.eventLog?.flushSync(); } catch { /* best effort */ }
    this.app.stop();
  }
}

// The action protocol (parent design §2.2 + §6).
export const ACTIONS = {
  status: (rt) => rt.status(),
  start_turn: (rt, p) => rt.startTurn(p),
  interrupt: (rt) => rt.interrupt(),
  events: (rt, p) => rt.readEvents(p),
  tree: (rt, p) => rt.ws.tree({ path: p.path, depth: p.depth }),
  read_file: (rt, p) => rt.readFile(p),
  write_file: (rt, p) => rt.writeFile(p),
  changes: (rt, p) => rt.changes(p),
  diff: (rt, p) => rt.diff(p),
  snapshot: (rt, p) => rt.snapshot(p),
  export: (rt, p) => rt.exportProject(p),
};

function readBody(req, limit) {
  return new Promise((resolve, reject) => {
    const chunks = [];
    let size = 0;
    req.on("data", (chunk) => {
      size += chunk.length;
      if (size > limit) {
        reject(new CodingError("bad_request", "request body is larger than 16 MB"));
        req.destroy();
        return;
      }
      chunks.push(chunk);
    });
    req.on("end", () => resolve(Buffer.concat(chunks).toString("utf8")));
    req.on("error", reject);
  });
}

function sendJson(res, body) {
  const data = JSON.stringify(body);
  res.writeHead(200, { "content-type": "application/json", "content-length": Buffer.byteLength(data) });
  res.end(data);
}

export function createHttpServer(runtime) {
  return http.createServer(async (req, res) => {
    const url = (req.url || "").split("?")[0];
    if (req.method === "GET" && url === "/ping") {
      sendJson(res, runtime.ping());
      return;
    }
    if (req.method === "POST" && url === "/invocations") {
      let payload;
      try {
        const raw = await readBody(req, MAX_BODY_BYTES);
        payload = raw ? JSON.parse(raw) : {};
      } catch (err) {
        const message = err instanceof CodingError ? err.message : "request body is not JSON";
        if (!res.headersSent && !res.destroyed) {
          sendJson(res, { ok: false, action: null, boot_id: runtime.bootId, error: { code: "bad_request", message } });
        }
        return;
      }
      sendJson(res, await runtime.handle(payload));
      return;
    }
    res.writeHead(404, { "content-type": "application/json" });
    res.end(JSON.stringify({ error: "not found" }));
  });
}

export function main() {
  const config = configFromEnv();
  const runtime = new CodingRuntime(config);
  const server = createHttpServer(runtime);
  const port = Number(process.env.PORT || 8080);
  server.listen(port, "0.0.0.0", () => log(`coding runtime ${config.version} listening :${port} boot=${runtime.bootId}`));
  const stop = () => {
    runtime.shutdown();
    server.close(() => process.exit(0));
    setTimeout(() => process.exit(0), 3000).unref();
  };
  process.on("SIGTERM", stop);
  process.on("SIGINT", stop);
}

if (process.argv[1] && import.meta.url === pathToFileURL(process.argv[1]).href) main();
