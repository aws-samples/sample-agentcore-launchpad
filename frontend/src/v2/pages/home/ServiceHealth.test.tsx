// @vitest-environment jsdom
import { cleanup, render, screen } from "@testing-library/react";
import i18next from "i18next";
import { initReactI18next } from "react-i18next";
import { MemoryRouter } from "react-router-dom";
import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

import type { OverviewInfo } from "../../../lib/api";
import type { Feature } from "../../../lib/capabilities";
import en from "../../../locales/en/common.json";
import { ServiceHealth } from "./ServiceHealth";

// the policy row follows `GET /api/capabilities.features` (D20): off unless told otherwise
let features: Set<Feature> | null = new Set<Feature>(["gateway"]);
vi.mock("../../../lib/capabilities", async (importOriginal) => ({
  ...(await importOriginal<typeof import("../../../lib/capabilities")>()),
  useFeatures: () => ({ features, loading: features === null }),
}));

/**
 * WB-001: the home card said "run your first evaluation" next to a 15-run / 85 % tile
 * because `/api/overview` carried no `services.evaluation`. The backend now emits the
 * row; this pins the card's reading of it with the real en copy. r7-e: the detail is
 * structured and formatted client-side. D20: a `policy` row is back — for the self-hosted
 * Cedar module, rendered only when the `policy` feature is on (§2.6).
 */

const OFF: OverviewInfo = {
  registry_assets: { agents: 0, tools: 0, skills: 0, total: 0 },
  active_sessions: 0,
  eval_pass_rate: null,
  eval_runs: 0,
  services: {
    gateway: true,
    memory: true,
    registry: true,
    evaluation: false,
    observability: true,
  },
  service_detail: {
    gateway: "gw-1",
    memory: "mem-1",
    registry: "reg-1",
    evaluation: null,
    observability: "aws/spans",
  },
};

function renderCard(info: OverviewInfo) {
  return render(
    <MemoryRouter>
      <ServiceHealth info={info} error={null} onRetry={() => undefined} activeAgents={2} />
    </MemoryRouter>,
  );
}

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

describe("ServiceHealth (WB-001)", () => {
  it("reads READY for the evaluation row when services.evaluation is true", () => {
    renderCard({
      ...OFF,
      eval_runs: 15,
      eval_pass_rate: 0.85,
      services: { ...OFF.services, evaluation: true },
      service_detail: { ...OFF.service_detail, evaluation: { runs: 15, pass_rate: 0.85 } },
    });
    const row = screen.getByTestId("v2-health-evaluation");
    expect(row.textContent).toContain(en.v2.home.health.ready);
    expect(row.textContent).not.toContain(en.v2.home.health.creates.evaluation);
    // the structured detail is formatted in the viewer's language as the row's tooltip
    expect(row.querySelector(".state")?.getAttribute("title")).toBe("15 runs · 85 % pass");
    expect(row.querySelector(".dot")?.className).toContain("ok");
  });

  it("keeps the 'run your first evaluation' hint while nothing has completed", () => {
    renderCard(OFF);
    const row = screen.getByTestId("v2-health-evaluation");
    expect(row.textContent).toContain(en.v2.home.health.creates.evaluation);
    expect(row.querySelector(".dot")?.className).toContain("idle");
  });

  it("lists no policy row without the policy feature (the AgentCore service is absent in cn)", () => {
    features = new Set<Feature>(["gateway"]);
    renderCard(OFF);
    expect(screen.queryByTestId("v2-health-policy")).toBeNull();
    expect(screen.getAllByRole("listitem").map((li) => li.getAttribute("data-testid"))).toEqual([
      "v2-health-runtime",
      "v2-health-gateway",
      "v2-health-memory",
      "v2-health-registry",
      "v2-health-evaluation",
      "v2-health-observability",
    ]);
    // while the capabilities are still loading the row waits rather than flashing in
    features = null;
    cleanup();
    renderCard(OFF);
    expect(screen.queryByTestId("v2-health-policy")).toBeNull();
  });

  // Global structure (2026-10-28): the policy row shows with the `policy` feature and opens the
  // Policy page; `services.policy` / `service_detail.policy` come from the backend, read as-is
  it("renders the policy row with the feature, linked to /v2/policy", () => {
    features = new Set<Feature>(["gateway", "policy"]);
    renderCard({ ...OFF, services: { ...OFF.services, policy: true }, service_detail: { ...OFF.service_detail, policy: "1 engine · 2 gateways" } });
    const row = screen.getByTestId("v2-health-policy");
    expect(row.querySelector("a")?.getAttribute("href")).toBe("/v2/policy");
    expect(row.textContent).toContain(en.v2.home.health.ready);
    expect(row.querySelector(".state")?.getAttribute("title")).toBe("1 engine · 2 gateways");
  });
});
