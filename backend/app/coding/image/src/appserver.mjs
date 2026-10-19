// JSON-RPC client for `codex app-server` over stdio.
//
// Framing (codex 0.163, live-verified in the spike): newline-delimited JSON, requests
// are {id, method, params} with no "jsonrpc" field, responses {id, result|error},
// notifications {method, params}. The handshake is an `initialize` request followed
// by an `initialized` notification. Server->client requests (approvals, elicitation,
// dynamic tools) must be answered or the turn hangs: this platform runs full-auto, so
// every one is declined with an error and reported to `onServerRequest`.
import { spawn as nodeSpawn } from "node:child_process";
import readline from "node:readline";

import { CodingError, log } from "./util.mjs";

const DEFAULT_REQUEST_TIMEOUT_MS = 120000;
const STDERR_LINE_MAX = 2000;

export class AppServer {
  /**
   * @param {object} opts
   * @param {string[]} opts.command   argv[0..] of the app-server (e.g. ["codex", "app-server"])
   * @param {string[]} [opts.extraArgs] appended to the command (config overrides)
   * @param {string} opts.cwd
   * @param {object} opts.env
   * @param {(method: string, params: any) => void} opts.onNotification
   * @param {(method: string, params: any) => void} [opts.onServerRequest]
   * @param {(code: number|null, signal: string|null) => void} [opts.onExit]
   * @param {Function} [opts.spawnFn]  injectable for tests
   * @param {object} [opts.clientInfo]
   */
  constructor(opts) {
    this.opts = opts;
    this.spawnFn = opts.spawnFn || nodeSpawn;
    this.proc = null;
    this.ready = null;
    this.nextId = 1;
    this.pending = new Map();
    this.requestTimeoutMs = opts.requestTimeoutMs || DEFAULT_REQUEST_TIMEOUT_MS;
  }

  get running() {
    return Boolean(this.proc);
  }

  // Spawn + handshake once; concurrent callers share the same promise. After the
  // process exits, the next call spawns a fresh one.
  ensure() {
    if (this.ready) return this.ready;
    this.ready = this._start().catch((err) => {
      const failure = err instanceof CodingError ? err : new CodingError("appserver_failed", String(err?.message || err));
      this._abandon(failure);
      throw failure;
    });
    return this.ready;
  }

  // A start that failed (spawn error, no handshake): forget the process — a
  // failed spawn never emits `exit`, so `_onExit` would not clear it — and fail
  // whatever was still waiting on it. The next ensure() spawns afresh.
  _abandon(err) {
    const proc = this.proc;
    this.proc = null;
    this.ready = null;
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer);
      reject(err);
    }
    this.pending.clear();
    try { proc?.kill("SIGTERM"); } catch { /* already gone */ }
  }

  async _start() {
    const [cmd, ...baseArgs] = this.opts.command;
    const args = [...baseArgs, ...(this.opts.extraArgs || [])];
    const proc = this.spawnFn(cmd, args, {
      cwd: this.opts.cwd,
      env: this.opts.env,
      stdio: ["pipe", "pipe", "pipe"],
    });
    this.proc = proc;
    const spawnError = new Promise((_, reject) => {
      proc.once("error", (err) => reject(new CodingError("appserver_failed", `app-server failed to start: ${err.message}`)));
    });
    spawnError.catch(() => {});
    proc.on("exit", (code, signal) => this._onExit(proc, code, signal));
    readline.createInterface({ input: proc.stdout }).on("line", (line) => this._onLine(line));
    readline.createInterface({ input: proc.stderr }).on("line", (line) => {
      log("[codex]", line.length > STDERR_LINE_MAX ? `${line.slice(0, STDERR_LINE_MAX)}…` : line);
    });
    proc.stdin.on("error", () => {}); // EPIPE after a crash surfaces through `exit`
    await Promise.race([
      this.request("initialize", {
        clientInfo: this.opts.clientInfo || { name: "launchpad-coding", title: "Launchpad Coding", version: "1" },
        capabilities: { experimentalApi: true, requestAttestation: false },
      }, { timeoutMs: 60000 }),
      spawnError,
    ]);
    this.notify("initialized", {});
  }

  _onExit(proc, code, signal) {
    if (this.proc !== proc) return;
    this.proc = null;
    this.ready = null;
    const err = new CodingError("appserver_failed", `app-server exited (code ${code}, signal ${signal})`);
    for (const { reject, timer } of this.pending.values()) {
      clearTimeout(timer);
      reject(err);
    }
    this.pending.clear();
    this.opts.onExit?.(code, signal);
  }

  _onLine(line) {
    if (!line.trim()) return;
    let msg;
    try {
      msg = JSON.parse(line);
    } catch {
      log("app-server: non-JSON line", line.slice(0, 200));
      return;
    }
    const isResponse = msg.id !== undefined && msg.method === undefined
      && (msg.result !== undefined || msg.error !== undefined);
    if (isResponse) {
      const waiter = this.pending.get(msg.id);
      if (!waiter) return;
      this.pending.delete(msg.id);
      clearTimeout(waiter.timer);
      if (msg.error !== undefined) {
        const detail = typeof msg.error === "object" ? (msg.error.message || JSON.stringify(msg.error)) : String(msg.error);
        waiter.reject(new CodingError("appserver_failed", `${waiter.method}: ${detail}`, { rpc_error: msg.error }));
      } else {
        waiter.resolve(msg.result);
      }
      return;
    }
    if (msg.id !== undefined && msg.method) {
      // server -> client request: full-auto platform, decline whatever it asks
      this.send({ id: msg.id, error: { code: -32601, message: "launchpad: client requests are not supported (full-auto)" } });
      this.opts.onServerRequest?.(msg.method, msg.params);
      return;
    }
    if (msg.method) this.opts.onNotification(msg.method, msg.params);
  }

  send(obj) {
    if (!this.proc) throw new CodingError("appserver_failed", "app-server is not running");
    this.proc.stdin.write(JSON.stringify(obj) + "\n");
  }

  notify(method, params) {
    this.send({ method, params });
  }

  request(method, params, { timeoutMs } = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new CodingError("appserver_failed", `${method}: no answer from app-server within ${Math.round((timeoutMs || this.requestTimeoutMs) / 1000)}s`));
      }, timeoutMs || this.requestTimeoutMs);
      timer.unref?.();
      this.pending.set(id, { resolve, reject, timer, method });
      try {
        this.send({ id, method, params });
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(err);
      }
    });
  }

  _kill() {
    const proc = this.proc;
    if (!proc) return;
    try { proc.kill("SIGTERM"); } catch { /* already gone */ }
  }

  stop() {
    this._kill();
  }
}
