// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../auth/auth-context";
import { ToastContext } from "../hooks";
import { type AgentInfo, ApiError } from "../../lib/api";
import en from "../../locales/en/common.json";
import { V2Agents } from "./Agents";

/**
 * N15-obs9 (isolation UI O2): the Agent detail mirrors the server's creator-or-admin gate on
 * redeploy / delete (`require_agent_creator`, 403 `agent.forbidden`) the way the canary /
 * Skill Lab / dataset / evaluator pages do (r13-g): a member who holds the permissions but
 * did not create the agent sees 删除 / 编辑 disabled with the creator-only hint and an
 * `Alert` naming the creator; the creator and administrators see them enabled; a NULL-creator
 * agent is an administrator's.
 */

let agent: AgentInfo;
// N16-obs76: `getAgent` answers from `agent` until a test makes it reject (the agent was deleted)
let getAgentError: Error | null = null;
const base: AgentInfo = {
  id: "a1", name: "demo", method: "zip_runtime", status: "active", arn: null, resource_id: null,
  version: null, created_by: "u-ada", created_by_name: "ada", error: null, spec: {},
  invoke_capability: { eligible: false, reason: null, reason_code: null },
} as AgentInfo;

// N17-obs457: what `agentVersions` answers (no rows unless a test sets them)
const NO_VERSIONS = { kind: "runtime", resource_id: "", versions: [], endpoints: [], latest_version: null,
  ledger_version: null, canary_endpoints: [] };
let versions: Record<string, unknown> = NO_VERSIONS;

// N16-obs111: `deleteAgent` answers from `deleteAgentError` when a test sets it (409 agent.in_use)
let deleteAgentError: Error | null = null;

vi.mock("../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      getAgent: () => (getAgentError ? Promise.reject(getAgentError) : Promise.resolve(agent)),
      deleteAgent: () =>
        deleteAgentError ? Promise.reject(deleteAgentError) : Promise.resolve({ deleted: true }),
      getJob: () => Promise.resolve(null),
      agentVersions: () => Promise.resolve(versions),
    },
  };
});

const auth = (over: Partial<AuthContextValue>): AuthContextValue => ({
  authRequired: true, username: "bob", userId: "u-bob", role: "member", email: null,
  accountExpiresAt: null, isAdmin: false, can: () => true, logout: async () => undefined, ...over,
});

const toasts: Array<{ tone: string; text: string }> = [];

function mount(value: AuthContextValue) {
  toasts.length = 0;
  return render(
    <ToastContext.Provider value={(tone, text) => void toasts.push({ tone, text })}>
      <AuthContext.Provider value={value}>
        <MemoryRouter initialEntries={["/v2/agents?view=detail&id=a1"]}>
          <V2Agents />
        </MemoryRouter>
      </AuthContext.Provider>
    </ToastContext.Provider>,
  );
}

beforeAll(async () => {
  await i18next.use(initReactI18next).init({
    lng: "en", fallbackLng: "en", resources: { en: { common: en } }, defaultNS: "common",
    interpolation: { escapeValue: false },
  });
});
afterEach(() => {
  cleanup();
  getAgentError = null;
  deleteAgentError = null;
  versions = NO_VERSIONS;
});

describe("AgentDetail delete refused by a running canary (N16-obs111)", () => {
  it("keeps the dialog open, names the canary and links to its detail page", async () => {
    agent = { ...base, created_by: null, created_by_name: null };
    deleteAgentError = new ApiError("agent.in_use", "1 Runtime canary(ies) on this agent are still running", {
      canaries: [{ id: "cn1", name: "CANARY-demo", status: "running", stage: "traffic" }],
    });
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-agent-delete")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-agent-delete"));
    await waitFor(() => expect(screen.getByTestId("v2-confirm-ok")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-confirm-ok"));
    await waitFor(() => expect(screen.getByTestId("v2-agent-delete-blocked")).toBeTruthy());
    // the dialog is still open (the delete did not happen), the reason is the 409's copy
    expect(screen.getByTestId("v2-confirm-ok")).toBeTruthy();
    expect(screen.getByTestId("v2-agent-delete-blocked").textContent).toContain("Runtime canary");
    const link = screen.getByTestId("v2-agent-delete-blocked-canary-cn1") as HTMLAnchorElement;
    expect(link.textContent).toContain("CANARY-demo");
    expect(link.textContent).toContain("Running"); // N17-obs457: the canary status is worded
    expect(link.getAttribute("href")).toBe("/v2/eval/experiments?mode=canary&canary=cn1");
    // the 409 copy itself is the localized apiErrors entry — referrer-neutral since N17-obs3
    expect(en.apiErrors["agent.in_use"]).toContain("canary");
    expect(en.apiErrors["agent.in_use"]).toContain("evaluation run");
    expect(en.apiErrors["agent.in_use"]).not.toContain("deleting");
    // N17-obs3: the dialog is the message — no generic error toast above it
    expect(toasts.filter((x) => x.tone === "error")).toEqual([]);
  });
});

