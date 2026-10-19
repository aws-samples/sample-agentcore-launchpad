// The project workspace on AgentCore managed session storage, and everything that
// touches its files: layout, fresh init / snapshot restore, the path guard, tree /
// read / write, git-tree baselines for per-turn changes, snapshot and export.
//
// Layout (parent design §2.1):
//   <workspace>/project/      the git repo the agent works in (codex cwd)
//   <workspace>/.codex/       CODEX_HOME — config.toml (from the image), sessions/
//                             (rollouts); plugins + .tmp link to the image copy
//   <workspace>/.launchpad/   events.jsonl, state.json, initialized marker
//   <stateDir>                local disk: codex sqlite_home (SQLite cannot live on
//                             session storage — NFS), scratch files
//   <depsDir>                 local disk: node_modules / venv targets, tool caches
// Session storage is 1 GB, so dependency dirs are symlinks to local disk; they
// come back empty after a microVM restart and are reinstalled by the agent.
import crypto from "node:crypto";
import fs from "node:fs";
import fsp from "node:fs/promises";
import path from "node:path";

import { CodingError, getFile, putFile, run, runOk, sha256 } from "./util.mjs";

export const MAX_FILE_BYTES = 2 * 1024 * 1024;
export const MAX_EXPORT_BYTES = 50 * 1024 * 1024;
export const MAX_DIFF_BYTES = 4 * 1024 * 1024;
export const MAX_TREE_ENTRIES = 5000;
// Never listed, never snapshotted or exported.
export const HIDDEN_NAMES = new Set([".git", "node_modules", ".venv", "__pycache__"]);
// project/<link> -> <depsDir>/<target>
export const DEP_LINKS = [
  ["node_modules", "node_modules"],
  [".venv", "venv"],
];
const GIT_IDENTITY = [
  ["user.name", "Launchpad Coding Agent"],
  ["user.email", "coding-agent@launchpad.local"],
];
const EXPORT_ZIP_PY = [
  "import os, sys, zipfile",
  "out, root = sys.argv[1], sys.argv[2]",
  "names = [n for n in sys.stdin.buffer.read().split(b'\\0') if n]",
  "with zipfile.ZipFile(out, 'w', zipfile.ZIP_DEFLATED) as zf:",
  "    for raw in names:",
  "        rel = os.fsdecode(raw)",
  "        zf.write(os.path.join(root, rel), rel)",
].join("\n");

export function layout(opts = {}) {
  const workspace = opts.workspace || "/mnt/workspace";
  const meta = path.join(workspace, ".launchpad");
  const stateDir = opts.stateDir || "/var/lib/coding-state";
  return {
    workspace,
    project: path.join(workspace, "project"),
    codexHome: path.join(workspace, ".codex"),
    meta,
    eventsFile: path.join(meta, "events.jsonl"),
    stateFile: path.join(meta, "state.json"),
    markerFile: path.join(meta, "initialized"),
    stateDir,
    scratchDir: path.join(stateDir, "scratch"),
    depsDir: opts.depsDir || "/var/lib/coding-deps",
    codexTemplate: opts.codexTemplate || "/opt/codex-home",
    projectTemplates: opts.projectTemplates || "/opt/coding-templates",
  };
}

// Validate a client-supplied project-relative path. Refuses absolute paths, any
// `..` segment, NUL bytes and `.git`. Returns the normalized POSIX form ("." for
// the root).
export function cleanRelPath(p, { allowRoot = false } = {}) {
  if (p === undefined || p === null || p === "" || p === ".") {
    if (allowRoot) return ".";
    throw new CodingError("path_invalid", "a file path is required");
  }
  if (typeof p !== "string" || p.length > 4096 || p.includes("\0")) {
    throw new CodingError("path_invalid", "invalid path");
  }
  const unified = p.replaceAll("\\", "/");
  if (unified.startsWith("/") || /^[A-Za-z]:/.test(unified)) {
    throw new CodingError("path_invalid", "absolute paths are not allowed");
  }
  const parts = unified.split("/").filter((s) => s !== "" && s !== ".");
  if (parts.includes("..")) throw new CodingError("path_invalid", "'..' is not allowed in a path");
  if (!parts.length) {
    if (allowRoot) return ".";
    throw new CodingError("path_invalid", "a file path is required");
  }
  if (parts[0] === ".git") throw new CodingError("path_invalid", "the .git directory is not accessible");
  return parts.join("/");
}

