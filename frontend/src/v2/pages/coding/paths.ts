// Codex reports paths absolutely (`/mnt/workspace/project/README.md`), while the IDE's
// tree, tabs and Changes tab are project-relative (`README.md`). Every codex path that
// reaches the UI goes through `projectPath` — one rule, unit-tested.

/** The project's working directory in the coding runtime (parent design §2.1). */
export const PROJECT_ROOT = "/mnt/workspace/project";

const ROOT_SEGMENTS = PROJECT_ROOT.split("/").filter(Boolean);

/**
 * A path as codex reported it → project-relative (`src/app.py`). Absolute paths must
 * lie under `PROJECT_ROOT`; relative paths are relative to the project (codex's cwd)
 * and come back as they were, minus `.` / empty segments. `..` is resolved and never
 * climbs out. `null` when the path is empty, outside the project (another absolute
 * path, `~`, a `..` escape) or the project root itself (no file to open).
 */
export function projectPath(raw: string | null | undefined): string | null {
  if (typeof raw !== "string" || raw === "" || raw.startsWith("~")) return null;
  const absolute = raw.startsWith("/");
  const segments: string[] = [];
  for (const segment of raw.split("/")) {
    if (segment === "" || segment === ".") continue;
    if (segment === "..") {
      if (segments.length === 0) return null;
      segments.pop();
      continue;
    }
    segments.push(segment);
  }
  if (absolute) {
    if (segments.length <= ROOT_SEGMENTS.length) return null;
    if (ROOT_SEGMENTS.some((segment, i) => segments[i] !== segment)) return null;
    return segments.slice(ROOT_SEGMENTS.length).join("/");
  }
  return segments.length ? segments.join("/") : null;
}

/** How a codex path is shown: project-relative when inside the project, else as reported. */
export function displayPath(raw: string): string {
  return projectPath(raw) ?? raw;
}
