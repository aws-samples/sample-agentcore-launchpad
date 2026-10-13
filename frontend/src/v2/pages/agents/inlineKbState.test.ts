import { describe, expect, it } from "vitest";

import type { DataSource, IngestionJob, KnowledgeBaseDetail } from "../../../lib/knowledgeBases";
import {
  blocksSubmit,
  created,
  displayPhase,
  type InlineKb,
  inlineKbPhase,
  mountKb,
  PHASE_TONE,
  phaseInFlight,
  phaseLabelKey,
  refreshed,
  submitBlock,
  syncTarget,
  unmountKb,
  uploadFailed,
  uploadSucceeded,
} from "./inlineKbState";

const job = (status: string): IngestionJob => ({ job_id: `j-${status}`, status });

const source = (status: string, jobs?: IngestionJob[]): DataSource => ({
  ds_id: "ds1",
  name: "upload",
  status,
  bucket: "artifacts",
  ingestion_jobs: jobs,
});

function detail(status: string, sources: DataSource[] = [], kbId = "KB1"): KnowledgeBaseDetail {
  return {
    kb_id: kbId,
    name: "sales-kb",
    description: "",
    status,
    updated_at: null,
    data_sources: sources,
    attached_agents: [],
  };
}

// a File stand-in: the state module only stores it
const FILES = [{ name: "a.pdf", size: 10 } as unknown as File];

const mounted = (d: KnowledgeBaseDetail | null): InlineKb => ({ ...uploadSucceeded(created("sales-kb", FILES, detail("CREATING"))), detail: d });

describe("inlineKbPhase", () => {
  it("follows a KB from creation to ready", () => {
    expect(inlineKbPhase(null)).toBe("unknown");
    expect(inlineKbPhase(detail("CREATING"))).toBe("creating");
    expect(inlineKbPhase(detail("ACTIVE"))).toBe("sourcePending");
    expect(inlineKbPhase(detail("ACTIVE", [source("CREATING")]))).toBe("sourcePending");
    expect(inlineKbPhase(detail("ACTIVE", [source("AVAILABLE")]))).toBe("starting");
    expect(inlineKbPhase(detail("ACTIVE", [source("AVAILABLE", [])]))).toBe("starting");
    expect(inlineKbPhase(detail("ACTIVE", [source("AVAILABLE", [job("IN_PROGRESS")])]))).toBe("indexing");
    expect(inlineKbPhase(detail("ACTIVE", [source("available", [job("complete")])]))).toBe("ready");
  });

  it("reads the newest ingestion job only", () => {
    expect(inlineKbPhase(detail("ACTIVE", [source("AVAILABLE", [job("FAILED"), job("COMPLETE")])]))).toBe("ingestFailed");
    expect(inlineKbPhase(detail("ACTIVE", [source("AVAILABLE", [job("COMPLETE"), job("FAILED")])]))).toBe("ready");
  });

  it("treats FAILED and DELETING as a failed KB", () => {
    expect(inlineKbPhase(detail("FAILED"))).toBe("failed");
    expect(inlineKbPhase(detail("deleting"))).toBe("failed");
  });

  it("polls only while the phase still moves on its own", () => {
    for (const p of ["creating", "sourcePending", "starting", "indexing", "unknown"] as const) expect(phaseInFlight(p)).toBe(true);
    for (const p of ["ready", "ingestFailed", "failed"] as const) expect(phaseInFlight(p)).toBe(false);
  });
});

describe("status label", () => {
  it("shows 'checking' until the files are uploaded", () => {
    const rec = created("sales-kb", FILES, detail("ACTIVE", [source("AVAILABLE", [job("COMPLETE")])]));
    expect(displayPhase(rec)).toBe("unknown");
    expect(phaseLabelKey(rec)).toBe("v2.agents.wizard.inlineKb.state.unknown");
    expect(displayPhase(uploadSucceeded(rec))).toBe("ready");
    expect(phaseLabelKey(uploadSucceeded(rec))).toBe("v2.agents.wizard.inlineKb.state.ready");
  });

  it("has a tone for every phase", () => {
    expect(PHASE_TONE.ready).toBe("green");
    expect(PHASE_TONE.failed).toBe("red");
    expect(PHASE_TONE.ingestFailed).toBe("red");
    expect(PHASE_TONE.creating).toBe("blue");
  });
});

