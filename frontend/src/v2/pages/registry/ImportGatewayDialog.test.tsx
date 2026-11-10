// @vitest-environment jsdom
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import { AuthContext, type AuthContextValue } from "../../../auth/auth-context";
import type { GatewayImportPreview, GatewayImportResponse } from "../../../lib/api";
import en from "../../../locales/en/common.json";
import zh from "../../../locales/zh-CN/common.json";
import { IMPORTED, PREVIEW } from "../../../test-fixtures/gateway-import";
import { ImportGatewayDialog } from "./ImportGatewayDialog";
import { RecordList } from "./RecordList";

/**
 * WB-005: Registry → "Import Gateway targets" (V2). The button opens a dialog that lists
 * the onboarded Gateways' targets (all ticked), imports exactly the ticked ones through
 * `POST /api/registry/import-gateway`, shows one result row per target and asks the
 * caller to refresh the list when it closes after an import.
 */

const preview = vi.fn(async (): Promise<GatewayImportPreview> => PREVIEW);
type ImportBody = { gateway_id?: string; target_ids?: string[] };
const importGateway = vi.fn<(body: ImportBody) => Promise<GatewayImportResponse>>(async () => IMPORTED);
vi.mock("../../../lib/api", async (importOriginal) => {
  const actual = await importOriginal<typeof import("../../../lib/api")>();
  return {
    ...actual,
    api: {
      ...actual.api,
      registryImportGatewayPreview: () => preview(),
      registryImportGateway: (body: ImportBody) => importGateway(body),
      registryRecords: vi.fn(async () => ({ records: [] })),
      registryDiscoverable: vi.fn(async () => ({ records: [], count: 0 })),
    },
  };
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
  preview.mockClear();
  importGateway.mockClear();
});

describe("V2 ImportGatewayDialog", () => {
  it("lists the platform gateway's targets ticked by default, imports only the ticked ones and reports per item", async () => {
    const onClose = vi.fn();
    render(<ImportGatewayDialog open onClose={onClose} />);
    expect(screen.getByTestId("v2-import-gw")).toBeTruthy();
    expect(screen.getByText(en.registry.importGw.title)).toBeTruthy();

    const office = (await screen.findByTestId("v2-import-gw-pick-office-facts")) as HTMLInputElement;
    const weather = screen.getByTestId("v2-import-gw-pick-weather") as HTMLInputElement;
    expect(office.checked && weather.checked).toBe(true);
    expect(screen.getByTestId("v2-import-gw-selected").textContent).toBe("2 of 2 target(s) selected");
    expect(screen.getByText(en.registry.importGw.platformTag)).toBeTruthy();
    expect(screen.getByText(en.registry.importGw.platformNote)).toBeTruthy();
    // the already-imported target is labelled so, with its record status
    expect(screen.getByText(`${en.registry.importGw.state.imported} · ${en.v2.registry.status.APPROVED}`)).toBeTruthy();

    fireEvent.click(weather);
    expect(screen.getByTestId("v2-import-gw-selected").textContent).toBe("1 of 2 target(s) selected");
    const confirm = screen.getByTestId("v2-import-gw-confirm");
    expect(confirm.textContent).toBe("Import 1 target(s)");
    fireEvent.click(confirm);

    await waitFor(() => expect(importGateway).toHaveBeenCalledTimes(1));
    expect(importGateway).toHaveBeenCalledWith({ gateway_id: "gw-1", target_ids: ["tgt0000001"] });
    await screen.findByTestId("v2-import-gw-summary");
    expect(screen.getByTestId("v2-import-gw-summary").textContent).toBe(
      "1 created · 1 already imported · 0 reconciled · 0 failed",
    );
    expect(screen.getByTestId("v2-import-gw-result-office-facts").textContent).toBe(
      `${en.registry.importGw.state.created} · ${en.v2.registry.status.APPROVED}`,
    );
    expect(screen.getByTestId("v2-import-gw-result-weather").textContent).toBe(
      `${en.registry.importGw.state.already} · ${en.v2.registry.status.APPROVED}`,
    );
    expect(screen.getByText("rec-office")).toBeTruthy();

    fireEvent.click(screen.getByTestId("v2-import-gw-done"));
    expect(onClose).toHaveBeenCalledWith(true); // → the caller refreshes the list
  });

  it("cancelling before an import closes without a refresh; nothing is selected → confirm disabled", async () => {
    const onClose = vi.fn();
    render(<ImportGatewayDialog open onClose={onClose} />);
    await screen.findByTestId("v2-import-gw-pick-office-facts");
    fireEvent.click(screen.getByText(en.registry.importGw.selectNone));
    expect((screen.getByTestId("v2-import-gw-confirm") as HTMLButtonElement).disabled).toBe(true);
    fireEvent.click(screen.getByText(en.v2.common.cancel));
    expect(onClose).toHaveBeenCalledWith(false);
    expect(importGateway).not.toHaveBeenCalled();
  });

  it("shows a failed import honestly and keeps the dialog open", async () => {
    importGateway.mockRejectedValueOnce(new Error("boom"));
    render(<ImportGatewayDialog open onClose={vi.fn()} />);
    await screen.findByTestId("v2-import-gw-pick-office-facts");
    fireEvent.click(screen.getByTestId("v2-import-gw-confirm"));
    await screen.findByText("Import failed: boom");
    expect(screen.getByTestId("v2-import-gw-confirm")).toBeTruthy();
  });

  it("renders a failed result row with its reason and a non-IAM gateway's auth warning", async () => {
    importGateway.mockResolvedValueOnce({
      ...IMPORTED,
      gateway_id: "gw-2",
      gateway_name: "partner-gateway",
      auth: "oauth",
      platform: false,
      results: [
        {
          ...IMPORTED.results[0],
          name: "crm",
          target: "crm",
          target_id: "tgt0000003",
          gateway_id: "gw-2",
          record_id: "rec-held",
          created: false,
          status: "DRAFT",
          tool_source: "schema",
          auth_warning: "unmapped auth",
          error: "name 'crm' is held by a record that is not this gateway's",
          error_code: "registry.name_held",
        },
      ],
    });
    render(<ImportGatewayDialog open onClose={vi.fn()} />);
    await screen.findByTestId("v2-import-gw-pick-office-facts");
    fireEvent.click(screen.getByTestId("v2-import-gw-confirm"));
    await screen.findByTestId("v2-import-gw-summary");
    expect(screen.getByTestId("v2-import-gw-summary").textContent).toBe(
      "0 created · 0 already imported · 0 reconciled · 1 failed",
    );
    expect(screen.getByTestId("v2-import-gw-result-crm").textContent).toBe(`${en.registry.importGw.state.failed} · ${en.v2.registry.status.DRAFT}`);
    expect(screen.getByText("name 'crm' is held by a record that is not this gateway's")).toBeTruthy();
  });
});