const within = (root, full) => full === root || full.startsWith(root + path.sep);

async function lstatOrNull(p) {
  try { return await fsp.lstat(p); } catch { return null; }
}

function decodeText(buf) {
  if (buf.subarray(0, 8192).includes(0)) return null;
  try {
    return new TextDecoder("utf-8", { fatal: true }).decode(buf);
  } catch {
    return null;
  }
}

export class Workspace {
  constructor(paths) {
    this.p = paths;
  }

  // ------------------------------------------------------------- state + layout
  isInitialized() {
    return fs.existsSync(this.p.markerFile);
  }

  markInitialized() {
    fs.writeFileSync(this.p.markerFile, `${new Date().toISOString()}\n`);
  }

  readState() {
    try {
      return JSON.parse(fs.readFileSync(this.p.stateFile, "utf8"));
    } catch {
      return {};
    }
  }

  writeState(state) {
    const tmp = `${this.p.stateFile}.tmp-${process.pid}`;
    fs.writeFileSync(tmp, JSON.stringify(state, null, 2));
    fs.renameSync(tmp, this.p.stateFile);
  }

  async prepareDirs() {
    for (const dir of [this.p.meta, this.p.codexHome, this.p.stateDir, this.p.scratchDir, this.p.depsDir]) {
      await fsp.mkdir(dir, { recursive: true });
    }
  }

  // CODEX_HOME: config.toml is re-copied from the image on every boot (the image owns
  // the provider + plugin config, so a version update rolls it out); the plugin
  // payload and the marketplace checkout stay in the image, linked.
  async linkCodexHome() {
    await fsp.mkdir(this.p.codexHome, { recursive: true });
    const template = path.join(this.p.codexTemplate, "config.toml");
    if (fs.existsSync(template)) {
      await fsp.copyFile(template, path.join(this.p.codexHome, "config.toml"));
    }
    for (const name of ["plugins", ".tmp"]) {
      const link = path.join(this.p.codexHome, name);
      const target = path.join(this.p.codexTemplate, name);
      if (!fs.existsSync(target)) continue;
      const st = await lstatOrNull(link);
      if (st?.isSymbolicLink() && (await fsp.readlink(link)) === target) continue;
      if (st) await fsp.rm(link, { recursive: true, force: true });
      await fsp.symlink(target, link);
    }
  }

  // node_modules / .venv live on local disk. Their targets are (re)created empty on
  // every boot; a link the agent replaced with a real directory is left alone.
  async ensureDepLinks() {
    if (!fs.existsSync(this.p.project)) return;
    for (const [name, target] of DEP_LINKS) {
      const dest = path.join(this.p.depsDir, target);
      await fsp.mkdir(dest, { recursive: true });
      const link = path.join(this.p.project, name);
      if (!(await lstatOrNull(link))) await fsp.symlink(dest, link);
    }
  }

  git(args, opts = {}) {
    return run("git", ["-c", "core.quotepath=off", ...args], { cwd: this.p.project, ...opts });
  }

  gitOk(args, opts = {}) {
    return runOk("git", ["-c", "core.quotepath=off", ...args], { cwd: this.p.project, ...opts });
  }

