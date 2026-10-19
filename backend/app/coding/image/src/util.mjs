// Small shared helpers: subprocesses, hashing, presigned-URL transfers, typed errors.
// Zero dependencies — the image ships nothing but Node's standard library.
import { execFile, spawn } from "node:child_process";
import crypto from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import http from "node:http";
import https from "node:https";

// Application error with a stable code; the server answers {ok:false, error:{code,message}}.
export class CodingError extends Error {
  constructor(code, message, extra = undefined) {
    super(message);
    this.code = code;
    this.extra = extra;
  }
}

export const ERROR_CODES = new Set([
  "turn_running",
  "no_turn",
  "path_invalid",
  "file_too_large",
  "conflict",
  "not_found",
  "restore_failed",
  "appserver_failed",
  "upload_failed",
  "bad_request",
  "internal",
]);

export const sha256 = (buf) => crypto.createHash("sha256").update(buf).digest("hex");

export const log = (...args) => console.log(new Date().toISOString(), ...args);

// Run a command without a shell. stdin is closed unless `input` is given (codex and
// friends block on an open, idle stdin). Never rejects: the caller reads `code`.
export function run(cmd, args, { cwd, env, input, timeoutMs = 120000, maxBuffer = 64 << 20,
  encoding = "utf8" } = {}) {
  return new Promise((resolve) => {
    const child = execFile(
      cmd,
      args,
      { cwd, env, timeout: timeoutMs, maxBuffer, encoding: encoding === "buffer" ? "buffer" : "utf8" },
      (err, stdout, stderr) => {
        const code = err ? (typeof err.code === "number" ? err.code : 1) : 0;
        resolve({ code, stdout, stderr: String(stderr ?? ""), error: err || null });
      },
    );
    if (input !== undefined) child.stdin.end(input);
    else child.stdin.end();
  });
}

// Like run() but throws CodingError(internal) on a non-zero exit.
export async function runOk(cmd, args, opts = {}) {
  const r = await run(cmd, args, opts);
  if (r.code !== 0) {
    const what = `${cmd} ${args.slice(0, 3).join(" ")}`;
    throw new CodingError("internal", `${what} failed (${r.code}): ${r.stderr.trim().slice(0, 500)}`);
  }
  return r;
}

function transport(url) {
  return url.protocol === "https:" ? https : http;
}

// PUT a local file to a presigned URL. S3 refuses chunked uploads without a
// Content-Length, so the body is a sized file stream rather than a fetch() stream.
export async function putFile(url, file, { timeoutMs = 600000 } = {}) {
  const target = new URL(url);
  const { size } = await fsp.stat(file);
  return new Promise((resolve, reject) => {
    const req = transport(target).request(
      target,
      { method: "PUT", headers: { "Content-Length": size }, timeout: timeoutMs },
      (res) => {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => {
          if (res.statusCode >= 200 && res.statusCode < 300) resolve({ status: res.statusCode, bytes: size });
          else {
            const body = Buffer.concat(chunks).toString("utf8").slice(0, 300);
            reject(new CodingError("upload_failed", `upload answered HTTP ${res.statusCode}: ${body}`));
          }
        });
      },
    );
    req.on("timeout", () => req.destroy(new Error("upload timed out")));
    req.on("error", (e) => reject(e instanceof CodingError ? e : new CodingError("upload_failed", `upload failed: ${e.message}`)));
    fs.createReadStream(file).on("error", (e) => req.destroy(e)).pipe(req);
  });
}

// GET a presigned URL into a local file.
export async function getFile(url, file, { timeoutMs = 600000 } = {}) {
  const target = new URL(url);
  return new Promise((resolve, reject) => {
    const req = transport(target).get(target, { timeout: timeoutMs }, (res) => {
      if (res.statusCode !== 200) {
        const chunks = [];
        res.on("data", (c) => chunks.push(c));
        res.on("end", () => reject(new Error(
          `download answered HTTP ${res.statusCode}: ${Buffer.concat(chunks).toString("utf8").slice(0, 300)}`)));
        return;
      }
      const out = fs.createWriteStream(file);
      let bytes = 0;
      res.on("data", (c) => { bytes += c.length; });
      res.pipe(out);
      out.on("finish", () => resolve({ bytes }));
      out.on("error", reject);
    });
    req.on("timeout", () => req.destroy(new Error("download timed out")));
    req.on("error", reject);
  });
}

export { spawn };
