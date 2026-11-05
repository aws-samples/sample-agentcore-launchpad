// The IDE's file tree as plain data: directory listings (`GET …/tree?path=&depth=`)
// merged into one map, directories first then names in natural order, lazily
// expanded. Pure, so the merge and ordering rules are unit-tested.
import type { CodingTreeEntry } from "../../../lib/api";

export const ROOT = ".";

export interface TreeNode {
  path: string;
  name: string;
  dir: boolean;
  size: number | null;
  /** child paths in display order; undefined until the directory was listed */
  children?: string[];
}

export interface TreeState {
  nodes: Record<string, TreeNode>;
}

export interface TreeRow {
  path: string;
  name: string;
  dir: boolean;
  size: number | null;
  depth: number;
  expanded: boolean;
  loaded: boolean;
}

export function emptyTree(): TreeState {
  return { nodes: { [ROOT]: { path: ROOT, name: "", dir: true, size: null } } };
}

/** `a/b/c` from `./a//b/c/`; `.` for the root. Never climbs (`..` segments are dropped). */
export function normalizePath(path: string): string {
  const parts = path.split("/").filter((p) => p && p !== "." && p !== "..");
  return parts.length ? parts.join("/") : ROOT;
}

export function parentOf(path: string): string {
  const at = path.lastIndexOf("/");
  return at < 0 ? ROOT : path.slice(0, at);
}

export function nameOf(path: string): string {
  return path.slice(path.lastIndexOf("/") + 1);
}

const collator = new Intl.Collator(undefined, { numeric: true, sensitivity: "base" });

export function compareNodes(a: Pick<TreeNode, "dir" | "name">, b: Pick<TreeNode, "dir" | "name">): number {
  if (a.dir !== b.dir) return a.dir ? -1 : 1;
  return collator.compare(a.name, b.name) || (a.name < b.name ? -1 : a.name > b.name ? 1 : 0);
}

function dropSubtree(nodes: Record<string, TreeNode>, path: string): void {
  const node = nodes[path];
  if (!node) return;
  for (const child of node.children ?? []) dropSubtree(nodes, child);
  delete nodes[path];
}

/**
 * Merge one listing of `dir` taken to `depth` levels. Entries are relative to the
 * repo root; an entry not under `dir` is read as relative to it. Every directory
 * the listing fully covers (`dir` itself, and its subdirectories above the depth
 * limit) gets exactly the listed children: entries gone from disk leave the tree with
 * their subtrees. Directories at the depth limit keep what they had.
 */
export function mergeListing(tree: TreeState, dir: string, entries: readonly CodingTreeEntry[], depth = 1): TreeState {
  const base = normalizePath(dir);
  const prefix = base === ROOT ? "" : `${base}/`;
  const nodes = { ...tree.nodes };
  const relDepth = (path: string) => (base === ROOT ? path : path.slice(prefix.length)).split("/").length;

  const listed = new Map<string, CodingTreeEntry>();
  for (const entry of entries) {
    let path = normalizePath(entry.path);
    if (prefix && !path.startsWith(prefix)) path = normalizePath(`${prefix}${path}`);
    if (path === ROOT || path === base || listed.has(path)) continue;
    listed.set(path, entry);
  }

  // parent → listed children, every ancestor between `base` and an entry included
  const children = new Map<string, string[]>([[base, []]]);
  const implied = new Set<string>();
  for (const path of listed.keys()) {
    let child = path;
    let parent = parentOf(path);
    for (;;) {
      const kids = children.get(parent) ?? [];
      if (!kids.includes(child)) kids.push(child);
      children.set(parent, kids);
      if (parent === base || parent === ROOT) break;
      if (!listed.has(parent)) implied.add(parent);
      child = parent;
      parent = parentOf(parent);
    }
  }

  for (const [path, entry] of listed) {
    const previous = nodes[path];
    if (!entry.dir && previous?.children) {
      for (const gone of previous.children) dropSubtree(nodes, gone);
    }
    nodes[path] = {
      path,
      name: nameOf(path),
      dir: entry.dir,
      size: entry.dir ? null : (entry.size ?? null),
      children: entry.dir ? previous?.children : undefined,
    };
  }
  for (const path of [base, ...implied]) {
    if (!nodes[path]?.dir) nodes[path] = { path, name: path === ROOT ? "" : nameOf(path), dir: true, size: null, children: nodes[path]?.children };
  }

  const coveredDirs = [base, ...[...listed].filter(([path, e]) => e.dir && relDepth(path) < depth).map(([path]) => path), ...implied];
  for (const path of coveredDirs) {
    const kids = children.get(path) ?? [];
    const keep = new Set(kids);
    for (const old of nodes[path].children ?? []) {
      if (!keep.has(old)) dropSubtree(nodes, old);
    }
    nodes[path] = { ...nodes[path], children: kids.slice().sort((a, b) => compareNodes(nodes[a], nodes[b])) };
  }
  return { nodes };
}

export function isLoaded(tree: TreeState, path: string): boolean {
  return tree.nodes[normalizePath(path)]?.children !== undefined;
}

/** The rows to render: the root's children, each expanded directory's below it. */
export function visibleRows(tree: TreeState, expanded: ReadonlySet<string>): TreeRow[] {
  const rows: TreeRow[] = [];
  const walk = (path: string, depth: number) => {
    for (const childPath of tree.nodes[path]?.children ?? []) {
      const node = tree.nodes[childPath];
      if (!node) continue;
      const open = node.dir && expanded.has(childPath);
      rows.push({
        path: childPath,
        name: node.name,
        dir: node.dir,
        size: node.size,
        depth,
        expanded: open,
        loaded: node.children !== undefined,
      });
      if (open) walk(childPath, depth + 1);
    }
  };
  walk(ROOT, 0);
  return rows;
}

/** Directories to re-list on a refresh: the root and every expanded, loaded one. */
export function refreshTargets(tree: TreeState, expanded: ReadonlySet<string>): string[] {
  const out = [ROOT];
  for (const path of expanded) {
    if (path !== ROOT && tree.nodes[path]?.dir && tree.nodes[path]?.children !== undefined) out.push(path);
  }
  return out;
}

/** The directories to expand so `path` is visible. */
export function ancestorsOf(path: string): string[] {
  const out: string[] = [];
  let parent = parentOf(normalizePath(path));
  while (parent !== ROOT) {
    out.unshift(parent);
    parent = parentOf(parent);
  }
  return out;
}

export function fileExists(tree: TreeState, path: string): boolean {
  const node = tree.nodes[normalizePath(path)];
  return !!node && !node.dir;
}