  async initFresh() {
    await fsp.mkdir(this.p.project, { recursive: true });
    if (!fs.existsSync(path.join(this.p.project, ".git"))) {
      await this.gitOk(["init", "-q", "-b", "main"]);
    }
    for (const [key, value] of GIT_IDENTITY) {
      const has = await this.git(["config", "--local", "--get", key]);
      if (has.code !== 0) await this.gitOk(["config", "--local", key, value]);
    }
    for (const [src, dest] of [["AGENTS.md", "AGENTS.md"], ["gitignore", ".gitignore"]]) {
      const from = path.join(this.p.projectTemplates, src);
      const to = path.join(this.p.project, dest);
      if (fs.existsSync(from) && !fs.existsSync(to)) await fsp.copyFile(from, to);
    }
    await this.ensureDepLinks();
    if (!(await this.headTree())) {
      await this.gitOk(["add", "-A"]);
      const staged = await this.git(["diff", "--cached", "--quiet"]);
      if (staged.code !== 0) await this.gitOk(["commit", "-q", "-m", "Initialize project"]);
    }
  }

  async restore(url) {
    const tmp = path.join(this.p.scratchDir, `restore-${crypto.randomUUID()}.tgz`);
    try {
      let bytes;
      try {
        ({ bytes } = await getFile(url, tmp));
      } catch (e) {
        throw new CodingError("restore_failed", `snapshot download failed: ${e.message}`);
      }
      // Extracted over whatever is there: a missing marker never deletes files.
      const r = await run("tar", ["-xzf", tmp, "-C", this.p.workspace, "--no-same-owner"], { timeoutMs: 600000 });
      if (r.code !== 0) throw new CodingError("restore_failed", `snapshot extract failed: ${r.stderr.trim().slice(0, 300)}`);
      return { bytes };
    } finally {
      await fsp.rm(tmp, { force: true });
    }
  }

  // ------------------------------------------------------------- path guard
  // Absolute path for a cleaned relative path. The deepest existing ancestor's real
  // path must stay inside the project's real path (symlink escape), and so must the
  // target when it exists.
  async resolve(rel, { forWrite = false } = {}) {
    const root = await fsp.realpath(this.p.project);
    const full = path.join(root, rel === "." ? "" : rel);
    const st = await lstatOrNull(full);
    if (st) {
      if (forWrite && st.isSymbolicLink()) throw new CodingError("path_invalid", "cannot write through a symlink");
      const real = await fsp.realpath(full).catch(() => null);
      if (!real || !within(root, real)) throw new CodingError("path_invalid", "path escapes the project");
      return { full, st };
    }
    let probe = path.dirname(full);
    while (!(await lstatOrNull(probe))) probe = path.dirname(probe);
    const real = await fsp.realpath(probe);
    if (!within(root, real)) throw new CodingError("path_invalid", "path escapes the project");
    return { full, st: null };
  }

  // ------------------------------------------------------------- tree
  async tree({ path: p = ".", depth = 1 } = {}) {
    const rel = cleanRelPath(p, { allowRoot: true });
    const maxDepth = Math.min(Math.max(Number(depth) || 1, 1), 4);
    const { full, st } = await this.resolve(rel);
    if (!st) throw new CodingError("not_found", `${rel} does not exist`);
    if (!st.isDirectory()) throw new CodingError("path_invalid", `${rel} is not a directory`);
    const entries = [];
    let truncated = false;
    const walk = async (dirFull, dirRel, level) => {
      let items;
      try {
        items = await fsp.readdir(dirFull, { withFileTypes: true });
      } catch {
        return;
      }
      items.sort((a, b) => (Number(b.isDirectory()) - Number(a.isDirectory())) || a.name.localeCompare(b.name));
      for (const item of items) {
        if (HIDDEN_NAMES.has(item.name)) continue;
        if (entries.length >= MAX_TREE_ENTRIES) { truncated = true; return; }
        const childRel = dirRel === "." ? item.name : `${dirRel}/${item.name}`;
        const childFull = path.join(dirFull, item.name);
        if (item.isSymbolicLink()) {
          entries.push({ path: childRel, dir: false, symlink: true, size: 0 });
        } else if (item.isDirectory()) {
          entries.push({ path: childRel, dir: true });
          if (level + 1 < maxDepth) await walk(childFull, childRel, level + 1);
        } else if (item.isFile()) {
          const s = await lstatOrNull(childFull);
          entries.push({ path: childRel, dir: false, size: s?.size ?? 0 });
        }
      }
    };
    await walk(full, rel, 0);
    return { path: rel, depth: maxDepth, entries, truncated };
  }

