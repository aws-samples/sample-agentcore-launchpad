import type { SessionFiles } from "../../../lib/api";

/** What the Files tab shows: one read of the session's output folder. */
export interface FilesState {
  data: SessionFiles | null;
  /** a failed read, as text (never the owner-only case) */
  error: string | null;
  /** 404 `chat.session_not_found`: the session exists but is not the caller's */
  forbidden: boolean;
  loading: boolean;
}

export const FILES_IDLE: FilesState = { data: null, error: null, forbidden: false, loading: false };
