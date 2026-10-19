// Where a Markdown link written by the coding agent (or inside a project `.md` file)
// may go. codex links project files absolutely, often with a line:
// `[main.py](/mnt/workspace/project/main.py#L12)`, `…/main.py:12`, `…/main.py:12:3`.
// Followed as a browser navigation those are broken URLs, so a link into the project
// opens the file in the IDE instead; http/https/mailto open in a new tab; everything
// else (other schemes, paths outside the project, malformed escapes) stays inert text.
// Pure, so the rules are unit-tested.
import { resolveArtifactPath } from "../../../lib/skillLabArtifacts";
import { PROJECT_ROOT, projectPath } from "./paths";

export type CodingLinkBlocked = "scheme" | "outside" | "malformed" | "empty";

export type CodingLinkTarget =
  | { kind: "external"; href: string }
  | { kind: "file"; path: string; line: number | null; column: number | null }
  | { kind: "blocked"; reason: CodingLinkBlocked };

const SCHEME = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;
const SAFE_SCHEMES = new Set(["http:", "https:", "mailto:"]);
/** `:12` / `:12:3` closing a path (GNU / compiler style) */
const LINE_SUFFIX = /:(\d+)(?::(\d+))?$/;
/** `#L12`, `#L12C3`, `#L12-L20` (GitHub style) */
const LINE_FRAGMENT = /^L(\d+)(?:C(\d+))?(?:-|$)/i;

const blocked = (reason: CodingLinkBlocked): CodingLinkTarget => ({ kind: "blocked", reason });

const positive = (value: string | undefined): number | null => {
  const n = value === undefined ? NaN : Number(value);
  return Number.isSafeInteger(n) && n >= 1 ? n : null;
};

/**
 * Classify `href` (as react-markdown hands it over: percent-encoded by remark) for a
 * document whose relative links resolve against `baseDir` (project-relative; `""` or
 * `.` = the project root, which is codex's cwd).
 */
export function classifyCodingHref(href: string | null | undefined, baseDir: string): CodingLinkTarget {
  const raw = (href ?? "").trim();
  if (raw === "") return blocked("empty");
  let rest = raw;
  const scheme = raw.match(SCHEME);
  if (scheme) {
    const name = scheme[0].toLowerCase();
    if (SAFE_SCHEMES.has(name)) return { kind: "external", href: raw };
    if (name === "file:") {
      // file:///mnt/workspace/project/x and file://localhost/… — only the path matters
      rest = raw.slice(name.length).replace(/^\/\/(localhost)?(?=\/)/i, "");
      if (!rest.startsWith("/")) return blocked("malformed");
    } else if (!(name.includes(".") && /^\d+(?::\d+)?(?:[#?].*)?$/.test(raw.slice(name.length)))) {
      // `main.py:12` reads like a scheme but is a relative file with a line; anything
      // else with a scheme (javascript:, data:, vscode:, …) is never followed
      return blocked("scheme");
    }
  }
  if (rest.includes("\\") || rest.includes("\0")) return blocked("malformed");

  const hashAt = rest.indexOf("#");
  const fragment = hashAt >= 0 ? rest.slice(hashAt + 1) : "";
  if (hashAt >= 0) rest = rest.slice(0, hashAt);
  const queryAt = rest.indexOf("?");
  if (queryAt >= 0) rest = rest.slice(0, queryAt);

  let line: number | null = null;
  let column: number | null = null;
  const suffix = rest.match(LINE_SUFFIX);
  if (suffix) {
    line = positive(suffix[1]);
    column = line === null ? null : positive(suffix[2]);
    rest = rest.slice(0, suffix.index);
  } else {
    const anchor = fragment.match(LINE_FRAGMENT);
    if (anchor) {
      line = positive(anchor[1]);
      column = line === null ? null : positive(anchor[2]);
    }
  }

  // an in-document anchor, a bare `?query` or a directory has no file to open
  if (rest === "" || rest.endsWith("/")) return blocked("empty");
  if (rest.startsWith("~")) return blocked("outside");

  let path: string;
  if (rest.startsWith("/")) {
    const decoded = resolveArtifactPath("", rest);
    if (!decoded.ok) return blocked(decoded.reason);
    const absolute = `/${decoded.path}`;
    const inside = projectPath(absolute);
    if (inside === null) return blocked(absolute === PROJECT_ROOT ? "empty" : "outside");
    path = inside;
  } else {
    const base = baseDir
      .split("/")
      .filter((segment) => segment !== "" && segment !== ".")
      .join("/");
    const resolved = resolveArtifactPath(base, rest);
    if (!resolved.ok) return blocked(resolved.reason);
    if (resolved.path === "") return blocked("empty");
    path = resolved.path;
  }
  return { kind: "file", path, line, column };
}
