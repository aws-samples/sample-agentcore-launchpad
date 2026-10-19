// Shared fixtures for the runtime tests: throwaway directory layouts, a runtime
// driven by the fake app-server, a tiny presigned-URL stand-in (PUT/GET blob
// server), and polling helpers. Everything is torn down by `cleanup()`.
import fs from "node:fs";
import fsp from "node:fs/promises";
import http from "node:http";
import os from "node:os";
import path from "node:path";
import { fileURLToPath } from "node:url";

import { CodingRuntime } from "../src/server.mjs";
import { run } from "../src/util.mjs";

const HERE = path.dirname(fileURLToPath(import.meta.url));
export const IMAGE_DIR = path.resolve(HERE, "..");
export const FAKE_APP_SERVER = path.join(HERE, "fake-app-server.mjs");

// A fresh root with the runtime's directories (workspace = session storage,
// state/deps = local disk, codex template + project templates = the image).
export async function makeLayout() {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), "coding-rt-test-"));
  const codexTemplate = path.join(root, "codex-home");
  await fsp.mkdir(path.join(codexTemplate, "plugins"), { recursive: true });
  await fsp.copyFile(path.join(IMAGE_DIR, "codex-config.toml"), path.join(codexTemplate, "config.toml"));
  return {
    root,
    workspace: path.join(root, "workspace"),
    stateDir: path.join(root, "state"),
    depsDir: path.join(root, "deps"),
    codexTemplate,
    projectTemplates: path.join(IMAGE_DIR, "templates"),
    log: path.join(root, "fake-app-server.log"),
  };
}

// A second runtime layout sharing the image dirs but with its own workspace and
// local disk (a different microVM, e.g. after a version update wiped storage).
export async function siblingLayout(base) {
  const root = await fsp.mkdtemp(path.join(os.tmpdir(), "coding-rt-test-"));
  return {
    ...base,
    root,
    workspace: path.join(root, "workspace"),
    stateDir: path.join(root, "state"),
    depsDir: path.join(root, "deps"),
    log: path.join(root, "fake-app-server.log"),
  };
}

export function makeRuntime(layout, overrides = {}) {
  process.env.FAKE_APP_SERVER_LOG = layout.log;
  return new CodingRuntime({
    workspace: layout.workspace,
    stateDir: layout.stateDir,
    depsDir: layout.depsDir,
    codexTemplate: layout.codexTemplate,
    projectTemplates: layout.projectTemplates,
    models: ["model-a", "model-b"],
    efforts: ["low", "medium", "high", "xhigh"],
    region: "us-west-2",
    version: "test",
    appServerCommand: [process.execPath, FAKE_APP_SERVER],
    ...overrides,
  });
}

export async function call(rt, action, payload = {}) {
  return rt.handle({ action, ...payload });
}

// Call and require ok:true; returns the result.
export async function ok(rt, action, payload = {}) {
  const out = await call(rt, action, payload);
  if (!out.ok) throw new Error(`${action} failed: ${JSON.stringify(out.error)}`);
  return out.result;
}

export async function waitFor(predicate, { timeoutMs = 5000, intervalMs = 10, what = "condition" } = {}) {
  const deadline = Date.now() + timeoutMs;
  for (;;) {
    const value = await predicate();
    if (value) return value;
    if (Date.now() > deadline) throw new Error(`timed out waiting for ${what}`);
    await new Promise((r) => setTimeout(r, intervalMs));
  }
}

export function methods(events) {
  return events.map((e) => e.method);
}

// Wait until the event log holds a record with `method` (optionally matching).
export function waitForEvent(rt, method, match = () => true, opts = {}) {
  return waitFor(
    () => rt.eventLog?.records.find((r) => r.method === method && match(r)),
    { what: method, ...opts },
  );
}

// Presigned-URL stand-in: PUT stores the body under the path, GET returns it.
export async function startBlobServer() {
  const blobs = new Map();
  const counts = { get: 0, put: 0 };
  const server = http.createServer((req, res) => {
    const key = req.url;
    if (req.method === "PUT") {
      counts.put += 1;
      const chunks = [];
      req.on("data", (c) => chunks.push(c));
      req.on("end", () => {
        blobs.set(key, Buffer.concat(chunks));
        res.writeHead(200);
        res.end();
      });
      return;
    }
    if (req.method === "GET") {
      counts.get += 1;
      const body = blobs.get(key);
      if (!body) {
        res.writeHead(404);
        res.end("NoSuchKey");
        return;
      }
      // a slow first byte widens the window for concurrent restore callers
      setTimeout(() => {
        res.writeHead(200, { "content-length": body.length });
        res.end(body);
      }, 50);
      return;
    }
    res.writeHead(405);
    res.end();
  });
  await new Promise((resolve) => server.listen(0, "127.0.0.1", resolve));
  const { port } = server.address();
  return {
    blobs,
    counts,
    url: (key) => `http://127.0.0.1:${port}/${key}`,
    close: () => new Promise((resolve) => { server.closeAllConnections?.(); server.close(() => resolve()); }),
  };
}

export async function tarNames(file) {
  const r = await run("tar", ["-tzf", file]);
  if (r.code !== 0) throw new Error(r.stderr);
  return r.stdout.split("\n").filter(Boolean).map((n) => n.replace(/\/$/, ""));
}

export async function zipNames(file) {
  const r = await run("python3", ["-I", "-c",
    "import sys, zipfile; print('\\n'.join(zipfile.ZipFile(sys.argv[1]).namelist()))", file]);
  if (r.code !== 0) throw new Error(r.stderr);
  return r.stdout.split("\n").filter(Boolean);
}

export async function writeBlob(blobs, key, file) {
  await fsp.writeFile(file, blobs.get(`/${key}`));
  return file;
}

export async function cleanup(...things) {
  for (const thing of things) {
    if (!thing) continue;
    if (thing instanceof CodingRuntime) {
      thing.shutdown();
      if (thing.app.proc) {
        await new Promise((resolve) => {
          const t = setTimeout(resolve, 2000);
          thing.app.proc?.once("exit", () => { clearTimeout(t); resolve(); });
        });
      }
    } else if (typeof thing === "string" && fs.existsSync(thing)) {
      await fsp.rm(thing, { recursive: true, force: true });
    } else if (typeof thing.close === "function") {
      await thing.close();
    }
  }
}
