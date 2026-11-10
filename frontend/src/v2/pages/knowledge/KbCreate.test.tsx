// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter, useLocation } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { KnowledgeBaseDetail, UploadFilesResult } from "../../../lib/knowledgeBases";
import en from "../../../locales/en/common.json";
import { KbCreate } from "./KbCreate";

/**
 * N17-obs204 (cn/r17-aa) forensics: the create page's upload failed in the browser while
 * the page toasted "created" over the error and navigated to the detail — which then
 * auto-synced the empty prefix. Now a failed upload keeps the page, names the created KB
 * and the primary action retries the upload to that same KB; create is never repeated.
 */

const create = vi.fn<() => Promise<KnowledgeBaseDetail>>();
const uploadFiles = vi.fn<() => Promise<UploadFilesResult>>();
const toasts: string[] = [];

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    v2KnowledgeApi: { ...actual.v2KnowledgeApi, create: () => create(), uploadFiles: () => uploadFiles() },
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
  create.mockReset();
  uploadFiles.mockReset();
  toasts.length = 0;
});
afterEach(cleanup);

function Location() {
  const loc = useLocation();
  return <span data-testid="loc">{loc.search}</span>;
}

const created: KnowledgeBaseDetail = {
  kb_id: "KB1", name: "docs", description: "", status: "ACTIVE", updated_at: null,
  data_sources: [], attached_agents: [],
};

async function fillAndSubmit() {
  render(
    <MemoryRouter initialEntries={["/v2/knowledge-bases?view=new"]}>
      <KbCreate />
      <Location />
    </MemoryRouter>,
  );
  fireEvent.change(screen.getByTestId("v2-kb-name"), { target: { value: "docs" } });
  const input = screen.getByTestId("v2-kb-file-input") as HTMLInputElement;
  fireEvent.change(input, { target: { files: [new File([new Uint8Array([104, 105])], "a.txt")] } });
  await waitFor(() => expect(screen.getByTestId("v2-kb-files")).toBeTruthy());
  fireEvent.click(screen.getByTestId("v2-kb-submit"));
}

describe("KbCreate upload failure (N17-obs204)", () => {
  it("stays on the page, names the created KB and retries the upload to the SAME KB", async () => {
    create.mockResolvedValue(created);
    uploadFiles.mockRejectedValueOnce(new TypeError("Failed to fetch"));
    await fillAndSubmit();
    await waitFor(() => expect(screen.getByTestId("v2-kb-create-error")).toBeTruthy());
    expect(screen.getByTestId("v2-kb-create-error").textContent).toContain("Cannot reach the console") // N17-obs470 copy;
    expect(screen.getByTestId("loc").textContent).toBe("?view=new"); // no navigation
    expect(toasts).toEqual([]); // no "created" toast over the error
    expect(screen.getByTestId("v2-kb-created-pending-upload")).toBeTruthy();
    expect(screen.getByTestId("v2-kb-created-id").textContent).toBe("KB1"); // the created KB is named
    expect(screen.getByTestId("v2-kb-open-created")).toBeTruthy(); // … and reachable
    expect(screen.getByTestId("v2-kb-submit").textContent).toBe(en.knowledge.create.retryUpload);

    uploadFiles.mockResolvedValueOnce({
      keys: ["kb/KB1/DS1/a.txt"],
      job: { job_id: "ij1", status: "STARTING", coalesced: false, covers_upload: true },
    });
    fireEvent.click(screen.getByTestId("v2-kb-submit"));
    await waitFor(() => expect(screen.getByTestId("loc").textContent).toBe("?view=detail&id=KB1"));
    expect(create).toHaveBeenCalledTimes(1); // never created twice
    expect(uploadFiles).toHaveBeenCalledTimes(2);
  });

  it("lands on the detail right away when the upload succeeds", async () => {
    create.mockResolvedValue(created);
    uploadFiles.mockResolvedValue({ keys: ["kb/KB1/DS1/a.txt"], job: null });
    await fillAndSubmit();
    await waitFor(() => expect(screen.getByTestId("loc").textContent).toBe("?view=detail&id=KB1"));
    expect(toasts).toEqual(["“docs” created — provisioning the data source."]);
  });
});
