// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { KnowledgeBaseDetail, UploadFilesResult } from "../../../lib/knowledgeBases";
import en from "../../../locales/en/common.json";
import { KnowledgeBaseCreateDrawer } from "./KnowledgeBaseCreate";

/**
 * N17-obs204 (cn/r17-aa): the upload response carries the ingestion sync the server
 * started. The wizard tracks THAT job instead of starting a second sync of its own; it
 * starts one itself only when the upload reported none (`job: null`).
 */

const createKnowledgeBase = vi.fn<() => Promise<KnowledgeBaseDetail>>();
const getKnowledgeBase = vi.fn<() => Promise<KnowledgeBaseDetail>>();
const uploadKnowledgeBaseFiles = vi.fn<() => Promise<UploadFilesResult>>();
const syncKnowledgeBase = vi.fn(async () => ({ job_id: "ij-wizard", status: "STARTING" }));

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      createKnowledgeBase: () => createKnowledgeBase(),
      getKnowledgeBase: () => getKnowledgeBase(),
      uploadKnowledgeBaseFiles: () => uploadKnowledgeBaseFiles(),
      syncKnowledgeBase: () => syncKnowledgeBase(),
    },
  };
});

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
  createKnowledgeBase.mockReset();
  getKnowledgeBase.mockReset();
  uploadKnowledgeBaseFiles.mockReset();
  syncKnowledgeBase.mockClear();
});
afterEach(cleanup);

function detail(jobs: KnowledgeBaseDetail["data_sources"][number]["ingestion_jobs"] = []): KnowledgeBaseDetail {
  return {
    kb_id: "KB1",
    name: "docs",
    description: "",
    status: "ACTIVE",
    updated_at: null,
    data_sources: [
      {
        ds_id: "DS1", name: "DS1", status: "AVAILABLE", bucket: "bkt", prefix: "kb/KB1/",
        ingestion_jobs: jobs, has_objects: true,
      },
    ],
    attached_agents: [],
  };
}

async function createAndUpload(upload: UploadFilesResult) {
  createKnowledgeBase.mockResolvedValue(detail());
  getKnowledgeBase.mockResolvedValue(detail());
  uploadKnowledgeBaseFiles.mockResolvedValue(upload);
  render(
    <MemoryRouter>
      <KnowledgeBaseCreateDrawer workspaceId="default" onCreated={() => undefined} onClose={() => undefined} />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByTestId("v2-assistant-kb-name"), { target: { value: "docs" } });
  const input = screen.getByTestId("v2-assistant-kb-files") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File([new Uint8Array([104, 105])], "a.txt")] } });
  fireEvent.click(screen.getByTestId("v2-assistant-kb-submit"));
  await waitFor(() => expect(uploadKnowledgeBaseFiles).toHaveBeenCalledTimes(1));
  await waitFor(() => expect(getKnowledgeBase).toHaveBeenCalled());
}

describe("KnowledgeBaseCreate tracks the upload's sync (N17-obs204)", () => {
  it("does not start a sync of its own when the upload reported one", async () => {
    await createAndUpload({
      keys: ["kb/KB1/a.txt"],
      job: { job_id: "ij-upload", status: "STARTING", coalesced: false, covers_upload: true },
    });
    // the tracked job is the upload's: the status line says indexing is under way
    await waitFor(() => expect(screen.getByText(en.assistantPreparation.kb.indexing)).toBeTruthy());
    await new Promise((r) => setTimeout(r, 20));
    expect(syncKnowledgeBase).not.toHaveBeenCalled();
  });

  it("falls back to its own sync when the upload reported none", async () => {
    await createAndUpload({ keys: ["kb/KB1/a.txt"], job: null });
    await waitFor(() => expect(syncKnowledgeBase).toHaveBeenCalledTimes(1));
  });
});