describe("AgentDetail delete refused by an ACTIVE evaluation run (N17-obs18)", () => {
  it("keeps the dialog open, names the run and links to the run detail page", async () => {
    agent = { ...base, created_by: null, created_by_name: null };
    deleteAgentError = new ApiError("agent.in_use", "1 evaluation run(s) on this agent are still active", {
      canaries: [],
      runs: [{ id: "run1", name: "nightly", status: "queued" }],
    });
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-agent-delete")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-agent-delete"));
    await waitFor(() => expect(screen.getByTestId("v2-confirm-ok")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-confirm-ok"));
    await waitFor(() => expect(screen.getByTestId("v2-agent-delete-blocked")).toBeTruthy());
    expect(screen.getByTestId("v2-confirm-ok")).toBeTruthy();
    expect(screen.getByTestId("v2-agent-delete-blocked").textContent).toContain("evaluation run");
    expect(screen.getByTestId("v2-agent-delete-blocked").textContent).not.toContain("Runtime canary");
    const link = screen.getByTestId("v2-agent-delete-blocked-run-run1") as HTMLAnchorElement;
    expect(link.textContent).toContain("nightly");
    expect(link.textContent).toContain("Queued"); // N17-obs457: the run status is worded
    expect(link.getAttribute("href")).toBe("/v2/eval/tasks?view=detail&id=run1");
    // N17-obs3 (deploy-ops-14 T88): no canary-only toast above the dialog that lists the run
    expect(toasts.filter((x) => x.tone === "error")).toEqual([]);
  });

  it("still toasts an unrelated delete failure (the dialog has nothing to list)", async () => {
    agent = { ...base, created_by: null, created_by_name: null };
    deleteAgentError = new ApiError("agent.deploy_in_progress", "a deployment is in progress", {
      job_id: "j1", stage: "deploy", job_status: "running",
    });
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-agent-delete")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-agent-delete"));
    await waitFor(() => expect(screen.getByTestId("v2-confirm-ok")).toBeTruthy());
    fireEvent.click(screen.getByTestId("v2-confirm-ok"));
    await waitFor(() => expect(toasts.filter((x) => x.tone === "error")).toHaveLength(1));
    expect(screen.queryByTestId("v2-agent-delete-blocked")).toBeNull();
  });
});

describe("AgentDetail user-token carrier (Global parity, amendment 9)", () => {
  it("shows 携带用户令牌 yes / no from policy_carrier and nothing when the field is null", async () => {
    agent = { ...base, policy_carrier: true };
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-agent-policy-carrier")).toBeTruthy());
    expect(screen.getByTestId("v2-agent-policy-carrier").textContent).toBe(en.v2.agents.policyCarrierYes);
    // the retired coverage tag never renders
    expect(screen.queryByTestId("v2-agent-policy-coverage")).toBeNull();
    cleanup();
    agent = { ...base, policy_carrier: false };
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-agent-policy-carrier").textContent).toBe(en.v2.agents.policyCarrierNo));
    cleanup();
    agent = { ...base, policy_carrier: null };
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-agent-basic")).toBeTruthy());
    expect(screen.queryByTestId("v2-agent-policy-carrier")).toBeNull();
  });
});

describe("AgentDetail kb-stale notice (N17-obs31 / N17-obs5)", () => {
  it("is locatable by its testid and names the stripped knowledge base", async () => {
    agent = { ...base, kb_stale: true, last_strip: { kb_ids: ["KBGONE"], at: "2026-10-20T00:00:00Z", by: "root" } };
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    // N17-obs5: Alert used to drop `data-testid`, so this lookup failed live (deploy-ops-15)
    await waitFor(() => expect(screen.getByTestId("v2-agent-kb-stale")).toBeTruthy());
    expect(screen.getByTestId("v2-agent-kb-stale").textContent).toContain("KBGONE");
    expect(screen.getByTestId("v2-agent-kb-stale").textContent).toContain("Re-publish");
  });
});

describe("AgentDetail Refresh after a delete (N16-obs76)", () => {
  it("replaces the stale ACTIVE view with the not-found notice when the refresh answers 404", async () => {
    agent = { ...base, created_by: null, created_by_name: null };
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-agent-delete")).toBeTruthy());
    expect(screen.getByText(en.status.active)).toBeTruthy();
    // the agent is deleted elsewhere; the detail's Refresh re-reads it → 404 agent.not_found
    getAgentError = new ApiError("agent.not_found", "agent not found", null);
    fireEvent.click(screen.getByText(en.v2.common.refresh));
    await waitFor(() => expect(screen.getByRole("alert")).toBeTruthy());
    expect(screen.getByRole("alert").textContent?.toLowerCase()).toContain("agent not found");
    expect(screen.queryByText(en.status.active)).toBeNull();
    expect(screen.queryByTestId("v2-agent-delete")).toBeNull();
  });
});