describe("transitions", () => {
  it("create keeps the files and the id, not yet uploaded", () => {
    const rec = created("sales-kb", FILES, detail("CREATING"));
    expect(rec).toMatchObject({ kb_id: "KB1", name: "sales-kb", uploaded: false, uploadError: null });
    expect(rec.files).toBe(FILES);
  });

  it("an upload failure keeps the record for a retry; a success clears the error", () => {
    const rec = created("sales-kb", FILES, detail("CREATING"));
    const bad = uploadFailed(rec, "boom");
    expect(bad).toMatchObject({ uploaded: false, uploadError: "boom" });
    expect(bad.files).toBe(FILES);
    expect(uploadSucceeded(bad)).toMatchObject({ uploaded: true, uploadError: null });
  });

  it("a poll updates only the record it was issued for", () => {
    const rec = mounted(detail("CREATING"));
    const fresh = detail("ACTIVE");
    expect(refreshed(rec, "KB1", fresh)?.detail).toBe(fresh);
    expect(refreshed(rec, "OTHER", fresh)).toBe(rec);
    expect(refreshed(null, "KB1", fresh)).toBeNull();
  });

  it("starts the first ingestion once, when the source is available with no job", () => {
    const none = new Set<string>();
    expect(syncTarget(detail("ACTIVE", [source("AVAILABLE")]), none)).toBe("ds1");
    expect(syncTarget(detail("ACTIVE", [source("AVAILABLE")]), new Set(["ds1"]))).toBeNull();
    expect(syncTarget(detail("ACTIVE", [source("CREATING")]), none)).toBeNull();
    expect(syncTarget(detail("CREATING"), none)).toBeNull();
    expect(syncTarget(detail("ACTIVE", [source("AVAILABLE", [job("FAILED")])]), none)).toBeNull();
  });

  it("mounts and unmounts like picking an existing KB", () => {
    expect(mountKb(["A"], "KB1")).toEqual(["A", "KB1"]);
    expect(mountKb(["A", "KB1"], "KB1")).toEqual(["A", "KB1"]);
    expect(unmountKb(["A", "KB1"], "KB1")).toEqual(["A"]);
    expect(unmountKb(["A"], "KB1")).toEqual(["A"]);
  });
});

describe("submitBlock", () => {
  it("never blocks without an inline KB", () => {
    expect(submitBlock(null, [], false)).toBeNull();
    expect(blocksSubmit(null, ["KB1"], false)).toBe(false);
  });

  it("blocks while the create / upload calls run", () => {
    expect(submitBlock(null, [], true)).toBe("busy");
    expect(blocksSubmit(mounted(detail("ACTIVE")), ["KB1"], true)).toBe(true);
  });

  it("waits until the mounted KB is ACTIVE", () => {
    expect(submitBlock(mounted(detail("CREATING")), ["KB1"], false)).toBe("waiting");
    expect(submitBlock(mounted(null), ["KB1"], false)).toBe("waiting");
    expect(submitBlock(mounted(detail("ACTIVE")), ["KB1"], false)).toBeNull();
  });

  it("does not wait for the data source or ingestion", () => {
    expect(submitBlock(mounted(detail("ACTIVE", [source("CREATING")])), ["KB1"], false)).toBeNull();
    expect(submitBlock(mounted(detail("ACTIVE", [source("AVAILABLE", [job("IN_PROGRESS")])])), ["KB1"], false)).toBeNull();
    expect(submitBlock(mounted(detail("ACTIVE", [source("AVAILABLE", [job("FAILED")])])), ["KB1"], false)).toBeNull();
  });

  it("blocks on a failed KB", () => {
    expect(submitBlock(mounted(detail("FAILED")), ["KB1"], false)).toBe("failed");
    expect(submitBlock(mounted(detail("DELETING")), ["KB1"], false)).toBe("failed");
  });

  it("ignores a KB that is not mounted (upload failed or unticked)", () => {
    const notUploaded = uploadFailed(created("sales-kb", FILES, detail("CREATING")), "boom");
    expect(submitBlock(notUploaded, [], false)).toBeNull();
    expect(submitBlock(mounted(detail("CREATING")), ["OTHER"], false)).toBeNull();
    expect(submitBlock(mounted(detail("FAILED")), [], false)).toBeNull();
  });
});
