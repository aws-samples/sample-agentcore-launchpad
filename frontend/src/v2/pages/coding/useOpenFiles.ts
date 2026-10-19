import { useCallback, useRef, useState } from "react";

import i18n from "../../../i18n";
import { ApiError, codingApi, errorMessage } from "../../../lib/api";
import { normalizePath } from "./fileTree";

/** One editor tab: the file as last read from disk and the member's draft of it. */
export interface OpenFile {
  path: string;
  loading: boolean;
  error: string | null;
  binary: boolean;
  /** content as last read from (or saved to) the workspace */
  content: string;
  draft: string;
  sha256: string | null;
  saving: boolean;
  /** a save was refused because the file changed since it was read */
  conflict: { sha256: string | null } | null;
  /** the file changed on disk (an agent turn) while the draft had unsaved edits */
  changedOnDisk: boolean;
  /** Markdown files: rendered instead of the editor */
  preview: boolean;
}

export const isDirty = (file: OpenFile) => !file.loading && !file.binary && file.draft !== file.content;

/** `unchanged`: nothing to write (a clean tab, or one still loading / failed to load). */
export type SaveResult = "saved" | "conflict" | "failed" | "unchanged";

/**
 * The IDE's open files. Reads go through `GET …/file`; a save sends the sha256 it
 * read (`expected_sha256`), so a file the agent changed in between is refused with
 * `409 coding.file_conflict` instead of being overwritten — the tab then offers
 * reload (drop the draft) or overwrite (save against the newer version).
 */
export function useOpenFiles(projectId: string) {
  const [files, setFiles] = useState<OpenFile[]>([]);
  const filesRef = useRef(files);
  filesRef.current = files;
  // saves in flight, by path: a second ⌘S before the first answer must not race it
  const inFlight = useRef(new Set<string>());

  const patch = useCallback((path: string, change: Partial<OpenFile> | ((file: OpenFile) => Partial<OpenFile>)) => {
    setFiles((prev) => prev.map((f) => (f.path === path ? { ...f, ...(typeof change === "function" ? change(f) : change) } : f)));
  }, []);

  const read = useCallback(
    async (path: string, keepDraft: boolean) => {
      try {
        const file = await codingApi.readFile(projectId, path);
        // a file gone from disk (the agent deleted or moved it) reads `exists: false`
        // with no content, hash or binary flag
        const missing = file.exists === false;
        const content = file.content ?? "";
        const sha256 = missing ? null : (file.sha256 ?? null);
        patch(path, (f) => {
          const dirty = keepDraft && f.draft !== f.content;
          if (missing && !dirty) {
            // no empty editor whose save would quietly recreate the file
            return {
              loading: false,
              error: i18n.t("v2.coding.editor.missing"),
              binary: false,
              content: "",
              draft: "",
              sha256: null,
              changedOnDisk: false,
              conflict: null,
            };
          }
          return {
            loading: false,
            error: null,
            binary: !!file.binary,
            content,
            draft: dirty ? f.draft : content,
            sha256: dirty ? f.sha256 : sha256,
            changedOnDisk: dirty && sha256 !== f.sha256,
            conflict: dirty ? f.conflict : null,
          };
        });
      } catch (err) {
        patch(path, { loading: false, error: errorMessage(err) });
      }
    },
    [projectId, patch],
  );

  const open = useCallback(
    (raw: string) => {
      const path = normalizePath(raw);
      if (filesRef.current.some((f) => f.path === path)) return path;
      setFiles((prev) => [
        ...prev,
        {
          path,
          loading: true,
          error: null,
          binary: false,
          content: "",
          draft: "",
          sha256: null,
          saving: false,
          conflict: null,
          changedOnDisk: false,
          preview: false,
        },
      ]);
      void read(path, false);
      return path;
    },
    [read],
  );

  const close = useCallback((path: string) => setFiles((prev) => prev.filter((f) => f.path !== path)), []);

  // called on every keystroke (and by Monaco echoing a model sync): unchanged → no render
  const setDraft = useCallback(
    (path: string, draft: string) =>
      setFiles((prev) => {
        const at = prev.findIndex((f) => f.path === path);
        if (at < 0 || prev[at].draft === draft) return prev;
        const next = prev.slice();
        next[at] = { ...prev[at], draft };
        return next;
      }),
    [],
  );

  const togglePreview = useCallback((path: string) => patch(path, (f) => ({ preview: !f.preview })), [patch]);

  /** Re-read from disk, dropping the draft. */
  const reload = useCallback(
    (path: string) => {
      patch(path, (f) => ({ loading: true, conflict: null, changedOnDisk: false, draft: f.content }));
      void read(path, false);
    },
    [patch, read],
  );

  /** After an agent turn: clean tabs reload silently, dirty ones learn the disk changed. */
  const refreshAll = useCallback(() => {
    for (const f of filesRef.current) {
      if (!f.loading && !f.saving) void read(f.path, true);
    }
  }, [read]);

  const save = useCallback(
    async (path: string, overwrite = false): Promise<SaveResult> => {
      const file = filesRef.current.find((f) => f.path === path);
      if (!file || file.binary || file.saving || inFlight.current.has(path)) return "failed";
      // ⌘S on a tab that is loading, failed to load or gone from disk must not write
      // its empty buffer over (or back into) the file
      if (file.loading || file.error !== null || (!overwrite && !isDirty(file))) return "unchanged";
      const content = file.draft;
      const expected = overwrite ? (file.conflict?.sha256 ?? undefined) : file.sha256;
      inFlight.current.add(path);
      patch(path, { saving: true });
      try {
        const res = await codingApi.writeFile(projectId, { path, content, expected_sha256: expected });
        patch(path, (f) => ({
          saving: false,
          content,
          sha256: res.sha256,
          conflict: null,
          changedOnDisk: false,
          // keep typing done while the save was in flight
          draft: f.draft,
        }));
        return "saved";
      } catch (err) {
        if (err instanceof ApiError && err.code === "coding.file_conflict") {
          const sha = (err.detail as { sha256?: string } | null)?.sha256 ?? null;
          patch(path, { saving: false, conflict: { sha256: sha } });
          return "conflict";
        }
        patch(path, { saving: false });
        throw err;
      } finally {
        inFlight.current.delete(path);
      }
    },
    [projectId, patch],
  );

  return { files, open, close, setDraft, togglePreview, reload, refreshAll, save };
}
