/**
 * Where a Markdown link inside a job artifact may go.
 *
 * Relative links resolve against the directory of the file being viewed and
 * stay inside the job's `out/` tree — they are opened through the same scoped
 * artifacts API as a click in the listing, never as a browser navigation. The
 * server's path guard stays authoritative; this only decides what the console
 * is willing to *ask* for. Absolute filesystem paths, protocol-relative URLs,
 * root escapes, every scheme except http/https/mailto, and anything whose
 * percent-decoding is malformed or smuggles a path separator / NUL are refused.
 */
export type BlockedReason = "scheme" | "absolute" | "outside" | "malformed" | "empty";

export type ArtifactLinkTarget =
  | { kind: "external"; href: string }
  | { kind: "artifact"; path: string }
  | { kind: "blocked"; reason: BlockedReason };

export type ResolvedArtifactPath =
  | { ok: true; path: string }
  | { ok: false; reason: "outside" | "malformed" };

const SCHEME = /^[a-zA-Z][a-zA-Z0-9+.-]*:/;
const SAFE_SCHEMES = new Set(["http:", "https:", "mailto:"]);

export const parentDir = (path: string) => {
  const cut = path.lastIndexOf("/");
  return cut === -1 ? "" : path.slice(0, cut);
};

/**
 * Normalize `base/rel` into a tree path. Each `/`-separated segment is
 * percent-decoded on its own, and a decoded segment may not itself contain a
 * separator (`%2F`, `%5C`) or a NUL — an encoded separator is never allowed to
 * become a real one after the split. `..` climbing above the root is `outside`;
 * a segment that does not decode is `malformed`.
 */
export function resolveArtifactPath(baseDir: string, rel: string): ResolvedArtifactPath {
  const segments = baseDir ? baseDir.split("/") : [];
  for (const raw of rel.split("/")) {
    let segment: string;
    try {
      segment = decodeURIComponent(raw);
    } catch {
      return { ok: false, reason: "malformed" };
    }
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (segments.length === 0) return { ok: false, reason: "outside" };
      segments.pop();
      continue;
    }
    if (/[/\\\0]/.test(segment)) return { ok: false, reason: "malformed" };
    segments.push(segment);
  }
  return { ok: true, path: segments.join("/") };
}

export function classifyArtifactHref(href: string | undefined, baseDir: string): ArtifactLinkTarget {
  const raw = (href ?? "").trim();
  if (raw === "") return { kind: "blocked", reason: "empty" };
  const scheme = raw.match(SCHEME);
  if (scheme) {
    return SAFE_SCHEMES.has(scheme[0].toLowerCase())
      ? { kind: "external", href: raw }
      : { kind: "blocked", reason: "scheme" };
  }
  if (raw.startsWith("/") || raw.startsWith("\\") || raw.startsWith("~")) {
    return { kind: "blocked", reason: "absolute" };
  }
  if (raw.includes("\\") || raw.includes("\0")) return { kind: "blocked", reason: "malformed" };
  // In-document anchors and bare queries have nothing to open.
  const target = raw.split("#")[0].split("?")[0];
  if (target === "") return { kind: "blocked", reason: "empty" };
  const resolved = resolveArtifactPath(baseDir, target);
  if (!resolved.ok) return { kind: "blocked", reason: resolved.reason };
  return { kind: "artifact", path: resolved.path }; // "" is the out/ root itself
}
