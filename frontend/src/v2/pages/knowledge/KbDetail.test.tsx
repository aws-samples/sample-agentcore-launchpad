// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../../auth/auth-context";
import type { KnowledgeBaseDetail, UploadFilesResult } from "../../../lib/knowledgeBases";
import { uploadOutcomeKey } from "../../../lib/knowledgeBases";
import en from "../../../locales/en/common.json";
import { KbDetail } from "./KbDetail";

/**
 * N17-obs204 (cn/r17-aa): the detail page's automatic first sync used to fire as soon as a
 * data source was AVAILABLE with no jobs — before the files had landed — while the page's
 * own upload started no sync. Now the auto first sync needs `has_objects === true`, and
 * the upload reads the sync the SERVER started from the response, toasts its outcome and
 * polls that job.
 */

const get = vi.fn<() => Promise<KnowledgeBaseDetail>>();
const sync = vi.fn<(id: string, dsId: string, opts?: { full?: boolean }) => Promise<{ job_id: string; status: string }>>(
  async () => ({ job_id: "ij-manual", status: "STARTING" }),
);
const uploadFiles = vi.fn<() => Promise<UploadFilesResult>>();
const addSource = vi.fn<(id: string, body: unknown) => Promise<Record<string, unknown>>>();
const query = vi.fn<() => Promise<{ results: unknown[] }>>(async () => ({ results: [] }));
const toasts: string[] = [];

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: { ...actual.api, listAgents: vi.fn(async () => ({ agents: [] })) },
    v2KnowledgeApi: {
      ...actual.v2KnowledgeApi,
      get: () => get(),
      // the options are forwarded only when given, so `toHaveBeenCalledWith("KB1", "DS1")` holds
      sync: (id: string, dsId: string, opts?: { full?: boolean }) => (opts ? sync(id, dsId, opts) : sync(id, dsId)),
      uploadFiles: () => uploadFiles(),
      addSource: (id: string, body: unknown) => addSource(id, body),
      query: () => query(),
    },
  };
});
vi.mock("../../hooks", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../hooks")>()),
  useV2Toast: () => (_tone: string, text: string) => {
    toasts.push(text);
  },
}));

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

beforeEach(() => {
  get.mockReset();
  sync.mockClear();
  uploadFiles.mockReset();
  addSource.mockReset();
  query.mockClear();
  toasts.length = 0;
});
afterEach(cleanup);

function detail(hasObjects: boolean | null, jobs: KnowledgeBaseDetail["data_sources"][number]["ingestion_jobs"] = []): KnowledgeBaseDetail {
  return {
    kb_id: "KB1",
    name: "docs",
    description: "",
    status: "ACTIVE",
    created_at: "2026-10-21T00:00:00Z",
    updated_at: "2026-10-21T00:00:00Z",
    failure_reasons: [],
    data_sources: [
      {
        ds_id: "DS1",
        name: "DS1",
        mode: "upload",
        status: "AVAILABLE",
        bucket: "bkt",
        prefix: "kb/KB1/DS1/",
        ingestion_jobs: jobs,
        jobs_degraded: false,
        has_objects: hasObjects,
      } as KnowledgeBaseDetail["data_sources"][number],
    ],
    attached_agents: [],
  };
}

function renderDetail() {
  return render(
    <MemoryRouter initialEntries={["/v2/knowledge-bases?view=detail&id=KB1"]}>
      <KbDetail id="KB1" onGone={() => undefined} />
    </MemoryRouter>,
  );
}

