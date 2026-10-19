import assert from "node:assert/strict";
import fs from "node:fs";
import fsp from "node:fs/promises";
import os from "node:os";
import path from "node:path";
import { after, before, describe, test } from "node:test";

import { sha256 } from "../src/util.mjs";
import { Workspace, cleanRelPath, layout } from "../src/workspace.mjs";
import { cleanup, makeLayout, startBlobServer, tarNames, writeBlob, zipNames } from "./helpers.mjs";

let lay;
let ws;
let blobs;
let outside;

before(async () => {
  lay = await makeLayout();
  ws = new Workspace(layout(lay));
  await ws.prepareDirs();
  await ws.linkCodexHome();
  await ws.initFresh();
  blobs = await startBlobServer();
  outside = await fsp.mkdtemp(path.join(os.tmpdir(), "coding-outside-"));
  await fsp.writeFile(path.join(outside, "private.txt"), "outside the project\n");
});

after(async () => { await cleanup(blobs, lay?.root, outside); });

const projectPath = (...p) => path.join(ws.p.project, ...p);

async function expectCode(promise, code) {
  await assert.rejects(promise, (err) => {
    assert.equal(err.code, code, `expected ${code}, got ${err.code}: ${err.message}`);
    return true;
  });
}

describe("fresh project", () => {
  test("is a git repo on main with the template files committed", async () => {
    assert.ok(fs.existsSync(projectPath(".git")));
    assert.ok(fs.readFileSync(projectPath("AGENTS.md"), "utf8").includes("Launchpad"));
    assert.ok(fs.existsSync(projectPath(".gitignore")));
    const branch = await ws.git(["rev-parse", "--abbrev-ref", "HEAD"]);
    assert.equal(branch.stdout.trim(), "main");
    const tracked = (await ws.git(["ls-files"])).stdout.split("\n").filter(Boolean).sort();
    assert.deepEqual(tracked, [".gitignore", "AGENTS.md"]);
  });

  test("keeps dependency dirs on local disk via symlinks", async () => {
    for (const [name, target] of [["node_modules", "node_modules"], [".venv", "venv"]]) {
      const st = await fsp.lstat(projectPath(name));
      assert.ok(st.isSymbolicLink(), `${name} is a symlink`);
      assert.equal(await fsp.readlink(projectPath(name)), path.join(lay.depsDir, target));
    }
  });

  test("links the image's codex plugins and copies its config", async () => {
    assert.ok((await fsp.lstat(path.join(ws.p.codexHome, "plugins"))).isSymbolicLink());
    assert.ok(fs.readFileSync(path.join(ws.p.codexHome, "config.toml"), "utf8").includes("amazon-bedrock-runtime"));
  });
});

describe("path guard", () => {
  test("cleanRelPath refuses traversal, absolute paths, NUL and .git", () => {
    for (const bad of ["../x", "a/../../x", "a/..", "/etc/passwd", "C:/x", "a\0b", ".git/config", "./.git/HEAD", "..\\x"]) {
      assert.throws(() => cleanRelPath(bad), (e) => e.code === "path_invalid", bad);
    }
    assert.throws(() => cleanRelPath(""), (e) => e.code === "path_invalid");
    assert.equal(cleanRelPath("", { allowRoot: true }), ".");
    assert.equal(cleanRelPath("./src//app.py"), "src/app.py");
    assert.equal(cleanRelPath("a\\b.txt"), "a/b.txt");
  });

  test("a symlink that leaves the project is refused for reads and writes", async () => {
    await fsp.symlink(path.join(outside, "private.txt"), projectPath("escape-file"));
    await fsp.symlink(outside, projectPath("escape-dir"));
    await expectCode(ws.readFile({ path: "escape-file" }), "path_invalid");
    await expectCode(ws.readFile({ path: "escape-dir/private.txt" }), "path_invalid");
    await expectCode(ws.writeFile({ path: "escape-file", content: "x" }), "path_invalid");
    await expectCode(ws.writeFile({ path: "escape-dir/new.txt", content: "x" }), "path_invalid");
    await expectCode(ws.tree({ path: "escape-dir" }), "path_invalid");
    assert.equal(fs.existsSync(path.join(outside, "new.txt")), false);
    assert.equal(fs.readFileSync(path.join(outside, "private.txt"), "utf8"), "outside the project\n");
    await fsp.rm(projectPath("escape-file"));
    await fsp.rm(projectPath("escape-dir"));
  });

  test("an in-project symlink can be read but not written through", async () => {
    await fsp.writeFile(projectPath("real.txt"), "real\n");
    await fsp.symlink("real.txt", projectPath("alias.txt"));
    assert.equal((await ws.readFile({ path: "alias.txt" })).content, "real\n");
    await expectCode(ws.writeFile({ path: "alias.txt", content: "x" }), "path_invalid");
    await fsp.rm(projectPath("alias.txt"));
    await fsp.rm(projectPath("real.txt"));
  });
});

