// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../../auth/auth-context";
import type { EvaluationRunInfo } from "../../../lib/evaluation";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { ToastContext } from "../../hooks";
import { TaskList } from "./TaskList";

/**
 * N17-obs555: 全部任务 offered 删除 on a completed run while `DELETE /api/eval/runs/{id}`
 * answers 409 `run.not_deletable` for anything but `failed` / `stopped`. The button follows
 * the server's rule: disabled with a hint on a completed run and on an active one (stop
 * first), enabled on failed / stopped runs.
 */

function run(id: string, status: EvaluationRunInfo["status"]): EvaluationRunInfo {
  return {
    id, name: `run ${id}`, description: "", agent_id: "agent-a", agent_name: "ada-agent",
    dataset_id: "ds-a", dataset_name: "ds", mode: "evaluators", evaluators: ["Builtin.Helpfulness"],
    status, queue_position: 0, scores: [], insights: {}, session_ids: ["s1"], batch_eval_id: null,
    error: null, created_at: "2026-11-08T00:00:00Z", updated_at: "2026-11-08T00:00:00Z",
    created_by: "u-ada", created_by_name: "ada",
  } as unknown as EvaluationRunInfo;
}

const RUNS = [run("done-1", "completed"), run("fail-1", "failed"), run("stop-1", "stopped"),
              run("queue-1", "queued"), run("eval-1", "evaluating")];

vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      listEvaluationRuns: () => Promise.resolve({ runs: RUNS, total: RUNS.length }),
      v2OnlineConfigs: () => Promise.resolve({ configs: [], total: 0 }),
      v2Evaluators: () => Promise.resolve({ evaluators: [], builtin_count: 0 }),
    },
  };
});

const auth: AuthContextValue = {
  authRequired: true, username: "ada", userId: "u-ada", role: "member", email: null,
  accountExpiresAt: null, isAdmin: false, can: () => true, logout: async () => undefined,
};

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "zh-CN", fallbackLng: "en", resources: { en: { common: en }, "zh-CN": { common: zh } },
    defaultNS: "common", interpolation: { escapeValue: false },
  });
});
afterEach(cleanup);

function mount() {
  return render(
    <ToastContext.Provider value={() => undefined}>
      <AuthContext.Provider value={auth}>
        <MemoryRouter initialEntries={["/v2/eval/tasks?tab=all"]}>
          <TaskList />
        </MemoryRouter>
      </AuthContext.Provider>
    </ToastContext.Provider>,
  );
}

describe("TaskList — the run delete button follows the server's rule (N17-obs555)", () => {
  it("disables 删除 on a completed run with the hint, keeps it for failed / stopped, stops active first", async () => {
    mount();
    const completed = (await screen.findByTestId("v2-task-delete-done-1")) as HTMLButtonElement;
    expect(completed.disabled).toBe(true);
    expect(completed.title).toBe(zh.v2.tasks.deleteCompletedHint);

    expect((screen.getByTestId("v2-task-delete-fail-1") as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByTestId("v2-task-delete-stop-1") as HTMLButtonElement).disabled).toBe(false);

    for (const id of ["queue-1", "eval-1"]) {
      const active = screen.getByTestId(`v2-task-delete-${id}`) as HTMLButtonElement;
      expect(active.disabled).toBe(true);
      expect(active.title).toBe(zh.v2.tasks.deleteActiveHint);
    }
  });
});