/** N17-obs250: the t1 gateway's targets collide with default's platform records. */
const HELD: GatewayImportPreview = {
  gateways: [
    {
      ...PREVIEW.gateways[1],
      gateway_id: "gw-t1",
      name: "launchpad-gw-t1",
      targets: [
        {
          ...PREVIEW.gateways[1].targets[0], target_id: "tgt-hr", target: "hr-database",
          record_id: "rec-hr", status: "APPROVED", held: true,
          held_by: { record_id: "rec-hr", name: "hr-database", status: "APPROVED", origin: "gateway",
                     gateway_id: "gw-1", gateway_name: "launchpad-gw" },
        },
        {
          ...PREVIEW.gateways[1].targets[0], target_id: "tgt-crm", target: "crm",
          record_id: "rec-crm", status: "DRAFT", held: true,
          held_by: { record_id: "rec-crm", name: "crm", status: "DRAFT", origin: "member" },
        },
        { ...PREVIEW.gateways[0].targets[1] }, // this gateway's own record → 已导入
      ],
    },
  ],
};

describe("V2 ImportGatewayDialog — who holds a name (N17-obs250)", () => {
  it("names the holding gateway or member server and links the held record", async () => {
    preview.mockResolvedValueOnce(HELD);
    const onClose = vi.fn();
    const onOpenRecord = vi.fn();
    render(<ImportGatewayDialog open onClose={onClose} onOpenRecord={onOpenRecord} />);
    const byGateway = await screen.findByTestId("v2-import-gw-held-hr-database");
    expect(byGateway.textContent).toBe(
      `Name held by the MCP record of gateway launchpad-gw (${en.v2.registry.status.APPROVED}) — cannot import again`,
    );
    expect(screen.getByTestId("v2-import-gw-held-crm").textContent).toBe(
      `Name held by the member-registered MCP server crm (${en.v2.registry.status.DRAFT}) — cannot import again`,
    );
    expect(screen.getByTestId("v2-import-gw-held-link-crm").textContent).toBe("crm");
    expect(screen.getByTestId("v2-import-gw-held-link-hr-database").textContent).toBe("record");
    expect(screen.getByText(`${en.registry.importGw.state.imported} · ${en.v2.registry.status.APPROVED}`)).toBeTruthy();
    fireEvent.click(screen.getByTestId("v2-import-gw-held-link-hr-database"));
    expect(onClose).toHaveBeenCalledWith(false);
    expect(onOpenRecord).toHaveBeenCalledWith("rec-hr");
  });

  it("renders the zh copy with full-width punctuation", async () => {
    await i18next.changeLanguage("zh-CN");
    preview.mockResolvedValueOnce(HELD);
    render(<ImportGatewayDialog open onClose={vi.fn()} onOpenRecord={vi.fn()} />);
    const byGateway = await screen.findByTestId("v2-import-gw-held-hr-database");
    const status = zh.v2.registry.status.APPROVED;
    expect(byGateway.textContent).toBe(`名称已被网关 launchpad-gw 的 MCP 记录占用（${status}），不能重复导入`);
    expect(screen.getByTestId("v2-import-gw-held-crm").textContent).toBe(
      `名称已被成员注册的 MCP 服务器 crm 占用（${zh.v2.registry.status.DRAFT}），不能重复导入`,
    );
    expect(screen.getByText(`${zh.registry.importGw.state.imported} · ${zh.v2.registry.status.APPROVED}`)).toBeTruthy();
  });

  it("without a host page to open records the holder is plain text", async () => {
    preview.mockResolvedValueOnce(HELD);
    render(<ImportGatewayDialog open onClose={vi.fn()} />);
    await screen.findByTestId("v2-import-gw-held-crm");
    expect(screen.queryByTestId("v2-import-gw-held-link-crm")).toBeNull();
  });
});

