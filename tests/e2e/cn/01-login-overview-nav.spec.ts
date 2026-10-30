import { expect, test } from "@playwright/test";
import { attach, evidence, ledger, rawI18nKeys, setLanguage } from "./helpers";

// feature → the V2 nav routes that belong to it (frontend/src/v2/nav.ts). Round 4: the
// catalogue is read from GET /api/capabilities (13 on default after N12/N13), never a literal.
const FEATURE_PATHS: Record<string, string[]> = {
  agents: ["/v2/agents"], registry: ["/v2/registry"], knowledge_bases: ["/v2/knowledge-bases"],
  chat: ["/v2/chat"], memory: ["/v2/memory"], identity: ["/v2/connections"], gateway: ["/v2/governance"],
  observability: ["/v2/observability"], workspaces: ["/v2/workspaces"], users: ["/v2/users"],
  evaluation: ["/v2/eval/insights", "/v2/eval/data", "/v2/eval/tasks", "/v2/eval/online", "/v2/eval/evaluators"],
  optimization: ["/v2/eval/experiments"], skill_lab: ["/v2/skill-lab"],
};
const ALL_FEATURE_PATHS = Object.values(FEATURE_PATHS).flat();

/**
 * Case 1 — Login → Overview; the sidebar shows exactly the modules that GET /api/capabilities
 * lists (one nav entry group per feature), nothing else — once in en, once in zh-CN.
 * zh-CN half (§4.1): no visible raw i18n key on any console page.
 */
test("case 1: login lands on the console and the navigation matches /api/capabilities (en + zh-CN)", async ({ page }, testInfo) => {
  ledger("case1 start");
  const caps = await (await page.request.get("/api/capabilities")).json();
  const features = caps.features as string[];
  ledger(`case1 capabilities=${features.length} ${JSON.stringify(features)}`);
  for (const f of features) expect(FEATURE_PATHS, `nav route map knows feature ${f}`).toHaveProperty(f);
  expect(features.length).toBeGreaterThanOrEqual(10);

  for (const lang of ["en", "zh-CN"] as const) {
    await page.goto("/");
    if (lang === "zh-CN") await setLanguage(page, "zh-CN");
    await expect(page.getByTestId("v2-shell")).toBeVisible();
    await expect(page).toHaveURL(/\/v2\/?$/);
    await expect(page.getByTestId("v2-kpi-agents")).toBeVisible();
    const side = page.locator("aside.v2-side");
    const links = side.locator("a[href]");
    const hrefs = (await links.evaluateAll((as) => as.map((a) => (a as HTMLAnchorElement).getAttribute("href") ?? ""))).map((h) => h.split("?")[0]);
    const labels = (await links.allInnerTexts()).map((l) => l.trim()).filter(Boolean);
    for (const f of features) expect(FEATURE_PATHS[f].some((p) => hrefs.includes(p)), `[${lang}] nav missing module for feature ${f}`).toBe(true);
    const navFeatures = new Set(Object.entries(FEATURE_PATHS).filter(([, paths]) => paths.some((p) => hrefs.includes(p))).map(([f]) => f));
    expect([...navFeatures].sort(), `[${lang}] nav features == /api/capabilities`).toEqual([...features].sort());
    for (const gone of ["/evaluation", "/skills", "/policy", "/harness", "/governance/policies"]) {
      expect(hrefs.some((h) => h.startsWith(gone)), `[${lang}] removed module ${gone} still in nav`).toBe(false);
    }
    const allowed = new Set(["/v2", ...ALL_FEATURE_PATHS, "/v2/announcements", "/v2/assistant"]);
    expect(hrefs.filter((h) => !allowed.has(h)), `[${lang}] unexpected nav entries`).toEqual([]);
    await evidence(page, `case01-overview-nav-${lang}`);
    ledger(`case1 ${lang} nav=${JSON.stringify(labels)}`);

    if (lang === "zh-CN") {
      // §4.1: visible text matching ^[a-z0-9_.]+\.[a-z0-9_.]+$ must be 0 on every page
      const pages = ["/v2", ...features.map((f) => FEATURE_PATHS[f][0])];
      const found: Record<string, string[]> = {};
      for (const p of pages) {
        await page.goto(p);
        await page.waitForLoadState("networkidle").catch(() => {});
        await page.waitForTimeout(800);
        const keys = await rawI18nKeys(page);
        if (keys.length) found[p] = keys;
        ledger(`case1 zh-CN ${p} rawKeys=${keys.length}${keys.length ? " " + JSON.stringify(keys.slice(0, 8)) : ""}`);
      }
      await page.goto("/v2/agents");
      await evidence(page, "case01-zh-agents");
      await attach(testInfo, "zh-raw-i18n-keys.json", found);
      expect(found, "zh-CN pages with raw i18n keys").toEqual({});
      // Chinese copy actually rendered (not English fallback)
      await page.goto("/v2");
      const body = await page.locator("body").innerText();
      expect(/[一-鿿]/.test(body), "zh-CN UI should render CJK text").toBe(true);
      await setLanguage(page, "en");
    }
  }
  ledger("case1 end PASS");
});
