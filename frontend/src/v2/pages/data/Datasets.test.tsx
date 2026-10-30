// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { V2Dataset } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { DatasetDetail, DatasetEditor, DatasetsTab } from "./Datasets";

/** N17-obs424: the dataset console — JSONL import, the per-scenario ground-truth editor,
 *  the read-only scenario list, the revision and the rewrite notice. */

const ds: V2Dataset = {
  id: "ds1", name: "golden", kind: "predefined", locale: "zh-CN", description: "", item_count: 2,
  items: [
    {
      scenario_id: "refund-1",
      turns: [{ input: "我的订单在哪？", expected_response: "今天发货。" }, { input: "能退款吗？" }],
      assertions: ["提到订单", "给出退款方式"],
      expected_trajectory: ["launchpad_gw_orders___lookup"],
      split: "held_out",
    },
    { scenario_id: "greet", turns: [{ input: "你好" }], expected_trajectory: [] },
  ],
  cloud: null, has_ground_truth: true, created_at: "2026-10-25T00:00:00Z", revision: 3,
  split_counts: { tuning: 1, held_out: 1 }, item_splits: ["held_out", "tuning"], agent_id: null,
};

const update = vi.fn((_id: string, body: { items?: unknown[] }) => Promise.resolve({ ...ds, items: body.items ?? ds.items, tool_name_rewrites: [] }));
const upload = vi.fn((body: { name: string }) => Promise.resolve({ ...ds, id: "ds2", name: body.name, tool_name_rewrites: [{ scenario_id: "s1", from: "a", to: "launchpad_gw_a" }] }));

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      v2Datasets: () => Promise.resolve({ datasets: [ds] }),
      listAgents: () => Promise.resolve({ agents: [] }),
      v2UpdateDataset: (id: string, body: { items?: unknown[] }) => update(id, body),
      v2UploadDataset: (body: { name: string }) => upload(body),
    },
  };
});

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(() => {
  cleanup();
  update.mockClear();
  upload.mockClear();
});

describe("dataset detail (read-only)", () => {
  it("shows the revision and every scenario's ground truth", async () => {
    render(
      <MemoryRouter initialEntries={["/v2/eval/data?tab=datasets&view=dataset&id=ds1"]}>
        <DatasetDetail id="ds1" />
      </MemoryRouter>,
    );
    expect(await screen.findByTestId("v2-dataset-revision")).toHaveProperty("textContent", "r3");
    expect(screen.getByText("refund-1")).toBeTruthy();
    expect(screen.getByText("参考回答：今天发货。")).toBeTruthy();
    expect(screen.getByText("2 条断言")).toBeTruthy();
    expect(screen.getByText("launchpad_gw_orders___lookup")).toBeTruthy();
    expect(screen.getByText("不应调用工具")).toBeTruthy();
    expect(screen.getByText("留出")).toBeTruthy();
    expect(screen.queryByTestId("v2-dataset-rewrites")).toBeNull();
  });

  it("lists the names a save rewrote", async () => {
    render(
      <MemoryRouter initialEntries={[{ pathname: "/v2/eval/data", search: "?tab=datasets&view=dataset&id=ds1", state: { rewrites: [{ scenario_id: "refund-1", from: "orders___lookup", to: "launchpad_gw_orders___lookup" }] } }]}>
        <DatasetDetail id="ds1" />
      </MemoryRouter>,
    );
    const notice = await screen.findByTestId("v2-dataset-rewrites");
    expect(notice.textContent).toContain("orders___lookup");
    expect(notice.textContent).toContain("1 个期望工具名已按智能体的实际工具名保存");
  });
});

describe("dataset editor", () => {
  it("edits turns, assertions, the trajectory and saves the full scenario", async () => {
    render(
      <MemoryRouter initialEntries={["/v2/eval/data?tab=datasets&view=dataset-edit&id=ds1"]}>
        <DatasetEditor id="ds1" />
      </MemoryRouter>,
    );
    fireEvent.change(await screen.findByTestId("v2-dataset-expected-0-1"), { target: { value: "可以，七天内。" } });
    fireEvent.click(screen.getByTestId("v2-dataset-add-turn-1"));
    fireEvent.change(screen.getByTestId("v2-dataset-input-1-1"), { target: { value: "再见" } });
    fireEvent.change(screen.getByTestId("v2-dataset-assertions-1"), { target: { value: "礼貌问候\n" } });
    fireEvent.change(screen.getByTestId("v2-dataset-trajectory-0"), { target: { value: "orders___lookup, refund" } });
    fireEvent.click(screen.getByTestId("v2-dataset-save"));
    await waitFor(() => expect(update).toHaveBeenCalled());
    const items = update.mock.calls[0][1].items as Record<string, unknown>[];
    expect(items[0]).toMatchObject({
      scenario_id: "refund-1", split: "held_out", assertions: ["提到订单", "给出退款方式"],
      expected_trajectory: ["orders___lookup", "refund"],
      turns: [{ input: "我的订单在哪？", expected_response: "今天发货。" }, { input: "能退款吗？", expected_response: "可以，七天内。" }],
    });
    expect(items[1]).toMatchObject({ scenario_id: "greet", expected_trajectory: [], assertions: ["礼貌问候"], turns: [{ input: "你好" }, { input: "再见" }] });
  });
});

describe("JSONL import", () => {
  it("uploads the file through the upload route", async () => {
    render(
      <MemoryRouter initialEntries={["/v2/eval/data?tab=datasets"]}>
        <DatasetsTab />
      </MemoryRouter>,
    );
    fireEvent.click(await screen.findByTestId("v2-dataset-upload"));
    const dialog = await screen.findByTestId("v2-dataset-upload-dialog");
    const line = JSON.stringify({ scenario_id: "s1", turns: [{ input: "hi" }], expected_trajectory: ["a"] });
    const file = new File([`${line}\n`], "golden-set.jsonl", { type: "application/jsonl" });
    fireEvent.change(within(dialog).getByTestId("v2-dataset-upload-file"), { target: { files: [file] } });
    await waitFor(() => expect((within(dialog).getByTestId("v2-dataset-upload-name") as HTMLInputElement).value).toBe("golden-set"));
    fireEvent.click(within(dialog).getByTestId("v2-dataset-upload-confirm"));
    await waitFor(() => expect(upload).toHaveBeenCalled());
    expect(upload.mock.calls[0][0]).toMatchObject({ name: "golden-set", jsonl: `${line}\n` });
  });
});
