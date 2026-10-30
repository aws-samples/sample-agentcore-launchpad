// @vitest-environment jsdom
import { cleanup, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { DataSource, KBDocument } from "../../../lib/knowledgeBases";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { SourcePanel } from "./KbDocuments";

/** N17-obs274 (rev-r22 pass 4): a document between its first and last ingestion slice is
 *  listed `INDEXING` — the console says 「索引中」 / "Indexing"; other statuses stay raw. */

const documents = vi.fn<() => Promise<{ documents: KBDocument[]; next_token: string | null }>>();

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return { ...actual, v2KnowledgeApi: { ...actual.v2KnowledgeApi, documents: () => documents() } };
});

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(async () => {
  cleanup();
  await i18next.changeLanguage("en");
});

const source = { ds_id: "DS1", name: "DS1", status: "AVAILABLE", bucket: "bkt", prefix: "kb/KB1/DS1/" } as DataSource;

function doc(name: string, status: string): KBDocument {
  return { name, uri: `s3://bkt/kb/KB1/DS1/${name}`, status, status_reason: null, indexed_at: null, size_bytes: 10, uploaded_at: null } as KBDocument;
}

describe("document status labels (N17-obs274)", () => {
  // N17-obs456: the other statuses are worded too (they used to stay raw)
  it.each([
    ["en", "Indexing", "Indexed", "Failed"],
    ["zh-CN", "索引中", "已索引", "失败"],
  ])("labels every document status in %s", async (lng, label, indexed, failed) => {
    await i18next.changeLanguage(lng);
    documents.mockResolvedValue({ documents: [doc("a.md", "INDEXING"), doc("b.md", "INDEXED"), doc("c.md", "FAILED")], next_token: null });
    render(<SourcePanel kbId="KB1" source={source} />);
    await waitFor(() => expect(screen.getByTestId("v2-kb-docs").textContent).toContain("a.md"));
    const table = screen.getByTestId("v2-kb-docs").textContent ?? "";
    expect(table).toContain(label);
    expect(table).toContain(indexed);
    expect(table).toContain(failed);
    // the rows only (the en header reads "INDEXED AT")
    const rows = screen.getByTestId("v2-kb-docs").querySelector("tbody")?.textContent ?? "";
    for (const raw of ["INDEXING", "INDEXED", "FAILED"]) expect(rows).not.toContain(raw);
  });
});