const MEMBER: AuthContextValue = {
  authRequired: true, username: "bob", userId: "u-bob", role: "member", email: null,
  accountExpiresAt: null, isAdmin: false, can: () => true, logout: async () => undefined,
};

describe("V2 ImportGatewayDialog — member-triggered platform import (N17-obs257)", () => {
  it("tells a member that platform records they import await administrator approval", async () => {
    importGateway.mockResolvedValueOnce({
      ...IMPORTED,
      auto_approved: false,
      results: IMPORTED.results.map((r) => (r.created ? { ...r, status: "PENDING_APPROVAL" } : r)),
    });
    render(
      <AuthContext.Provider value={MEMBER}>
        <ImportGatewayDialog open onClose={vi.fn()} />
      </AuthContext.Provider>,
    );
    await screen.findByTestId("v2-import-gw-pick-office-facts");
    expect(screen.getByText(en.registry.importGw.platformNoteMember)).toBeTruthy();
    expect(screen.queryByText(en.registry.importGw.platformNote)).toBeNull();
    fireEvent.click(screen.getByTestId("v2-import-gw-confirm"));
    const awaiting = await screen.findByTestId("v2-import-gw-awaiting");
    expect(awaiting.textContent).toBe("1 new record(s) are waiting for administrator approval.");
  });

  it("an administrator keeps the auto-approval note and sees no waiting notice", async () => {
    render(<ImportGatewayDialog open onClose={vi.fn()} />); // default context = admin
    await screen.findByTestId("v2-import-gw-pick-office-facts");
    expect(screen.getByText(en.registry.importGw.platformNote)).toBeTruthy();
    fireEvent.click(screen.getByTestId("v2-import-gw-confirm"));
    await screen.findByTestId("v2-import-gw-summary");
    expect(screen.queryByTestId("v2-import-gw-awaiting")).toBeNull();
  });
});

describe("V2 RecordList — the Import Gateway targets button", () => {
  it("opens the dialog instead of navigating away", async () => {
    render(
      <MemoryRouter initialEntries={["/v2/registry"]}>
        <RecordList />
      </MemoryRouter>,
    );
    const button = await screen.findByTestId("v2-registry-import-gateway");
    expect(button.textContent).toBe(en.v2.registry.importGateway);
    expect(button.getAttribute("title")).toBe(en.v2.registry.importGatewayHint);
    expect(screen.queryByTestId("v2-import-gw")).toBeNull();
    fireEvent.click(button);
    expect(screen.getByTestId("v2-import-gw")).toBeTruthy();
    await waitFor(() => expect(preview).toHaveBeenCalledTimes(1));
    await screen.findByTestId("v2-import-gw-pick-office-facts");
  });
});

/** N17-obs261: two member-submitted platform rows and a platform DRAFT, as an administrator's
 *  preview lists them (`will_approve`, `origin`, `endpoint`). */