describe("KbDetail auto first sync (N17-obs204)", () => {
  it("does NOT start a sync for an AVAILABLE source whose prefix holds no object", async () => {
    get.mockResolvedValue(detail(false));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-basic")).toBeTruthy());
    await waitFor(() => expect(get).toHaveBeenCalled());
    // give the poll loop a turn: still nothing
    await new Promise((r) => setTimeout(r, 20));
    expect(sync).not.toHaveBeenCalled();
  });

  it("does NOT start a sync when the server could not tell (has_objects null)", async () => {
    get.mockResolvedValue(detail(null));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-basic")).toBeTruthy());
    await new Promise((r) => setTimeout(r, 20));
    expect(sync).not.toHaveBeenCalled();
  });

  it("starts exactly one sync for an AVAILABLE source that holds objects and has no job yet", async () => {
    get.mockResolvedValue(detail(true));
    renderDetail();
    await waitFor(() => expect(sync).toHaveBeenCalledTimes(1));
    expect(sync).toHaveBeenCalledWith("KB1", "DS1");
    expect(toasts).toContain(en.knowledge.detail.sources.syncStarted);
  });

  it("leaves a source alone once a job exists, whatever has_objects says", async () => {
    get.mockResolvedValue(detail(true, [{ job_id: "ij-old", status: "COMPLETE" }]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-basic")).toBeTruthy());
    await new Promise((r) => setTimeout(r, 20));
    expect(sync).not.toHaveBeenCalled();
  });
});

describe("KbDetail upload (N17-obs204)", () => {
  async function uploadOne(result: UploadFilesResult) {
    get.mockResolvedValue(detail(false));
    uploadFiles.mockResolvedValue(result);
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-basic")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-add"));
    const input = screen.getByTestId("v2-kb-file-input") as HTMLInputElement;
    const file = new File([new Uint8Array([104, 105])], "a.txt", { type: "text/plain" });
    fireEvent.change(input, { target: { files: [file] } });
    await waitFor(() => expect(screen.getByTestId("v2-kb-files")).toBeTruthy());
    const callsBefore = get.mock.calls.length;
    fireEvent.click(screen.getByTestId("v2-kb-add-submit"));
    await waitFor(() => expect(uploadFiles).toHaveBeenCalledTimes(1));
    // the page re-reads the detail (polls the job the upload reported) …
    await waitFor(() => expect(get.mock.calls.length).toBeGreaterThan(callsBefore));
    // … and never starts a second sync of its own
    expect(sync).not.toHaveBeenCalled();
  }

  it("reads the job the server started and toasts that indexing began", async () => {
    await uploadOne({
      keys: ["kb/KB1/DS1/a.txt"],
      job: { job_id: "ij-fresh", status: "STARTING", coalesced: false, covers_upload: true },
    });
    expect(toasts).toEqual(["Uploaded 1 file(s), indexing started."]);
  });

  it("tells the user to sync by hand when the coalesced job predates the upload", async () => {
    await uploadOne({
      keys: ["kb/KB1/DS1/a.txt"],
      job: { job_id: "ij-earlier", status: "IN_PROGRESS", coalesced: true, covers_upload: false },
    });
    expect(toasts).toEqual(["Uploaded 1 file(s) — the running job may not include them; sync manually."]);
  });

  it("tells the user to sync by hand when the server could not start one", async () => {
    await uploadOne({ keys: ["kb/KB1/DS1/a.txt"], job: null });
    expect(toasts).toEqual(["Uploaded 1 file(s) — the sync could not be started; sync manually."]);
  });
});