describe("read / write", () => {
  test("write then read round-trips with a sha256", async () => {
    const w = await ws.writeFile({ path: "src/app.py", content: "print('hi')\n" });
    assert.equal(w.created, true);
    assert.equal(w.sha256, sha256(Buffer.from("print('hi')\n")));
    const r = await ws.readFile({ path: "src/app.py" });
    assert.equal(r.content, "print('hi')\n");
    assert.equal(r.sha256, w.sha256);
    assert.equal(r.binary, false);
    assert.equal(r.exists, true);
    assert.deepEqual(await ws.readFile({ path: "src/missing.py" }), { path: "src/missing.py", exists: false });
  });

  test("a stale expected_sha256 is a conflict carrying the current hash", async () => {
    const first = await ws.writeFile({ path: "notes.md", content: "v1\n" });
    const agentEdit = await ws.writeFile({ path: "notes.md", content: "v2 by the agent\n" });
    await assert.rejects(
      ws.writeFile({ path: "notes.md", content: "v1 edited\n", expected_sha256: first.sha256 }),
      (e) => e.code === "conflict" && e.extra.sha256 === agentEdit.sha256,
    );
    assert.equal(fs.readFileSync(projectPath("notes.md"), "utf8"), "v2 by the agent\n", "nothing written");
    const ok = await ws.writeFile({ path: "notes.md", content: "merged\n", expected_sha256: agentEdit.sha256 });
    assert.equal(ok.created, false);
    await assert.rejects(ws.writeFile({ path: "notes.md", content: "x", expected_sha256: null }), (e) => e.code === "conflict");
    await assert.rejects(ws.writeFile({ path: "brand-new.md", content: "x", expected_sha256: "abc" }), (e) => e.code === "conflict");
    await ws.writeFile({ path: "brand-new.md", content: "x", expected_sha256: null });
  });

  test("binary files are flagged without content and big files refused", async () => {
    await fsp.writeFile(projectPath("blob.bin"), Buffer.from([0, 1, 2, 3, 0, 255]));
    const r = await ws.readFile({ path: "blob.bin" });
    assert.equal(r.binary, true);
    assert.equal(r.content, null);
    await fsp.writeFile(projectPath("big.txt"), Buffer.alloc(2 * 1024 * 1024 + 1, 97));
    await expectCode(ws.readFile({ path: "big.txt" }), "file_too_large");
    await expectCode(ws.writeFile({ path: "big2.txt", content: "a".repeat(2 * 1024 * 1024 + 1) }), "file_too_large");
    await fsp.rm(projectPath("big.txt"));
  });

  test("tree lists root-relative entries and hides .git and dependency dirs", async () => {
    const { entries } = await ws.tree({ path: ".", depth: 2 });
    const paths = entries.map((e) => e.path);
    assert.ok(paths.includes("src") && paths.includes("src/app.py") && paths.includes("AGENTS.md"));
    for (const hidden of [".git", "node_modules", ".venv"]) assert.ok(!paths.includes(hidden), hidden);
    assert.equal(entries.find((e) => e.path === "src").dir, true);
    const sub = await ws.tree({ path: "src", depth: 1 });
    assert.deepEqual(sub.entries.map((e) => e.path), ["src/app.py"]);
  });
});

describe("changes, diffs and revisions", () => {
  test("worktree changes include untracked files; the turn base isolates one turn", async () => {
    await ws.gitOk(["add", "-A"]);
    await ws.gitOk(["commit", "-q", "-m", "baseline"]);
    const base = await ws.writeTree();
    const indexBefore = (await ws.git(["diff", "--cached", "--name-only"])).stdout;
    await fsp.writeFile(projectPath("src", "app.py"), "print('changed')\n");
    await fsp.writeFile(projectPath("added.py"), "x = 1\n");
    await fsp.rm(projectPath("brand-new.md"));
    const changes = (await ws.changes("worktree")).changes;
    assert.deepEqual(
      Object.fromEntries(changes.map((c) => [c.path, c.status])),
      { "added.py": "A", "brand-new.md": "D", "src/app.py": "M" },
    );
    const turn = (await ws.changes("turn", base)).changes.map((c) => c.path).sort();
    assert.deepEqual(turn, ["added.py", "brand-new.md", "src/app.py"]);
    assert.equal((await ws.git(["diff", "--cached", "--name-only"])).stdout, indexBefore, "user index untouched");
    const head = await ws.readFile({ path: "src/app.py", rev: "head" });
    assert.equal(head.content, "print('hi')\n");
    const atBase = await ws.readFile({ path: "src/app.py", rev: "turn" }, base);
    assert.equal(atBase.content, "print('hi')\n");
    assert.equal((await ws.readFile({ path: "added.py", rev: "head" })).exists, false);
    const { diff } = await ws.diff("worktree");
    assert.match(diff, /print\('changed'\)/);
    assert.match(diff, /added\.py/);
    await expectCode(ws.changes("turn", null), "no_turn");
    await expectCode(ws.changes("sideways"), "bad_request");
  });
});

