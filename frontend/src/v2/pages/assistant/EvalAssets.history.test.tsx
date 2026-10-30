// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, within } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { api, type AssistantEvalOperation, type AssistantEvalPlan, type AssistantEvalPlanState } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import { EvalAssetsCard } from "./EvalAssets";

/**
 * N17-obs467 (user confusion 2026-10-26, conversation e6ca9126): once a newer plan revision
 * exists, an earlier plan's SUCCEEDED operation moves to 「更早的操作」, whose only action was the
 * destructive cleanup — the dataset, evaluators and Lambda it created were invisible and the
 * user thought them gone. A history row now opens the same resource table read-only
 * (「查看」, placed BEFORE the cleanup), and a newer draft says an earlier revision already
 * created assets.
 */

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return { ...actual, api: { ...actual.api, assistantEvalPlan: vi.fn() } };
});

const plan = (revision: number, status: AssistantEvalPlan["status"]): AssistantEvalPlan => ({
  id: `plan-${revision}`, conversation_id: "e6ca9126", proposal_id: "p-1", source_revision: 1,
  source_content_hash: "h", revision, source: "platform", status,
  content: { scenarios: [], evaluators: [], blocked_golden_tests: [], recommendations: [] },
  content_hash: `hash${revision}`.padEnd(64, "0"), validation_errors: [], summary: null,
  created_by: "demo-admin", created_at: null, operation_id: null,
});

const op = (status: AssistantEvalOperation["status"], overrides: Partial<AssistantEvalOperation> = {}): AssistantEvalOperation => ({
  id: "8ef184fd", conversation_id: "e6ca9126", plan_id: "plan-2", plan_revision: 2,
  plan_hash: "hash2".padEnd(64, "0"), proposal_revision: 1, approved_by: "demo-admin",
  account_id: "123456789012", region: "cn-northwest-1", pinned: {}, status, attempts: 1,
  max_attempts: 3, dataset_id: "18f0af5c0ac5", error: null, created_at: null, updated_at: null,
  running: false, requires_new_plan: false,
  resources: [
    { kind: "dataset", key: "dataset", name: "travel-expense-golden", status: "ready",
      result: { dataset_id: "18f0af5c0ac5", item_count: 6 } },
    { kind: "evaluator", key: "ev:policy", name: "policy_compliance", status: "ready",
      result: { evaluator_id: "policy_compliance-Ab12Cd", level: "TRACE" } },
    { kind: "lambda_function", key: "fn:amount", name: "lp-eval-amount-check", status: "ready",
      result: { function_arn: "arn:aws-cn:lambda:cn-northwest-1:123456789012:function:lp-eval-amount-check" } },
  ],
  ...overrides,
});

function show(state: AssistantEvalPlanState, canMaterialize = true) {
  vi.mocked(api.assistantEvalPlan).mockResolvedValue(state);
  return render(
    <MemoryRouter>
      <EvalAssetsCard conversationId="e6ca9126" proposals={[]} canMaterialize={canMaterialize} workspaceId="default"
        apiMessage={String} onError={() => undefined} deployed={null} onRepair={async () => null} />
    </MemoryRouter>,
  );
}

const live = (): AssistantEvalPlanState => ({
  plans: [plan(2, "approved"), plan(3, "draft")], operations: [op("succeeded")], disclosure: "",
});

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});

afterEach(() => cleanup());

describe("earlier plan operations (N17-obs467)", () => {
  it("a history row offers 「View」 before the destructive cleanup", async () => {
    show(live());
    const history = await screen.findByTestId("v2-assistant-eval-history");
    const view = within(history).getByTestId("v2-assistant-eval-history-view-8ef184fd");
    const cleanupLink = within(history).getByText(en.assistantEval.cleanup);
    expect(view.compareDocumentPosition(cleanupLink) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it("「View」 opens the operation's resources read-only: dataset link, evaluator, Lambda", async () => {
    show(live());
    fireEvent.click(await screen.findByTestId("v2-assistant-eval-history-view-8ef184fd"));
    const panel = screen.getByTestId("v2-assistant-eval-history-operation");
    expect(panel.textContent).toContain("r2");
    const table = within(panel).getByTestId("v2-assistant-eval-history-resources");
    const dataset = within(table).getAllByRole("link").find((a) => a.getAttribute("href")?.includes("18f0af5c0ac5"));
    expect(dataset?.getAttribute("href")).toBe("/v2/eval/data?tab=datasets&view=dataset&id=18f0af5c0ac5");
    expect(table.textContent).toContain("policy_compliance-Ab12Cd");
    expect(table.textContent).toContain("function:lp-eval-amount-check");
    // read-only: no retry / cleanup inside the view, and the live view's test ids stay unique
    expect(within(panel).queryByTestId("v2-assistant-eval-cleanup")).toBeNull();
    expect(within(panel).queryByTestId("v2-assistant-eval-retry")).toBeNull();
    expect(screen.queryByTestId("v2-assistant-eval-operation")).toBeNull();
    fireEvent.click(screen.getByTestId("v2-assistant-eval-history-view-8ef184fd"));
    expect(screen.queryByTestId("v2-assistant-eval-history-operation")).toBeNull();
  });

  it("a viewer who may not clean up still sees the assets", async () => {
    show(live(), false);
    const history = await screen.findByTestId("v2-assistant-eval-history");
    expect(within(history).queryByText(en.assistantEval.cleanup)).toBeNull();
    fireEvent.click(within(history).getByTestId("v2-assistant-eval-history-view-8ef184fd"));
    expect(screen.getByTestId("v2-assistant-eval-history-resources").textContent).toContain("18f0af5c0ac5");
  });

  it("a newer draft says the earlier revision already created assets, with a way to see them", async () => {
    show(live());
    const notice = await screen.findByTestId("v2-assistant-eval-earlier-assets");
    expect(notice.textContent).toContain("r2");
    expect(notice.textContent).toContain("18f0af5c0ac5");
    fireEvent.click(within(notice).getByTestId("v2-assistant-eval-earlier-assets-view"));
    expect(screen.getByTestId("v2-assistant-eval-history-operation").textContent).toContain("r2");
  });

  it("no notice when the current plan has its own operation, or the earlier one created nothing live", async () => {
    show({ ...live(), plans: [plan(2, "approved")] });
    await screen.findByTestId("v2-assistant-eval-operation");
    expect(screen.queryByTestId("v2-assistant-eval-earlier-assets")).toBeNull();
    cleanup();
    show({ ...live(), operations: [op("failed")] });
    await screen.findByTestId("v2-assistant-eval-history");
    expect(screen.queryByTestId("v2-assistant-eval-earlier-assets")).toBeNull();
    cleanup();
    show({ ...live(), operations: [op("cleaned")] });
    await screen.findByTestId("v2-assistant-eval-history");
    expect(screen.queryByTestId("v2-assistant-eval-earlier-assets")).toBeNull();
  });
});