describe("KbDetail upload recreates a missing upload source (N17-obs242)", () => {
  const created: KnowledgeBaseDetail["data_sources"][number] = {
    ds_id: "DS2",
    name: "DS2",
    mode: "upload",
    status: "AVAILABLE",
    bucket: "bkt",
    prefix: "kb/KB1/DS2/",
    ingestion_jobs: [],
    jobs_degraded: false,
    has_objects: false,
  };

  it("lists the source the upload created, with its first job, once the detail is refetched", async () => {
    // the user's KB: its only upload source was removed, so the row changed after creation
    const empty: KnowledgeBaseDetail = { ...detail(false), data_sources: [], updated_at: "2026-10-21T08:00:00Z" };
    const job = { job_id: "ij-new", status: "STARTING", coalesced: false, covers_upload: true };
    get.mockResolvedValue(empty);
    uploadFiles.mockImplementation(async () => {
      // from now on the server's detail carries the new source and the upload's job
      get.mockResolvedValue({
        ...empty,
        data_sources: [
          {
            ...created,
            has_objects: true,
            ingestion_jobs: [{ job_id: "ij-new", status: "STARTING", started_at: "2026-10-21T09:00:00Z" }],
          },
        ],
      });
      return { keys: ["kb/KB1/DS2/a.txt"], job, data_source: created };
    });
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-no-source")).toBeTruthy());
    expect(screen.queryByTestId("v2-kb-repair")).toBeNull();
    expect(screen.getByTestId("v2-kb-sources-table").textContent).not.toContain("DS2");
    fireEvent.click(screen.getByTestId("v2-kb-add"));
    const input = screen.getByTestId("v2-kb-file-input") as HTMLInputElement;
    fireEvent.change(input, {
      target: { files: [new File([new Uint8Array([104, 105])], "a.txt", { type: "text/plain" })] },
    });
    await waitFor(() => expect(screen.getByTestId("v2-kb-files")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-add-submit"));
    await waitFor(() => expect(uploadFiles).toHaveBeenCalledTimes(1));
    // the refetched detail shows the new row with its STARTING job; the warning is gone
    await waitFor(() => expect(screen.getByTestId("v2-kb-sources-table").textContent).toContain("DS2"));
    expect(screen.getByTestId("v2-kb-sources-table").textContent).toContain("s3://bkt/kb/KB1/DS2/");
    // N17-obs456: the job status reads through v2.awsStatus (en: "Starting")
    expect(screen.getByTestId("v2-kb-sources-table").textContent).toContain("Starting");
    expect(screen.queryByTestId("v2-kb-no-source")).toBeNull();
    expect(toasts).toEqual(["Data source created automatically.", "Uploaded 1 file(s), indexing started."]);
    // the page starts no sync of its own — the upload's job is the first sync
    expect(sync).not.toHaveBeenCalled();
  });

  it("says nothing about a created source when the KB already had one", async () => {
    get.mockResolvedValue(detail(false));
    uploadFiles.mockResolvedValue({
      keys: ["kb/KB1/DS1/a.txt"],
      job: { job_id: "ij-fresh", status: "STARTING", coalesced: false, covers_upload: true },
    });
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-basic")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-add"));
    fireEvent.change(screen.getByTestId("v2-kb-file-input"), {
      target: { files: [new File([new Uint8Array([104, 105])], "a.txt", { type: "text/plain" })] },
    });
    await waitFor(() => expect(screen.getByTestId("v2-kb-files")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-add-submit"));
    await waitFor(() => expect(uploadFiles).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(toasts.length).toBe(1));
    expect(toasts).toEqual(["Uploaded 1 file(s), indexing started."]);
  });

  it("tells the user, before removing an upload source, that the next upload creates a fresh one", async () => {
    get.mockResolvedValue(detail(false));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-ds-remove")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-ds-remove"));
    const body = screen.getByTestId("v2-confirm").textContent ?? "";
    expect(body).toContain("Remove “DS1” from this knowledge base?");
    expect(screen.getByTestId("v2-kb-ds-remove-note").textContent).toBe(
      "The next upload to this knowledge base creates a fresh upload data source (a new prefix) automatically.",
    );
  });

  it("keeps the plain confirmation for an existing-mode source", async () => {
    const d = detail(false);
    d.data_sources[0] = { ...d.data_sources[0], mode: "existing", name: "manual", prefix: "kb/KB1/manual/" };
    get.mockResolvedValue(d);
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-ds-remove")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-ds-remove"));
    const body = screen.getByTestId("v2-confirm").textContent ?? "";
    expect(body).toContain("Remove “manual” from this knowledge base?");
    expect(screen.queryByTestId("v2-kb-ds-remove-note")).toBeNull();
  });
});

