// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, beforeEach, describe, expect, it, vi } from "vitest";

import type { V2Dataset } from "../../../lib/api";
import { resetModelDefaultsForTests } from "../../../lib/model-defaults";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { TaskWizard } from "./TaskWizard";

const fixture = vi.hoisted(() => ({ datasets: [] as V2Dataset[], submit: vi.fn(), loadError: false }));
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return { ...actual, api: { ...actual.api,
    listAgents: async () => ({ agents: [{ id: "agent-1", name: "Test agent", status: "active" }] }),
    v2Datasets: async () => { if (fixture.loadError) throw new Error("dataset read failed"); return { datasets: fixture.datasets }; },
    v2Evaluators: async () => ({ evaluators: [], builtin_count: 0 }),
    v2CreateRun: fixture.submit,
  } };
});
beforeAll(async () => {
  Element.prototype.scrollIntoView = () => undefined;
  await i18next.use(initReactI18next).init({ lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common", interpolation: { escapeValue: false } });
});
beforeEach(() => { resetModelDefaultsForTests({}); fixture.submit.mockReset(); fixture.submit.mockResolvedValue({ id: "run-new" }); fixture.loadError = false; });
afterEach(cleanup);

function dataset(size: number, id = "ds-1"): V2Dataset {
  return { id, name: id, kind: "predefined", locale: "zh-CN", description: "", item_count: size,
    items: Array.from({ length: size }, (_, i) => ({ scenario_id: `case-${i + 1}`, turns: [{ input: `Question ${i + 1}`, expected_response: "Expected" }] })),
    item_splits: Array.from({ length: size }, () => "tuning"), split_counts: { tuning: size, held_out: 0 },
    cloud: null, has_ground_truth: true, created_at: null, revision: 1 };
}
async function mount() {
  render(<MemoryRouter initialEntries={["/v2/eval/tasks?view=new&agent=agent-1&dataset=ds-1"]}><TaskWizard /></MemoryRouter>);
  fireEvent.change(await screen.findByTestId("v2-task-name"), { target: { value: "Preview regression" } });
  return within(await screen.findByTestId("v2-task-dataset-preview"));
}
async function select(testId: string, label: string | RegExp) {
  fireEvent.click(screen.getByTestId(testId));
  fireEvent.click(await screen.findByRole("option", { name: label }));
}
async function submit() {
  fireEvent.click(screen.getByTestId("v2-task-next"));
  fireEvent.click(await screen.findByTestId("v2-task-submit"));
  await waitFor(() => expect(fixture.submit).toHaveBeenCalledTimes(1));
  return fixture.submit.mock.calls[0][0];
}

describe("TaskWizard dataset split preview", () => {
  it.each([5, 8])("shows every scenario for a %i-item dataset and uses that count at submission", async (size) => {
    fixture.datasets = [dataset(size)];
    const preview = await mount();
    expect(preview.getAllByText(/^Question /)).toHaveLength(size);
    expect(screen.getByTestId("v2-task-preview-range").textContent).toContain(`显示第 1–${size} 个`);
    expect(screen.getByTestId("v2-task-dataset-count").textContent).toContain(`本次将评估 ${size} 个场景`);
    fireEvent.click(screen.getByTestId("v2-task-next"));
    expect(screen.getByTestId("v2-task-dataset-count").textContent).toContain(`本次将评估 ${size} 个场景`);
    fireEvent.click(screen.getByTestId("v2-task-submit"));
    await waitFor(() => expect(fixture.submit).toHaveBeenCalledTimes(1));
    expect(fixture.submit.mock.calls[0][0]).toMatchObject({ dataset_id: "ds-1" });
    expect(fixture.submit.mock.calls[0][0]).not.toHaveProperty("split");
    expect(fixture.submit.mock.calls[0][0]).not.toHaveProperty("items");
  });

  it("uses server-resolved splits even when raw scenarios omit split, and resets the page", async () => {
    const ds = dataset(22);
    ds.item_splits = ds.items.map((_, i) => i % 2 ? "held_out" : "tuning");
    fixture.datasets = [ds, dataset(5, "ds-2")];
    await mount();
    fireEvent.click(screen.getByTestId("v2-task-preview-pages-next"));
    expect(screen.getByTestId("v2-task-preview-range").textContent).toContain("11–20");
    await select("v2-task-split", zh.v2.tasks.splitOption.held_out);
    const preview = within(screen.getByTestId("v2-task-dataset-preview"));
    expect(preview.getByText("Question 2")).toBeTruthy();
    expect(preview.queryByText("Question 1")).toBeNull();
    expect(screen.getByTestId("v2-task-preview-range").textContent).toContain("1–10");
    expect(screen.getByTestId("v2-task-dataset-count").textContent).toContain("11 个场景");
    fireEvent.click(screen.getByTestId("v2-task-preview-pages-next"));
    expect(preview.getAllByText(/^Question /)).toHaveLength(1);
    expect(preview.getByText("Question 22")).toBeTruthy();
    await select("v2-task-split", zh.v2.tasks.splitOption.tuning);
    expect(preview.getByText("Question 1")).toBeTruthy();
    expect(preview.queryByText("Question 2")).toBeNull();
    await select("v2-task-dataset", /^ds-2/);
    expect(screen.getByTestId("v2-task-preview-range").textContent).toContain("1–5");
  });

  it("paging the held-out preview never changes the selected request scope", async () => {
    const ds = dataset(22); ds.item_splits = ds.items.map(() => "held_out"); fixture.datasets = [ds];
    await mount();
    await select("v2-task-split", zh.v2.tasks.splitOption.held_out);
    fireEvent.click(screen.getByTestId("v2-task-preview-pages-next"));
    const payload = await submit();
    expect(payload).toEqual({ agent_id: "agent-1", name: "Preview regression", description: undefined, evaluators: ["Builtin.Correctness", "Builtin.Helpfulness"], dataset_id: "ds-1", split: "held_out", judge_model_id: expect.any(String) });
  });

  it.each(["all", "held_out"] as const)("shows 0 without a 1–0 range and refuses an empty %s selection", async (split) => {
    fixture.datasets = [dataset(split === "all" ? 0 : 8)];
    await mount();
    if (split !== "all") await select("v2-task-split", zh.v2.tasks.splitOption[split]);
    expect(screen.getByTestId("v2-task-dataset-count").textContent).toContain("0 个场景");
    expect(screen.getByTestId("v2-task-preview-range").textContent).toContain("0–0");
    expect(screen.getByText(zh.v2.tasks.previewEmpty)).toBeTruthy();
    fireEvent.click(screen.getByTestId("v2-task-next"));
    expect(screen.queryByTestId("v2-task-submit")).toBeNull();
    expect(fixture.submit).not.toHaveBeenCalled();
  });

  it("does not guess missing or incomplete server split metadata", async () => {
    const ds = dataset(8); ds.item_splits = ["held_out"]; fixture.datasets = [ds];
    await mount(); await select("v2-task-split", zh.v2.tasks.splitOption.held_out);
    expect(screen.getByTestId("v2-task-dataset-count").textContent).toContain(zh.v2.tasks.previewSplitUnavailable);
    expect(screen.queryByTestId("v2-task-preview-range")).toBeNull();
    fireEvent.click(screen.getByTestId("v2-task-next"));
    expect(fixture.submit).not.toHaveBeenCalled();
    await select("v2-task-split", zh.v2.tasks.splitOption.all);
    expect(within(screen.getByTestId("v2-task-dataset-preview")).getAllByText(/^Question /)).toHaveLength(8);
  });

  it("shows a dataset read failure without calling it an empty dataset", async () => {
    fixture.loadError = true; fixture.datasets = [];
    render(<MemoryRouter initialEntries={["/v2/eval/tasks?view=new&agent=agent-1&dataset=ds-1"]}><TaskWizard /></MemoryRouter>);
    expect(await screen.findByText("dataset read failed")).toBeTruthy();
    expect(screen.queryByTestId("v2-task-dataset-count")).toBeNull();
    expect(fixture.submit).not.toHaveBeenCalled();
  });

  it.each(["revision", "content", "removed"])("refreshes a changed dataset (%s) before creating a run", async (change) => {
    fixture.datasets = [dataset(8)];
    await mount();
    fireEvent.click(screen.getByTestId("v2-task-next"));
    fixture.datasets = change === "removed" ? [] : [dataset(5)];
    if (change === "revision") fixture.datasets[0].revision = 2;
    fireEvent.click(screen.getByTestId("v2-task-submit"));
    expect(await screen.findByText(zh.v2.tasks.previewChanged)).toBeTruthy();
    expect(fixture.submit).not.toHaveBeenCalled();
    if (change !== "removed") {
      expect(screen.getByTestId("v2-task-dataset-count").textContent).toContain("5 个场景");
      expect(within(screen.getByTestId("v2-task-dataset-preview")).getAllByText(/^Question /)).toHaveLength(5);
    } else {
      expect(screen.queryByTestId("v2-task-dataset-count")).toBeNull();
    }
  });

  it("does not create a run when the submission refresh fails", async () => {
    fixture.datasets = [dataset(8)];
    await mount();
    fireEvent.click(screen.getByTestId("v2-task-next"));
    fixture.loadError = true;
    fireEvent.click(screen.getByTestId("v2-task-submit"));
    expect(await screen.findByText("dataset read failed")).toBeTruthy();
    expect(fixture.submit).not.toHaveBeenCalled();
  });
});