const APPROVING: GatewayImportPreview = {
  gateways: [
    {
      ...PREVIEW.gateways[0],
      will_approve: 3,
      member_submissions_to_approve: 2,
      targets: [
        {
          ...PREVIEW.gateways[0].targets[0], record_id: "rec-office", status: "PENDING_APPROVAL",
          imported: true, origin: "member", will_approve: true,
          endpoint: "https://member.example/mcp", endpoint_kind: "mcpServer",
        },
        {
          ...PREVIEW.gateways[0].targets[1], status: "PENDING_APPROVAL", origin: "member",
          will_approve: true, endpoint: "arn:aws-cn:lambda:cn-northwest-1:111122223333:function:weather",
          endpoint_kind: "lambda",
        },
        {
          ...PREVIEW.gateways[0].targets[1], target_id: "tgt0000009", target: "old-draft",
          record_id: "rec-draft", status: "DRAFT", origin: "platform", will_approve: true,
          endpoint: null, endpoint_kind: "openApiSchema",
        },
      ],
    },
  ],
};

describe("V2 ImportGatewayDialog — what an administrator's import approves (N17-obs261)", () => {
  it("tags every row the import approves, names member submissions and endpoints, and counts the ticked ones", async () => {
    preview.mockResolvedValueOnce(APPROVING);
    render(<ImportGatewayDialog open onClose={vi.fn()} />); // default context = admin
    await screen.findByTestId("v2-import-gw-pick-office-facts");
    for (const name of ["office-facts", "weather", "old-draft"]) {
      expect(screen.getByTestId(`v2-import-gw-will-approve-${name}`).textContent).toBe(
        en.registry.importGw.willApprove,
      );
    }
    expect(screen.getByTestId("v2-import-gw-origin-office-facts").textContent).toBe(
      en.registry.importGw.origin.member,
    );
    expect(screen.getByTestId("v2-import-gw-origin-old-draft").textContent).toBe(
      en.registry.importGw.origin.platform,
    );
    expect(screen.getByTestId("v2-import-gw-endpoint-office-facts").textContent).toBe(
      "https://member.example/mcp · mcpServer",
    );
    expect(screen.getByTestId("v2-import-gw-endpoint-old-draft").textContent).toBe("— · openApiSchema");
    // the one-line summary above the table counts the TICKED member submissions
    expect(screen.getByTestId("v2-import-gw-member-approvals").textContent).toBe(
      "2 record(s) submitted by members will be approved by this import.",
    );
    fireEvent.click(screen.getByTestId("v2-import-gw-pick-office-facts"));
    expect(screen.getByTestId("v2-import-gw-member-approvals").textContent).toBe(
      "1 record(s) submitted by members will be approved by this import.",
    );
    fireEvent.click(screen.getByTestId("v2-import-gw-pick-weather"));
    expect(screen.queryByTestId("v2-import-gw-member-approvals")).toBeNull(); // the DRAFT is not a member's
  });

  it("renders the zh copy with full-width punctuation", async () => {
    await i18next.changeLanguage("zh-CN");
    preview.mockResolvedValueOnce(APPROVING);
    render(<ImportGatewayDialog open onClose={vi.fn()} />);
    await screen.findByTestId("v2-import-gw-pick-office-facts");
    expect(screen.getByTestId("v2-import-gw-will-approve-weather").textContent).toBe("将被批准");
    expect(screen.getByTestId("v2-import-gw-origin-weather").textContent).toBe("成员提交");
    expect(screen.getByTestId("v2-import-gw-member-approvals").textContent).toBe(
      "本次导入将批准 2 条由成员提交的记录。",
    );
  });

  it("a member's preview (nothing will_approve) shows no approval summary or tag", async () => {
    preview.mockResolvedValueOnce({
      gateways: [{
        ...APPROVING.gateways[0], will_approve: 0, member_submissions_to_approve: 0,
        targets: APPROVING.gateways[0].targets.map((t) => ({ ...t, will_approve: false })),
      }],
    });
    render(
      <AuthContext.Provider value={MEMBER}>
        <ImportGatewayDialog open onClose={vi.fn()} />
      </AuthContext.Provider>,
    );
    await screen.findByTestId("v2-import-gw-pick-office-facts");
    expect(screen.queryByTestId("v2-import-gw-member-approvals")).toBeNull();
    expect(screen.queryByTestId("v2-import-gw-will-approve-office-facts")).toBeNull();
    expect(screen.getByTestId("v2-import-gw-origin-office-facts")).toBeTruthy();
  });
});
