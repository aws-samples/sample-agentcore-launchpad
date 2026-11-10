/**
 * Pure parts of the UI random walk (round 16, r16-n step 3): which controls a step may
 * click, what counts as "an effect", and the URL ↔ page consistency rules. The Playwright
 * driver (r1-random-walk.spec.ts) feeds it DOM snapshots; everything here is unit-tested.
 */

/** A control as the driver inventories it from the live DOM. */
export interface Control {
  /** stable id for the step log: testid, else role+text */
  id: string;
  kind: "button" | "link" | "tab" | "radio" | "checkbox" | "text" | "select" | "listbox" | "option";
  text: string;
  /** innerText of the enclosing table row / card, when any */
  rowText: string;
  href?: string | null;
  disabled: boolean;
  /** inside an open dialog (Confirm / Modal) */
  inDialog: boolean;
  /** the `data-x16n` handle the driver stamped on the element (runtime only) */
  handle?: string;
  /** an already-selected option card / tab / radio: clicking it legitimately does nothing */
  alreadyOn?: boolean;
}

/** Labels that destroy or stop something — allowed only on x16n- fixtures. */
export const DESTRUCTIVE_RE = /\b(delete|remove|stop|pause|purge|reject|disable|uninstall|teardown|revoke|sign out|log ?out|end session|clear)\b|删除|移除|停止|暂停|清除|注销|退出/i;
/** Controls the walk never touches: leave the console, spend money, or mutate shared state. */
export const FORBIDDEN_RE = /\b(sign out|log ?out|deploy|redeploy|re-?publish|install|bootstrap|import|register|publish|approve|promote|rollback|create agent|new agent|start)\b|^(中文|EN)$|退出登录|部署|重新部署|发布|安装|导入|注册|批准/i;

/** The fixture marker: a row / dialog mentioning it may be mutated. */
export const FIXTURE_RE = /x16n-/;

export function isClickable(c: Control, modulePrefix: string): { ok: boolean; why?: string } {
  if (c.disabled) return { ok: false, why: "disabled" };
  if (c.alreadyOn) return { ok: false, why: "already selected" };
  if (c.kind === "link") {
    const href = c.href ?? "";
    // stay inside the module (and inside the console)
    if (/^https?:\/\//.test(href) && !href.includes("/v2/")) return { ok: false, why: "external link" };
    const pathPart = href.replace(/^https?:\/\/[^/]+/, "");
    if (pathPart && !pathPart.startsWith(modulePrefix) && !pathPart.startsWith("?")) return { ok: false, why: "leaves module" };
  }
  if (FORBIDDEN_RE.test(c.text)) return { ok: false, why: "forbidden action" };
  if (DESTRUCTIVE_RE.test(c.text)) {
    if (!FIXTURE_RE.test(c.rowText)) return { ok: false, why: "destructive outside a fixture row" };
    return { ok: true };
  }
  if (c.inDialog && /confirm|ok|yes|确认|确定/i.test(c.text) && !FIXTURE_RE.test(c.rowText)) return { ok: false, why: "confirm outside a fixture dialog" };
  return { ok: true };
}

export interface Snapshot {
  url: string;
  textHash: string;
  elementCount: number;
  requests: number;
  openDialogs: number;
}

/** A click had an effect when something observable changed. */
export function effectOf(before: Snapshot, after: Snapshot): string[] {
  const out: string[] = [];
  if (before.url !== after.url) out.push("url");
  if (before.textHash !== after.textHash) out.push("text");
  if (before.elementCount !== after.elementCount) out.push("dom");
  if (after.requests > before.requests) out.push("network");
  if (before.openDialogs !== after.openDialogs) out.push("dialog");
  return out;
}

/** Controls whose click legitimately changes nothing visible. */
export const NO_EFFECT_OK_RE = /refresh|reload|retry|export|download|csv|copy|upload|browse|choose file|刷新|重试|导出|下载|复制|上传/i;

/** URL ↔ page rules per module: what the DOM must show for a URL. */
export interface UrlRule {
  /** the query param that selects a sub view */
  param: string;
  value: string;
  /** a selector that must be visible when the param has that value */
  mustSee: string;
}
export const URL_RULES: Record<string, UrlRule[]> = {
  "/v2/eval/tasks": [
    // the wizard's step 0 (name) or step 1 (evaluator / insight pickers)
    { param: "view", value: "new", mustSee: '[data-testid="v2-task-name"], [data-testid^="v2-task-eval-"], [data-testid^="v2-task-insight-"]' },
    { param: "view", value: "detail", mustSee: 'button:has-text("Back"), [data-testid="v2-task-source"], [data-testid="v2-task-agent"]' },
    { param: "tab", value: "all", mustSee: '[data-testid="v2-tab-all"][aria-selected="true"]' },
  ],
  "/v2/agents": [
    { param: "view", value: "new", mustSee: '[data-testid="v2-agent-method-harness"], [data-testid="v2-agent-name"], [data-testid="v2-agent-review"]' },
    { param: "view", value: "detail", mustSee: '[data-testid="v2-agent-basic"], [data-testid="v2-agent-deploy"], button:has-text("Back")' },
  ],
  "/v2/memory": [
    { param: "tab", value: "short-term", mustSee: '[data-testid="v2-tab-short-term"][aria-selected="true"]' },
    { param: "tab", value: "long-term", mustSee: '[data-testid="v2-tab-long-term"][aria-selected="true"]' },
    { param: "tab", value: "resources", mustSee: '[data-testid="v2-tab-resources"][aria-selected="true"]' },
    { param: "tab", value: "overview", mustSee: '[data-testid="v2-tab-overview"][aria-selected="true"]' },
  ],
};

export function rulesFor(url: string): UrlRule[] {
  const u = new URL(url, "https://x");
  const rules = URL_RULES[u.pathname] ?? [];
  return rules.filter((r) => u.searchParams.get(r.param) === r.value);
}

/** Pick an index with a seeded uniform. */
export function pickIndex(random: () => number, n: number): number {
  return Math.min(n - 1, Math.floor(random() * n));
}

/** Weighted choice: prefer controls not yet clicked in this run (coverage), then random. */
export function chooseControl(random: () => number, controls: Control[], seen: Set<string>): Control | null {
  if (controls.length === 0) return null;
  const fresh = controls.filter((c) => !seen.has(c.id));
  const pool = fresh.length && random() < 0.7 ? fresh : controls;
  return pool[pickIndex(random, pool.length)];
}
