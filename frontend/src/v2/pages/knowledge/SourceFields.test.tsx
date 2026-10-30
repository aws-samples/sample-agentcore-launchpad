// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { KB_SUPPORTED_EXT } from "../../../lib/knowledgeBases";
import en from "../../../locales/en/common.json";
import { emptySource, type SourceDraft } from "./source";
import { SourceFields } from "./SourceFields";

/**
 * N17-obs239 / N17-obs240 (user decision 2026-10-21): the "coming soon" connector list is
 * gone; the form states the document types the ingestion Lambda indexes and its limits, the
 * file input is filtered, and an unsupported extension is refused at pick time — nothing
 * reaches the draft, so nothing is uploaded.
 */

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en",
    fallbackLng: "en",
    resources: { en: { common: en } },
    defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(cleanup);

function renderFields(value: SourceDraft = emptySource(), error: string | null = null) {
  const onChange = vi.fn<(next: SourceDraft) => void>();
  render(<SourceFields value={value} onChange={onChange} error={error} />);
  return onChange;
}

const input = () => screen.getByTestId("v2-kb-file-input") as HTMLInputElement;
const pick = (...files: File[]) => fireEvent.change(input(), { target: { files } });

describe("SourceFields supported types and limits (N17-obs240)", () => {
  it("renders one chip per supported extension and the limits hint — no coming-soon list", () => {
    renderFields();
    const chips = Array.from(screen.getByTestId("v2-kb-supported").querySelectorAll(".v2-tag")).map(
      (el) => el.textContent,
    );
    expect(chips).toEqual([...KB_SUPPORTED_EXT]);
    expect(chips).toEqual([".txt", ".md", ".markdown", ".csv", ".json", ".html", ".htm", ".pdf"]);
    expect(screen.getByTestId("v2-kb-supported").textContent).toContain(en.knowledge.source.supportedLabel);
    expect(screen.getByTestId("v2-kb-supported-hint").textContent).toBe(en.knowledge.source.supportedHint);
    expect(en.knowledge.source.supportedHint).toContain("2 MB");
    expect(en.knowledge.source.supportedHint).toContain("400 chunks");
    expect(screen.queryByText(/coming soon/i)).toBeNull();
    expect(screen.queryByText(/sharepoint/i)).toBeNull();
  });

  it("filters the file picker with the same extension list", () => {
    renderFields();
    expect(input().getAttribute("accept")).toBe(".txt,.md,.markdown,.csv,.json,.html,.htm,.pdf");
  });
});

describe("SourceFields pick-time refusal (N17-obs239)", () => {
  it("refuses a.docx: field error names the file and the draft receives nothing", () => {
    const onChange = renderFields();
    pick(new File(["x"], "a.docx"));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.queryByTestId("v2-kb-files")).toBeNull();
    const err = screen.getByText(/Unsupported file type/);
    expect(err.className).toBe("err");
    expect(err.textContent).toContain("a.docx");
    expect(err.textContent).toContain(".pdf");
  });

  it("refuses the whole pick when one of several files is unsupported", () => {
    const onChange = renderFields();
    pick(new File(["x"], "ok.txt"), new File(["x"], "bad.xlsx"));
    expect(onChange).not.toHaveBeenCalled();
    expect(screen.getByText(/Unsupported file type/).textContent).toContain("bad.xlsx");
  });

  it("accepts A.PDF (case-insensitive) and clears an earlier refusal", () => {
    const onChange = renderFields();
    pick(new File(["x"], "a.docx"));
    expect(screen.getByText(/Unsupported file type/)).toBeTruthy();
    const pdf = new File(["%PDF"], "A.PDF");
    pick(pdf);
    expect(onChange).toHaveBeenCalledTimes(1);
    expect(onChange.mock.calls[0][0].files).toEqual([pdf]);
    expect(screen.queryByText(/Unsupported file type/)).toBeNull();
  });

  it("the parent's own validation message wins over the pick error", () => {
    renderFields(emptySource(), "Pick at least one file");
    pick(new File(["x"], "a.docx"));
    expect(screen.getByText("Pick at least one file").className).toBe("err");
    expect(screen.queryByText(/Unsupported file type/)).toBeNull();
  });

  it("warns (does not block) about a picked file over 2 MB", () => {
    const big = new File([new Uint8Array(2_000_001)], "big.pdf");
    const small = new File(["x"], "small.txt");
    renderFields({ ...emptySource(), files: [big, small] });
    expect(screen.getByTestId("v2-kb-files").textContent).toContain("big.pdf");
    const hint = screen.getByTestId("v2-kb-large-hint");
    expect(hint.textContent).toContain("big.pdf");
    expect(hint.textContent).not.toContain("small.txt");
    expect(hint.className).toBe("hint");
  });

  it("shows no size warning when every file is under 2 MB", () => {
    renderFields({ ...emptySource(), files: [new File(["x"], "small.txt")] });
    expect(screen.queryByTestId("v2-kb-large-hint")).toBeNull();
  });
});