describe("uploadOutcomeKey", () => {
  it("maps the four outcomes onto the three messages", () => {
    expect(uploadOutcomeKey(null)).toBe("knowledge.detail.sources.uploadedSyncUnavailable");
    expect(uploadOutcomeKey({ job_id: "a", status: "STARTING", coalesced: false, covers_upload: true }))
      .toBe("knowledge.detail.sources.uploadedIndexing");
    expect(uploadOutcomeKey({ job_id: "a", status: "STARTING", coalesced: true, covers_upload: true }))
      .toBe("knowledge.detail.sources.uploadedSyncRunning");
    expect(uploadOutcomeKey({ job_id: "a", status: "IN_PROGRESS", coalesced: true, covers_upload: false }))
      .toBe("knowledge.detail.sources.uploadedSyncManual");
  });
});

/**
 * N17-obs243 / N17-obs244 (cn/r17-al), live 2026-10-22 on KB `test`: the repair banner
 * showed on a KB whose upload source the user had removed, seven clicks created seven empty
 * sources, and the seven COMPLETE-but-empty syncs plus an empty retrieval read as broken
 * ingestion.
 */
describe("KbDetail without a data source (N17-obs243)", () => {
  const noSources = (updated: string): KnowledgeBaseDetail => ({
    ...detail(false),
    data_sources: [],
    updated_at: updated,
  });

  it("offers the repair only while the KB never had a source (creation window)", async () => {
    get.mockResolvedValue(noSources("2026-10-21T00:00:00Z"));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-missing-source")).toBeTruthy());
    expect(screen.getByTestId("v2-kb-repair")).toBeTruthy();
    expect(screen.getByTestId("v2-kb-missing-source").textContent).toContain(
      "clicking twice cannot create a duplicate",
    );
    expect(screen.queryByTestId("v2-kb-no-source")).toBeNull();
  });

  it("shows a neutral upload pointer, without a repair button, once the last source was removed", async () => {
    get.mockResolvedValue(noSources("2026-10-22T02:40:00Z"));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-no-source")).toBeTruthy());
    expect(screen.getByTestId("v2-kb-no-source").textContent).toBe(
      "This knowledge base has no data source yet. Use Add data source → Upload files; an upload source is created automatically.",
    );
    expect(screen.queryByTestId("v2-kb-repair")).toBeNull();
    expect(screen.queryByTestId("v2-kb-missing-source")).toBeNull();
  });

  it("keeps the repair button disabled from the click until the refetched detail lands", async () => {
    get.mockResolvedValue(noSources("2026-10-21T00:00:00Z"));
    let landDetail: (d: KnowledgeBaseDetail) => void = () => undefined;
    addSource.mockImplementation(async () => {
      // the next detail read hangs until the test lets it land
      get.mockImplementation(() => new Promise<KnowledgeBaseDetail>((resolve) => (landDetail = resolve)));
      return { ...detail(false).data_sources[0], created: true };
    });
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-repair")).toBeTruthy());
    const button = screen.getByTestId("v2-kb-repair") as HTMLButtonElement;
    fireEvent.click(button);
    fireEvent.click(button); // a second click in the same tick is swallowed too
    await waitFor(() => expect(addSource).toHaveBeenCalledTimes(1));
    await waitFor(() => expect(toasts).toContain("Data source created automatically."));
    // the POST answered, the refresh has not landed: still disabled
    expect((screen.getByTestId("v2-kb-repair") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByTestId("v2-kb-repair"));
    expect(addSource).toHaveBeenCalledTimes(1);
    get.mockResolvedValue(detail(false));
    landDetail(detail(false));
    await waitFor(() => expect(screen.queryByTestId("v2-kb-missing-source")).toBeNull());
    expect(addSource).toHaveBeenCalledWith("KB1", { mode: "upload" });
  });

  it("does not claim a creation when the server returned the existing source", async () => {
    get.mockResolvedValue(noSources("2026-10-21T00:00:00Z"));
    addSource.mockImplementation(async () => {
      get.mockResolvedValue(detail(false));
      return { ...detail(false).data_sources[0], created: false };
    });
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-repair")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-repair"));
    await waitFor(() => expect(screen.queryByTestId("v2-kb-missing-source")).toBeNull());
    expect(toasts).not.toContain("Data source created automatically.");
  });
});

