import { useCallback, useEffect, useRef, useState } from "react";

import {
  api,
  ApiError,
  errorMessage,
  type InspectedSkill,
  type RegistryRecordOut,
  type SkillLabJobBody,
  type SkillLabJobInfo,
} from "../../../lib/api";
import { isLiveJob, JOB_POLL_MS, LIST_POLL_MS } from "../../../lib/skillLab";
import { useLoad } from "../../hooks";

/* Skill Lab V2 view state and data hooks (the non-component exports). */

export type Tab = "tasksets" | "taskgen" | "eval" | "train";
export const TABS: Tab[] = ["tasksets", "taskgen", "eval", "train"];

export const JOB_STATUSES = ["queued", "running", "succeeded", "failed", "cancelled", "interrupted"] as const;

/**
 * Pass rates / best scores are only known after reading a job's own result
 * file, which the lists deliberately do not do (one file read per row); values
 * seen while browsing details are remembered for the session so the list
 * column fills in as the user clicks around.
 */
export const passRateCache = new Map<string, number>();
export const bestScoreCache = new Map<string, number>();

/** A job list of one type, refreshed every 8 s (jobs finish on their own). */
export function useJobList(type: "eval" | "train" | "taskgen") {
  const [rows, setRows] = useState<SkillLabJobInfo[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const reload = useCallback(async () => {
    try {
      setRows(await api.skillLabJobs(type));
      setError(null);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setLoading(false);
    }
  }, [type]);
  useEffect(() => {
    void reload();
    const timer = window.setInterval(() => void reload(), LIST_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reload]);
  return { rows, loading, error, reload };
}

/**
 * One job: fetched once, then polled every 2.5 s while it is live. `missing`
 * is set only when the backend answered "no such job", distinguishing a dead
 * deep link from a fetch that has not landed yet.
 */
export function useJob(id: string) {
  const [job, setJob] = useState<SkillLabJobInfo | null>(null);
  const [missing, setMissing] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const liveRef = useRef(false);
  liveRef.current = job !== null && isLiveJob(job);

  useEffect(() => {
    let stale = false;
    const fetchJob = async () => {
      try {
        const next = await api.skillLabJobGet(id);
        if (stale) return;
        setJob(next);
        setMissing(false);
        setError(null);
      } catch (err) {
        if (stale) return;
        if (err instanceof ApiError && err.code === "skill_lab.job_not_found") setMissing(true);
        else setError(errorMessage(err));
      }
    };
    void fetchJob();
    const timer = window.setInterval(() => {
      if (liveRef.current) void fetchJob();
    }, JOB_POLL_MS);
    return () => {
      stale = true;
      window.clearInterval(timer);
    };
  }, [id]);

  return { job, setJob, missing, error };
}

export function useTasksets(key = "skill-lab-tasksets") {
  return useLoad(() => api.skillLabTasksets(), key);
}

/** Workspace AGENT_SKILLS records, any status; a failed read is an empty list. */
export function useSkillRecords() {
  return useLoad(
    () =>
      api
        .v2SkillLabSkillRecords()
        .then((body) => body.records)
        .catch(() => [] as RegistryRecordOut[]),
    "skill-lab-skill-records",
  );
}

/** Where a generation lands: a new set, or `set → split` for an expansion. */
export function taskgenTarget(job: SkillLabJobInfo, newLabel: string) {
  return job.taskset_id ? `${job.taskset_name} → ${job.split}` : newLabel;
}

/** Skill source of an eval / train wizard. */
export interface SourceState {
  tab: "registry" | "upload";
  recordId: string;
  staging: { id: string; skills: InspectedSkill[] } | null;
  stagedIndex: number | null;
}

export const initialSource = (recordId: string | null): SourceState => ({
  tab: "registry",
  recordId: recordId ?? "",
  staging: null,
  stagedIndex: null,
});

/** The job body's `skill_source`, or null while nothing valid is picked. */
export function skillSourceOf(src: SourceState): SkillLabJobBody["skill_source"] | null {
  if (src.tab === "registry") return src.recordId ? { kind: "registry", record_id: src.recordId } : null;
  if (src.staging && src.stagedIndex !== null)
    return { kind: "upload", staging_id: src.staging.id, index: src.stagedIndex };
  return null;
}
