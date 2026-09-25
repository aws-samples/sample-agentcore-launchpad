import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../../../lib/api";
import { JOB_POLL_MS } from "../../../lib/skillLab";

/**
 * Raw CLI log tail of a Skill Lab job. The backend serves the log by byte
 * offset (`{content, next_offset, eof}`), so chunks are appended instead of
 * re-reading the file — a training log runs to tens of MB.
 */
export function JobLog({ jobId, live, testId = "v2-skilllab-log" }: { jobId: string; live: boolean; testId?: string }) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const offset = useRef(0);
  const box = useRef<HTMLPreElement>(null);

  // Reset before the fetch effect below runs (declaration order), so a job
  // switch never appends the new log onto the previous job's bytes.
  useEffect(() => {
    offset.current = 0;
    setText("");
  }, [jobId]);

  useEffect(() => {
    let stale = false;
    const tick = async () => {
      try {
        // The server caps each chunk, so one tick may need several reads to
        // reach the end of an already-long log (a finished job polls once).
        for (let guard = 0; guard < 40; guard += 1) {
          const chunk = await api.skillLabJobLog(jobId, offset.current);
          if (stale) return;
          offset.current = chunk.next_offset;
          if (chunk.content) setText((prev) => prev + chunk.content);
          if (chunk.eof) return;
        }
      } catch {
        /* transient — the next tick retries, and a dead job stops polling anyway */
      }
    };
    void tick();
    if (!live) {
      return () => {
        stale = true;
      };
    }
    const timer = window.setInterval(() => void tick(), JOB_POLL_MS);
    return () => {
      stale = true;
      window.clearInterval(timer);
    };
  }, [jobId, live]);

  // Follow the tail only while the job is live; a finished log stays where the reader put it.
  useEffect(() => {
    if (live && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [text, live]);

  return (
    <pre ref={box} className="v2-pre v2-skilllab-log" data-testid={testId}>
      {text || <span className="v2-muted">{t("skillLab.eval.log.waiting")}</span>}
    </pre>
  );
}