describe("snapshot, export and restore", () => {
  test("a snapshot carries the repo and runtime state but no deps or ignored files", async () => {
    await fsp.writeFile(path.join(lay.depsDir, "node_modules", "dep.js"), "module.exports = 1\n");
    await fsp.mkdir(projectPath("__pycache__"), { recursive: true });
    await fsp.writeFile(projectPath("__pycache__", "x.pyc"), "bytecode");
    await fsp.writeFile(projectPath(".env"), "DEBUG=1\n");
    await fsp.mkdir(path.join(ws.p.codexHome, "sessions", "2026"), { recursive: true });
    await fsp.writeFile(path.join(ws.p.codexHome, "sessions", "2026", "rollout.jsonl"), "{}\n");
    await fsp.writeFile(ws.p.eventsFile, `${JSON.stringify({ seq: 1, ts: 1, method: "a", params: {} })}\n`);
    ws.writeState({ threadId: "thr_1" });
    const out = await ws.snapshot(blobs.url("snap.tgz"));
    assert.ok(out.bytes > 0);
    const file = await writeBlob(blobs.blobs, "snap.tgz", path.join(lay.root, "snap.tgz"));
    const names = await tarNames(file);
    for (const want of ["project/AGENTS.md", "project/src/app.py", "project/added.py", "project/.git/HEAD",
      ".launchpad/events.jsonl", ".launchpad/state.json", ".codex/sessions/2026/rollout.jsonl", ".codex/config.toml"]) {
      assert.ok(names.includes(want), `snapshot has ${want}`);
    }
    for (const unwanted of ["project/node_modules", "project/.venv", "project/.env", "project/__pycache__/x.pyc", ".codex/plugins"]) {
      assert.ok(!names.some((n) => n === unwanted || n.startsWith(`${unwanted}/`)), `snapshot lacks ${unwanted}`);
    }
    assert.ok(!names.some((n) => n.includes("launchpad-index-")), "no temp git index");
  });

  test("an export zip is the deployable project only", async () => {
    await fsp.symlink("added.py", projectPath("link.py"));
    const out = await ws.export(blobs.url("export.zip"));
    const file = await writeBlob(blobs.blobs, "export.zip", path.join(lay.root, "export.zip"));
    const names = (await zipNames(file)).sort();
    assert.ok(names.includes("AGENTS.md") && names.includes("src/app.py") && names.includes("added.py"));
    assert.ok(!names.some((n) => n.startsWith(".git/") || n.startsWith("node_modules") || n.startsWith(".venv")));
    assert.ok(!names.includes(".env") && !names.some((n) => n.includes("__pycache__")));
    assert.ok(!names.includes("link.py"), "symlinks are skipped (BYOC refuses them)");
    assert.deepEqual(out.skipped_symlinks, ["link.py"]);
    assert.ok(out.root_files.includes("AGENTS.md"));
    await fsp.rm(projectPath("link.py"));
  });

  test("a snapshot restores into an empty workspace", async () => {
    const other = new Workspace(layout({ ...lay, workspace: path.join(lay.root, "restored"), depsDir: path.join(lay.root, "deps2") }));
    await other.prepareDirs();
    await other.restore(blobs.url("snap.tgz"));
    await other.ensureDepLinks();
    assert.equal(fs.readFileSync(path.join(other.p.project, "src", "app.py"), "utf8"), "print('changed')\n");
    assert.equal(other.readState().threadId, "thr_1");
    assert.ok(fs.existsSync(path.join(other.p.codexHome, "sessions", "2026", "rollout.jsonl")));
    assert.ok((await fsp.lstat(path.join(other.p.project, "node_modules"))).isSymbolicLink());
    const log = await other.git(["log", "--oneline"]);
    assert.match(log.stdout, /baseline/);
    await assert.rejects(other.restore(blobs.url("missing.tgz")), (e) => e.code === "restore_failed");
  });

  test("an upload the URL refuses is upload_failed", async () => {
    await assert.rejects(ws.snapshot("http://127.0.0.1:9/nowhere"), (e) => e.code === "upload_failed");
  });
});