describe("AgentDetail creator-or-admin mirror (N15-obs9)", () => {
  it("disables delete / edit with the creator-only hint for a non-creator member", async () => {
    agent = { ...base };
    mount(auth({}));
    await waitFor(() => expect(screen.getByTestId("v2-agent-edit")).toBeTruthy());
    expect((screen.getByTestId("v2-agent-edit") as HTMLButtonElement).disabled).toBe(true);
    expect((screen.getByTestId("v2-agent-delete") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("v2-agent-edit").getAttribute("title")).toBe(en.v2.common.creatorOnly);
    expect(screen.getByTestId("v2-agent-creator-only").textContent).toContain("ada");
  });

  it("keeps them enabled for the creator and for an administrator", async () => {
    agent = { ...base };
    mount(auth({ username: "ada", userId: "u-ada" }));
    await waitFor(() => expect(screen.getByTestId("v2-agent-edit")).toBeTruthy());
    expect((screen.getByTestId("v2-agent-edit") as HTMLButtonElement).disabled).toBe(false);
    expect((screen.getByTestId("v2-agent-delete") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByTestId("v2-agent-creator-only")).toBeNull();
    cleanup();
    agent = { ...base, created_by: null, created_by_name: null }; // a NULL-creator agent is an administrator's
    mount(auth({ username: "root", userId: null, role: "admin", isAdmin: true }));
    await waitFor(() => expect(screen.getByTestId("v2-agent-edit")).toBeTruthy());
    expect((screen.getByTestId("v2-agent-delete") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.queryByTestId("v2-agent-creator-only")).toBeNull();
  });

  it("treats a NULL-creator agent as an administrator's for a member", async () => {
    agent = { ...base, created_by: null, created_by_name: null };
    mount(auth({}));
    await waitFor(() => expect(screen.getByTestId("v2-agent-edit")).toBeTruthy());
    expect((screen.getByTestId("v2-agent-edit") as HTMLButtonElement).disabled).toBe(true);
    expect(screen.getByTestId("v2-agent-creator-only")).toBeTruthy();
  });
});

/** N17-obs457: the 版本与端点 table showed the runtime version and the DEFAULT endpoint status as
 *  the raw AgentCore enum 「READY」 on the zh console; both now read through v2.awsStatus. */
describe("AgentDetail versions and endpoints statuses (N17-obs457)", () => {
  it("words the version and endpoint statuses, the failure reason kept on hover", async () => {
    agent = { ...base, arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/demo-x", resource_id: "demo-x" };
    versions = {
      kind: "runtime", resource_id: "demo-x", latest_version: "2", ledger_version: "2", canary_endpoints: [],
      versions: [{ version: "2", status: "READY", last_updated_at: null }, { version: "1", status: "CREATE_FAILED", last_updated_at: null }],
      endpoints: [{ name: "DEFAULT", live_version: "2", target_version: null, status: "UPDATING", failure_reason: "busy" }],
    };
    mount(auth({ userId: "u-ada", username: "ada" }));
    const card = await screen.findByTestId("v2-agent-versions");
    await waitFor(() => expect(card.textContent).toContain("Ready"));
    expect(card.textContent).toContain("Create failed");
    expect(card.textContent).toContain("Updating");
    for (const raw of ["READY", "CREATE_FAILED", "UPDATING"]) expect(card.textContent).not.toContain(raw);
    expect(screen.getByText("Updating").getAttribute("title")).toBe("busy");
  });
});

describe("AgentDetail deploy panel statuses (N17-obs457)", () => {
  it("names every stage state and the deploy stage's leading READY, the log left as data", async () => {
    agent = {
      ...base, status: "active", arn: "arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/demo-x", resource_id: "demo-x",
      deployments: [{
        job_id: "job-12345678", status: "succeeded",
        stages: [
          { name: "generate", status: "succeeded", detail: "harness template · 14 files" },
          { name: "deploy", status: "succeeded", detail: "READY · arn:aws-cn:bedrock-agentcore:cn-northwest-1:1:runtime/demo-x" },
          { name: "register", status: "cancelled", detail: "" },
        ],
      }],
    } as unknown as AgentInfo;
    mount(auth({ userId: "u-ada", username: "ada" }));
    const panel = await screen.findByTestId("v2-agent-deploy");
    expect(panel.textContent).toContain("Ready · arn:aws-cn:bedrock-agentcore");
    expect(panel.textContent).toContain(en.v2.agents.stage.cancelled);
    const stages = panel.querySelector(".v2-stages")?.textContent ?? "";
    for (const raw of ["READY", "SUCCEEDED", "CANCELLED", "RUNNING"]) expect(stages).not.toContain(raw);
  });
});