describe("KbDetail empty-ingestion hints (N17-obs244)", () => {
  const emptyJob = {
    job_id: "ij-empty",
    status: "COMPLETE",
    started_at: "2026-10-22T02:45:09Z",
    statistics: { numberOfDocumentsScanned: 0, numberOfNewDocumentsIndexed: 0, numberOfDocumentsFailed: 0, prefix_empty: true },
  };

  it("marks a COMPLETE job over an empty prefix in the sources table", async () => {
    get.mockResolvedValue(detail(false, [emptyJob]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-last-sync-empty")).toBeTruthy());
    expect(screen.getByTestId("v2-kb-last-sync-empty").textContent).toBe("empty prefix · no files");
  });

  it("marks a COMPLETE job that scanned 0 even without the prefix_empty flag", async () => {
    const counts: Record<string, number> = { numberOfDocumentsScanned: 0, numberOfNewDocumentsIndexed: 0 };
    get.mockResolvedValue(detail(null, [{ ...emptyJob, statistics: counts }]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-last-sync-empty")).toBeTruthy());
  });

  it("leaves a job that ingested files unmarked", async () => {
    const full = { ...emptyJob, statistics: { numberOfDocumentsScanned: 2, numberOfNewDocumentsIndexed: 2, numberOfDocumentsFailed: 0 } };
    get.mockResolvedValue(detail(true, [full]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-sources-table").textContent).toContain("Complete"));
    expect(screen.queryByTestId("v2-kb-last-sync-empty")).toBeNull();
  });

  async function emptyRetrieval(d: KnowledgeBaseDetail): Promise<string> {
    get.mockResolvedValue(d);
    render(
      <MemoryRouter initialEntries={["/v2/knowledge-bases?view=detail&id=KB1&tab=retrieve"]}>
        <KbDetail id="KB1" onGone={() => undefined} />
      </MemoryRouter>,
    );
    await waitFor(() => expect(screen.getByTestId("v2-kb-query")).toBeTruthy());
    fireEvent.change(screen.getByTestId("v2-kb-query"), { target: { value: "leave policy" } });
    fireEvent.click(screen.getByTestId("v2-kb-search-btn"));
    await waitFor(() => expect(screen.getByTestId("v2-kb-retrieve-empty")).toBeTruthy());
    return screen.getByTestId("v2-kb-retrieve-empty").textContent ?? "";
  }

  it("says to upload first when no data source holds a file", async () => {
    const d = detail(false, [emptyJob]);
    d.data_sources.push({ ...d.data_sources[0], ds_id: "DS2", name: "DS2", has_objects: null });
    expect(await emptyRetrieval(d)).toBe(
      "This knowledge base holds no files yet — upload files under Data sources and wait for ingestion to finish.",
    );
  });

  it("keeps today's copy when some source holds files", async () => {
    const d = detail(false, [emptyJob]);
    d.data_sources.push({ ...d.data_sources[0], ds_id: "DS2", name: "DS2", has_objects: true });
    expect(await emptyRetrieval(d)).toBe(
      "No results — has ingestion completed? Check the data source status and ingestion jobs above.",
    );
  });
});

describe("KbDetail ingestion statistics labels (N17-obs271)", () => {
  const job = {
    job_id: "ij-inc",
    status: "COMPLETE",
    started_at: "2026-10-22T03:00:00Z",
    statistics: {
      numberOfDocumentsFailed: 0,
      numberOfDocumentsUnchanged: 3,
      numberOfDocumentsScanned: 4,
      numberOfNewDocumentsIndexed: 1,
      numberOfDocumentsDeleted: 1,
    },
  };

  it("summarises the latest ingestion with the console's labels, in display order", async () => {
    get.mockResolvedValue(detail(true, [job]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-last-sync-summary")).toBeTruthy());
    expect(screen.getByTestId("v2-kb-last-sync-summary").textContent).toBe(
      "Scanned 4 · Newly indexed 1 · Unchanged 3 · Failed 0 · Removed 1",
    );
  });

  it("labels Unchanged / Removed in the ingestion-jobs table instead of raw keys", async () => {
    get.mockResolvedValue(detail(true, [job]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-source-panel")).toBeTruthy());
    fireEvent.click(screen.getByRole("radio", { name: /Ingestion jobs/ }));
    await waitFor(() => expect(screen.getByTestId("v2-kb-job-stat-numberOfDocumentsUnchanged")).toBeTruthy());
    const unchanged = screen.getByTestId("v2-kb-job-stat-numberOfDocumentsUnchanged");
    expect(unchanged.textContent).toBe("Unchanged3");
    expect(unchanged.getAttribute("title")).toContain("not re-embedded");
    expect(screen.getByTestId("v2-kb-job-stat-numberOfDocumentsDeleted").textContent).toBe("Removed1");
    const table = screen.getByTestId("v2-kb-jobs").textContent ?? "";
    expect(table).not.toContain("Documents Unchanged");
    expect(table).not.toContain("numberOf");
  });
});

describe("KbDetail full rebuild (N17-obs272)", () => {
  const done = { job_id: "ij-done", status: "COMPLETE", started_at: "2026-10-22T03:00:00Z", statistics: { numberOfDocumentsScanned: 1 } };

  it("asks first, then POSTs the sync with {full: true}", async () => {
    get.mockResolvedValue(detail(true, [done]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-full-rebuild")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-full-rebuild"));
    await waitFor(() => expect(screen.getByTestId("v2-kb-full-rebuild-body")).toBeTruthy());
    expect(screen.getByTestId("v2-kb-full-rebuild-body").textContent).toBe(
      "A full rebuild re-chunks and re-embeds every document, which takes longer; use it only when retrieval results look wrong.",
    );
    expect(sync).not.toHaveBeenCalled(); // nothing before the confirmation
    fireEvent.click(screen.getByTestId("v2-confirm-ok"));
    await waitFor(() => expect(sync).toHaveBeenCalledWith("KB1", "DS1", { full: true }));
    expect(sync).toHaveBeenCalledTimes(1);
    await waitFor(() => expect(toasts).toContain("Full rebuild started."));
    await waitFor(() => expect(screen.queryByTestId("v2-kb-full-rebuild-body")).toBeNull());
  });

  it("does nothing when the dialog is cancelled, and the plain sync stays incremental", async () => {
    get.mockResolvedValue(detail(true, [done]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-full-rebuild")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-full-rebuild"));
    await waitFor(() => expect(screen.getByTestId("v2-kb-full-rebuild-body")).toBeTruthy());
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    await waitFor(() => expect(screen.queryByTestId("v2-kb-full-rebuild-body")).toBeNull());
    expect(sync).not.toHaveBeenCalled();
    fireEvent.click(screen.getByTestId("v2-kb-sync"));
    await waitFor(() => expect(sync).toHaveBeenCalledWith("KB1", "DS1"));
  });

  it("says a sync is already running when the server refuses a second rebuild (N17-obs275)", async () => {
    const { ApiError } = await import("../../../lib/api");
    sync.mockRejectedValueOnce(new ApiError("kb.sync_in_progress", "busy", { job_id: "ij-other" }));
    get.mockResolvedValue(detail(true, [done]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-full-rebuild")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-kb-full-rebuild"));
    await waitFor(() => expect(screen.getByTestId("v2-kb-full-rebuild-body")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-confirm-ok"));
    await waitFor(() => expect(toasts).toContain("An ingestion job is already running."));
    expect(toasts.some((x) => x.startsWith("Sync failed"))).toBe(false);
    await waitFor(() => expect(screen.queryByTestId("v2-kb-full-rebuild-body")).toBeNull());
  });

  it("is available exactly when the sync is (same gate)", async () => {
    const running = { ...done, job_id: "ij-run", status: "IN_PROGRESS" };
    get.mockResolvedValue(detail(true, [running]));
    renderDetail();
    await waitFor(() => expect(screen.getByTestId("v2-kb-full-rebuild")).toBeTruthy());
    const rebuild = screen.getByTestId("v2-kb-full-rebuild") as HTMLButtonElement;
    const plain = screen.getByTestId("v2-kb-sync") as HTMLButtonElement;
    expect(rebuild.disabled).toBe(true);
    expect(rebuild.disabled).toBe(plain.disabled);
    expect(rebuild.getAttribute("title")).toBe(plain.getAttribute("title"));
  });
});

// N17-obs530: a member who can SEE a KB another principal created (an administrator's, under
// `isolated`) must not be offered its mutations — the server answers 403 `auth.forbidden` on
// delete / data sources / upload / full rebuild / description; sync and the retrieval test stay.
const member = (over: Partial<AuthContextValue> = {}): AuthContextValue => ({
  authRequired: true, username: "itdesk05", userId: "u-desk", role: "member", email: null,
  accountExpiresAt: null, isAdmin: false, can: () => true, logout: async () => undefined, ...over,
});

function renderAs(value: AuthContextValue) {
  return render(
    <AuthContext.Provider value={value}>
      <MemoryRouter initialEntries={["/v2/knowledge-bases?view=detail&id=KB1"]}>
        <KbDetail id="KB1" onGone={() => undefined} />
      </MemoryRouter>
    </AuthContext.Provider>,
  );
}

describe("KbDetail creator-or-admin gating (N17-obs530)", () => {
  const adminsKb = (): KnowledgeBaseDetail => ({ ...detail(false), created_by: "u-admin", created_by_name: "trainer" });

  it("hides every mutation from a member who did not create the KB and names the creator", async () => {
    get.mockResolvedValue(adminsKb());
    renderAs(member());
    await waitFor(() => expect(screen.getByTestId("v2-kb-basic")).toBeTruthy());
    expect(screen.getByTestId("v2-kb-creator-only").textContent).toContain("trainer");
    for (const id of ["v2-kb-delete", "v2-kb-add", "v2-kb-full-rebuild", "v2-kb-ds-remove", "v2-kb-desc-edit"]) {
      expect(screen.queryByTestId(id), id).toBeNull();
    }
    // the incremental sync (and the documents / retrieval tabs) stay a viewer's
    expect(screen.getByTestId("v2-kb-sync")).toBeTruthy();
  });

  it("keeps the mutations for the creator and for an administrator", async () => {
    get.mockResolvedValue(adminsKb());
    renderAs(member({ userId: "u-admin", username: "trainer" }));
    await waitFor(() => expect(screen.getByTestId("v2-kb-basic")).toBeTruthy());
    for (const id of ["v2-kb-delete", "v2-kb-add", "v2-kb-full-rebuild", "v2-kb-ds-remove", "v2-kb-desc-edit"]) {
      expect(screen.getByTestId(id), id).toBeTruthy();
    }
    expect(screen.queryByTestId("v2-kb-creator-only")).toBeNull();
    cleanup();
    get.mockResolvedValue(adminsKb());
    renderAs(member({ userId: "u-other", role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-kb-basic")).toBeTruthy());
    expect(screen.getByTestId("v2-kb-delete")).toBeTruthy();
    expect(screen.queryByTestId("v2-kb-creator-only")).toBeNull();
  });

  it("offers a member no repair action on a creation-window KB they cannot change", async () => {
    get.mockResolvedValue({ ...adminsKb(), data_sources: [] });
    renderAs(member());
    await waitFor(() => expect(screen.getByTestId("v2-kb-missing-source")).toBeTruthy());
    expect(screen.queryByTestId("v2-kb-repair")).toBeNull();
  });
});