  // ------------------------------------------------------------- read / write
  // rev: undefined (working tree) | "head" | a tree id (the turn baseline)
  async readFile({ path: p, rev } = {}, revTree = null) {
    const rel = cleanRelPath(p);
    let buf;
    if (rev === undefined || rev === null || rev === "worktree") {
      const { full, st: lst } = await this.resolve(rel);
      if (!lst) return { path: rel, exists: false };
      // resolve() proved an in-project symlink's target stays inside: follow it
      const st = lst.isSymbolicLink() ? await fsp.stat(full) : lst;
      if (!st.isFile()) throw new CodingError("path_invalid", `${rel} is not a regular file`);
      if (st.size > MAX_FILE_BYTES) throw new CodingError("file_too_large", `${rel} is larger than 2 MB`, { size: st.size });
      buf = await fsp.readFile(full);
    } else {
      const tree = rev === "head" ? await this.headTree() : revTree;
      if (!tree) return { path: rel, exists: false, rev };
      const spec = `${tree}:${rel}`;
      const type = await this.git(["cat-file", "-t", spec]);
      if (type.code !== 0 || type.stdout.trim() !== "blob") return { path: rel, exists: false, rev };
      const size = Number((await this.gitOk(["cat-file", "-s", spec])).stdout.trim());
      if (size > MAX_FILE_BYTES) throw new CodingError("file_too_large", `${rel} is larger than 2 MB`, { size });
      buf = (await this.gitOk(["cat-file", "blob", spec], { encoding: "buffer" })).stdout;
    }
    const text = decodeText(buf);
    return {
      path: rel,
      exists: true,
      ...(rev ? { rev } : {}),
      content: text,
      binary: text === null,
      sha256: sha256(buf),
      size: buf.length,
    };
  }

  // expected_sha256: undefined → overwrite unconditionally; a hash → the file must
  // currently have it; null → the file must not exist yet.
  async writeFile({ path: p, content, expected_sha256: expected } = {}) {
    const rel = cleanRelPath(p);
    if (typeof content !== "string") throw new CodingError("bad_request", "content must be a string");
    const data = Buffer.from(content, "utf8");
    if (data.length > MAX_FILE_BYTES) throw new CodingError("file_too_large", "content is larger than 2 MB", { size: data.length });
    const { full, st } = await this.resolve(rel, { forWrite: true });
    if (st && !st.isFile()) throw new CodingError("path_invalid", `${rel} is not a regular file`);
    if (expected !== undefined) {
      const current = st ? sha256(await fsp.readFile(full)) : null;
      if (current !== (expected || null)) {
        throw new CodingError("conflict", `${rel} changed since it was read`, { sha256: current });
      }
    }
    await fsp.mkdir(path.dirname(full), { recursive: true });
    const tmp = path.join(path.dirname(full), `.${path.basename(full)}.launchpad-${crypto.randomUUID()}`);
    try {
      await fsp.writeFile(tmp, data);
      if (st) await fsp.chmod(tmp, st.mode & 0o7777);
      await fsp.rename(tmp, full);
    } finally {
      await fsp.rm(tmp, { force: true });
    }
    return { path: rel, sha256: sha256(data), size: data.length, created: !st };
  }

  // ------------------------------------------------------------- git baselines
  async headTree() {
    const r = await this.git(["rev-parse", "--verify", "-q", "HEAD^{tree}"]);
    return r.code === 0 ? r.stdout.trim() : null;
  }

