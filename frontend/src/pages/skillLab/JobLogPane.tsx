import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { api } from "../../lib/api";

/**
 * Raw CLI log tail for a Skill Lab job.
 *
 * The backend serves the log by byte offset (`{content, next_offset}`), so this
 * appends chunks instead of re-reading the file — a training log runs to tens of
 * MB. LaunchSequence cannot be reused: it renders the deployer's JSONL events,
 * ours is unstructured subprocess output.
 */
export function JobLogPane({
  jobId,
  live,
  testId = "skill-lab-job-log",
}: {
  jobId: string;
  live: boolean;
  testId?: string;
}) {
  const { t } = useTranslation();
  const [text, setText] = useState("");
  const offset = useRef(0);
  const box = useRef<HTMLDivElement>(null);

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
    const timer = setInterval(() => void tick(), 2500);
    return () => {
      stale = true;
      clearInterval(timer);
    };
  }, [jobId, live]);

  // Follow the tail only while the job is live; a finished log stays where the
  // reader put it.
  useEffect(() => {
    if (live && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [text, live]);

  return (
    <div
      ref={box}
      className="code"
      data-testid={testId}
      style={{
        maxHeight: 320,
        overflowY: "auto",
        whiteSpace: "pre-wrap",
        overflowWrap: "anywhere",
        overflowX: "hidden",
        fontSize: 10.5,
      }}
    >
      {text || <span className="cm">{t("skillLab.eval.log.waiting")}</span>}
    </div>
  );
}
