import { useCallback, useEffect, useRef, useState } from "react";

import { api, ApiError, errorMessage, type Announcement, type AnnouncementContent } from "../../../lib/api";
import {
  type Action, type DraftSession, isDirty, NEW_ID, publishReason, sessionFor, trimmed,
} from "./common";

export type ActOutcome =
  | { ok: true; action: Action; row: Announcement | null; from: string }
  | { ok: false; action: Action; error: string };

/**
 * Editing buffers + every write, kept in the page router so they survive list ↔ editor
 * navigation (the classic page kept them in one component for the same reason).
 * `tick` bumps after each successful write so the list refetches.
 */
export function useAnnouncementSessions() {
  const [sessions, setSessions] = useState<Record<string, DraftSession>>({});
  const sessionsRef = useRef(sessions);
  sessionsRef.current = sessions;
  const [detailError, setDetailError] = useState<{ id: string; message: string } | null>(null);
  const [tick, setTick] = useState(0);
  const inFlight = useRef(new Set<string>());
  const mounted = useRef(true);
  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const hasUnsaved = Object.values(sessions).some((s) => isDirty(s) || s.busy);
  useEffect(() => {
    if (!hasUnsaved) return;
    const warn = (event: BeforeUnloadEvent) => {
      event.preventDefault();
      event.returnValue = "";
    };
    window.addEventListener("beforeunload", warn);
    return () => window.removeEventListener("beforeunload", warn);
  }, [hasUnsaved]);

  const update = useCallback((id: string, patch: Partial<DraftSession>) => {
    setSessions((prev) => (prev[id] ? { ...prev, [id]: { ...prev[id], ...patch } } : prev));
  }, []);

  /** Open a buffer; an already-loaded buffer is never rehydrated from the server. */
  const ensure = useCallback((id: string, signal: AbortSignal) => {
    if (sessionsRef.current[id]) return;
    if (id === NEW_ID) {
      setSessions((prev) => (prev[NEW_ID] ? prev : { ...prev, [NEW_ID]: sessionFor(null) }));
      return;
    }
    setDetailError(null);
    api
      .getAnnouncement(id, signal)
      .then((row) => {
        if (!signal.aborted) setSessions((prev) => (prev[id] ? prev : { ...prev, [id]: sessionFor(row) }));
      })
      .catch((err: unknown) => {
        if (!signal.aborted) setDetailError({ id, message: errorMessage(err) });
      });
  }, []);

  const setContent = useCallback(
    (id: string, content: AnnouncementContent) => update(id, { content }),
    [update],
  );

  /**
   * Run one write. `fallback` is the list row, used when the list acts on an announcement
   * that has no open buffer yet.
   */
  const act = useCallback(
    async (id: string, action: Action, fallback?: Announcement): Promise<ActOutcome | null> => {
      const hadBuffer = Boolean(sessionsRef.current[id]);
      const session = sessionsRef.current[id] ?? (fallback ? sessionFor(fallback) : undefined);
      if (!session || inFlight.current.has(id)) return null;
      if (action === "publish" && publishReason(session)) return null;
      if (action !== "save" && !session.row) return null;
      inFlight.current.add(id);
      setSessions((prev) => ({ ...prev, [id]: { ...(prev[id] ?? session), busy: action, error: null, errorCode: null } }));
      try {
        let row: Announcement;
        if (action === "save") {
          const content = trimmed(session.content);
          row = session.row
            ? await api.saveAnnouncement(id, content, session.row.revision)
            : await api.createAnnouncement(content);
        } else {
          const revision = session.row!.revision;
          if (action === "delete") {
            await api.deleteAnnouncement(id, revision);
            if (!mounted.current) return null;
            setSessions((prev) => {
              const next = { ...prev };
              delete next[id];
              return next;
            });
            setTick((v) => v + 1);
            return { ok: true, action, row: null, from: id };
          }
          row =
            action === "publish"
              ? await api.publishAnnouncement(id, revision)
              : action === "unpublish"
                ? await api.unpublishAnnouncement(id, revision)
                : await api.getAnnouncement(id);
        }
        if (!mounted.current) return null;
        setSessions((prev) => {
          const next = { ...prev };
          const latest = prev[id] ?? session;
          if (id === NEW_ID) delete next[NEW_ID];
          // A list-row action leaves no buffer behind: the editor loads fresh on open.
          if (!hadBuffer) {
            delete next[id];
            return next;
          }
          next[row.id] = {
            ...sessionFor(row),
            // Withdrawing changes publication only; keep any unsaved draft edits.
            content: action === "unpublish" ? latest.content : row.content,
          };
          return next;
        });
        setTick((v) => v + 1);
        return { ok: true, action, row, from: id };
      } catch (err: unknown) {
        const message = errorMessage(err);
        if (mounted.current && hadBuffer) {
          update(id, { busy: null, error: message, errorCode: err instanceof ApiError ? err.code : null });
        } else if (mounted.current) {
          setSessions((prev) => {
            const next = { ...prev };
            delete next[id];
            return next;
          });
        }
        return { ok: false, action, error: message };
      } finally {
        inFlight.current.delete(id);
      }
    },
    [update],
  );

  return { sessions, detailError, tick, ensure, setContent, act, refresh: () => setTick((v) => v + 1) };
}

export type AnnouncementSessions = ReturnType<typeof useAnnouncementSessions>;