  async emptyTree() {
    return (await this.gitOk(["hash-object", "-t", "tree", "-w", "--stdin"], { input: "" })).stdout.trim();
  }

  // The working tree as a git tree object (tracked + untracked-not-ignored), built
  // through a private temporary index: the user's (and the agent's) index is never
  // touched. Seeded from HEAD so tracked-but-ignored files stay in.
  async writeTree() {
    const index = path.join(this.p.project, ".git", `launchpad-index-${crypto.randomUUID()}`);
    const env = { ...process.env, GIT_INDEX_FILE: index };
    try {
      const head = await this.headTree();
      await this.gitOk(head ? ["read-tree", head] : ["read-tree", "--empty"], { env });
      await this.gitOk(["add", "-A"], { env, timeoutMs: 300000 });
      return (await this.gitOk(["write-tree"], { env })).stdout.trim();
    } finally {
      await fsp.rm(index, { force: true });
      await fsp.rm(`${index}.lock`, { force: true });
    }
  }

  async _base(scope, turnBase) {
    if (scope === "turn") {
      if (!turnBase) throw new CodingError("no_turn", "no turn has run in this project yet");
      return turnBase;
    }
    if (scope !== "worktree") throw new CodingError("bad_request", "scope must be 'worktree' or 'turn'");
    return (await this.headTree()) || (await this.emptyTree());
  }

  // [{path, status: A|M|D|R, old_path?}] between the scope's base and the working tree.
  async changes(scope, turnBase = null) {
    const base = await this._base(scope, turnBase);
    const current = await this.writeTree();
    const r = await this.gitOk(["diff-tree", "-r", "-M", "-z", "--name-status", "--no-commit-id", base, current]);
    const tokens = r.stdout.split("\0");
    const out = [];
    for (let i = 0; i < tokens.length;) {
      const status = tokens[i];
      if (!status) { i += 1; continue; }
      const kind = status[0];
      if (kind === "R" || kind === "C") {
        out.push({ path: tokens[i + 2], status: kind === "R" ? "R" : "A", old_path: tokens[i + 1] });
        i += 3;
      } else {
        out.push({ path: tokens[i + 1], status: kind === "T" ? "M" : kind });
        i += 2;
      }
    }
    return { scope, base, changes: out };
  }

  // Unified diff between the scope's base and the working tree (for "copy patch").
  async diff(scope, turnBase = null) {
    const base = await this._base(scope, turnBase);
    const current = await this.writeTree();
    const r = await this.gitOk(["diff-tree", "-r", "-p", "-M", "--no-commit-id", base, current], { maxBuffer: 256 << 20 });
    let diff = r.stdout;
    let truncated = false;
    if (Buffer.byteLength(diff) > MAX_DIFF_BYTES) {
      diff = Buffer.from(diff).subarray(0, MAX_DIFF_BYTES).toString("utf8");
      truncated = true;
    }
    return { scope, diff, truncated };
  }

  // ------------------------------------------------------------- file selection
  // Project files that are tracked or untracked-but-not-ignored (relative to the
  // project). Dependency dirs are ignored by the template .gitignore; HIDDEN_NAMES
  // are dropped regardless, so an edited .gitignore cannot pull them in.
  async projectFiles() {
    let names;
    const r = await this.git(["ls-files", "-z", "-c", "-o", "--exclude-standard"]);
    if (r.code === 0) {
      names = [...new Set(r.stdout.split("\0").filter(Boolean))];
    } else {
      names = await this._walkFiles(this.p.project, "");
    }
    const out = [];
    for (const rel of names.sort()) {
      if (rel.split("/").some((part) => HIDDEN_NAMES.has(part))) continue;
      const st = await lstatOrNull(path.join(this.p.project, rel));
      if (!st || st.isDirectory()) continue; // deleted in the worktree, or a gitlink
      out.push({ rel, symlink: st.isSymbolicLink(), size: st.size });
    }
    return out;
  }

