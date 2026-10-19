import { describe, expect, it } from "vitest";

import {
  ancestorsOf,
  emptyTree,
  fileExists,
  isLoaded,
  mergeListing,
  normalizePath,
  refreshTargets,
  visibleRows,
} from "./fileTree";

const f = (path: string, size = 1) => ({ path, dir: false, size });
const d = (path: string) => ({ path, dir: true, size: null });

describe("fileTree", () => {
  it("sorts directories first, then names naturally", () => {
    const tree = mergeListing(emptyTree(), ".", [f("b.py"), d("src"), f("README.md"), f("a10.txt"), f("a2.txt"), d(".github")]);
    expect(visibleRows(tree, new Set()).map((r) => r.name)).toEqual([".github", "src", "a2.txt", "a10.txt", "b.py", "README.md"]);
    expect(isLoaded(tree, ".")).toBe(true);
    expect(isLoaded(tree, "src")).toBe(false);
  });

  it("lazy-loads a directory under its parent", () => {
    let tree = mergeListing(emptyTree(), ".", [d("src"), f("main.py")]);
    tree = mergeListing(tree, "src", [f("src/app.py"), d("src/lib")]);
    const rows = visibleRows(tree, new Set(["src"]));
    expect(rows.map((r) => [r.path, r.depth])).toEqual([
      ["src", 0],
      ["src/lib", 1],
      ["src/app.py", 1],
      ["main.py", 0],
    ]);
    expect(rows[0]).toMatchObject({ expanded: true, loaded: true });
    expect(rows[1]).toMatchObject({ expanded: false, loaded: false });
  });

  it("reads entries relative to the listed directory when they are not root-relative", () => {
    let tree = mergeListing(emptyTree(), ".", [d("src")]);
    tree = mergeListing(tree, "src", [f("app.py")]);
    expect(fileExists(tree, "src/app.py")).toBe(true);
  });

  it("drops entries (and their subtrees) gone from a re-listing", () => {
    let tree = mergeListing(emptyTree(), ".", [d("old"), f("keep.py")]);
    tree = mergeListing(tree, "old", [f("old/x.py")]);
    tree = mergeListing(tree, ".", [f("keep.py"), f("new.py")]);
    expect(Object.keys(tree.nodes).sort()).toEqual([".", "keep.py", "new.py"]);
  });

  it("keeps a loaded subdirectory's children on a shallow re-listing", () => {
    let tree = mergeListing(emptyTree(), ".", [d("src")]);
    tree = mergeListing(tree, "src", [f("src/app.py")]);
    tree = mergeListing(tree, ".", [d("src"), f("main.py")]);
    expect(fileExists(tree, "src/app.py")).toBe(true);
  });

  it("covers subdirectories above the depth limit", () => {
    let tree = mergeListing(emptyTree(), ".", [d("src"), d("src/lib"), f("src/a.py"), f("src/lib/b.py")], 3);
    expect(isLoaded(tree, "src")).toBe(true);
    expect(isLoaded(tree, "src/lib")).toBe(true);
    // depth 2: src is covered, src/lib is at the limit and keeps its children
    tree = mergeListing(tree, ".", [d("src"), d("src/lib")], 2);
    expect(fileExists(tree, "src/a.py")).toBe(false);
    expect(fileExists(tree, "src/lib/b.py")).toBe(true);
    // an empty covered directory is loaded with no children
    tree = mergeListing(tree, "src", [d("src/lib")], 2);
    expect(fileExists(tree, "src/lib/b.py")).toBe(false);
    expect(isLoaded(tree, "src/lib")).toBe(true);
  });

  it("replaces a directory that became a file", () => {
    let tree = mergeListing(emptyTree(), ".", [d("x")]);
    tree = mergeListing(tree, "x", [f("x/y")]);
    tree = mergeListing(tree, ".", [f("x")]);
    expect(fileExists(tree, "x")).toBe(true);
    expect(tree.nodes["x/y"]).toBeUndefined();
  });

  it("normalizes paths and lists refresh targets and ancestors", () => {
    expect(normalizePath("./a//b/")).toBe("a/b");
    expect(normalizePath("../../etc")).toBe("etc");
    expect(normalizePath("")).toBe(".");
    let tree = mergeListing(emptyTree(), ".", [d("a"), d("b")]);
    tree = mergeListing(tree, "a", [d("a/c")]);
    expect(refreshTargets(tree, new Set(["a", "b"]))).toEqual([".", "a"]);
    expect(ancestorsOf("a/c/d.py")).toEqual(["a", "a/c"]);
    expect(ancestorsOf("top.py")).toEqual([]);
  });

  it("ignores duplicate and root entries", () => {
    const tree = mergeListing(emptyTree(), ".", [f("a.py"), f("./a.py"), d(".")]);
    expect(visibleRows(tree, new Set()).map((r) => r.path)).toEqual(["a.py"]);
  });
});