  async _walkFiles(dirFull, dirRel) {
    const out = [];
    for (const item of await fsp.readdir(dirFull, { withFileTypes: true })) {
      if (HIDDEN_NAMES.has(item.name)) continue;
      const rel = dirRel ? `${dirRel}/${item.name}` : item.name;
      if (item.isDirectory()) out.push(...(await this._walkFiles(path.join(dirFull, item.name), rel)));
      else out.push(rel);
    }
    return out;
  }

  // Workspace-relative entries a snapshot carries.
  async snapshotEntries() {
    const entries = (await this.projectFiles()).map((f) => `project/${f.rel}`);
    const optional = ["project/.git", ".launchpad/events.jsonl", ".launchpad/state.json",
      ".codex/sessions", ".codex/config.toml"];
    for (const rel of optional) {
      if (await lstatOrNull(path.join(this.p.workspace, rel))) entries.push(rel);
    }
    return entries;
  }

  async snapshot(putUrl) {
    const t0 = Date.now();
    const entries = await this.snapshotEntries();
    const id = crypto.randomUUID();
    const list = path.join(this.p.scratchDir, `snapshot-${id}.list`);
    const tgz = path.join(this.p.scratchDir, `snapshot-${id}.tgz`);
    try {
      await fsp.writeFile(list, entries.join("\0") + "\0");
      const r = await run("tar", [
        "-czf", tgz, "-C", this.p.workspace,
        "--exclude=node_modules", "--exclude=.venv", "--exclude=__pycache__",
        "--exclude=launchpad-index-*",
        "--warning=no-file-changed", "--null", "-T", list,
      ], { timeoutMs: 600000 });
      // GNU tar exits 1 when a file changed while it was read: the archive is complete
      if (r.code !== 0 && r.code !== 1) {
        throw new CodingError("internal", `snapshot archive failed: ${r.stderr.trim().slice(0, 300)}`);
      }
      const { bytes } = await putFile(putUrl, tgz);
      return { bytes, files: entries.length, ms: Date.now() - t0 };
    } finally {
      await fsp.rm(list, { force: true });
      await fsp.rm(tgz, { force: true });
    }
  }

  // A zip of the project's files (tracked + untracked-not-ignored, no .git, no
  // symlinks — the BYOC upload check refuses them) for the deploy handoff.
  async export(putUrl) {
    const files = await this.projectFiles();
    const regular = files.filter((f) => !f.symlink);
    const skipped = files.filter((f) => f.symlink).map((f) => f.rel);
    const zip = path.join(this.p.scratchDir, `export-${crypto.randomUUID()}.zip`);
    try {
      const r = await run("python3", ["-I", "-c", EXPORT_ZIP_PY, zip, this.p.project], {
        input: regular.map((f) => f.rel).join("\0"),
        timeoutMs: 600000,
      });
      if (r.code !== 0) throw new CodingError("internal", `export archive failed: ${r.stderr.trim().slice(-300)}`);
      const { size } = await fsp.stat(zip);
      if (size > MAX_EXPORT_BYTES) {
        throw new CodingError("file_too_large", `the project export is ${(size / 1e6).toFixed(1)} MB (limit 50 MB)`, { size });
      }
      const { bytes } = await putFile(putUrl, zip);
      const rootFiles = regular.map((f) => f.rel).filter((rel) => !rel.includes("/"));
      return { bytes, files: regular.length, skipped_symlinks: skipped, root_files: rootFiles };
    } finally {
      await fsp.rm(zip, { force: true });
    }
  }

  async usageBytes() {
    const r = await run("du", ["-sb", this.p.workspace], { timeoutMs: 20000 });
    if (r.code !== 0 && !r.stdout) return null;
    const n = Number.parseInt(r.stdout.split(/\s+/)[0], 10);
    return Number.isFinite(n) ? n : null;
  }
}
