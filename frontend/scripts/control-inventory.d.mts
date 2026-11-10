// Types for the control-inventory generator (scripts/control-inventory.mjs), so the
// vitest guard in src/v2/__inventory__ can import it under `strict` without `allowJs`.

export interface ControlEffect {
  type: "api" | "navigate" | "dialog" | "local";
  detail: string;
}

export interface ControlRow {
  /** `<file>:<testId>` (or `<file>:#<ordinal>` without a testId); `~n` suffix on duplicates */
  id: string;
  /** V2 route the control renders under (`/v2/*` for the shell) */
  page: string;
  /** `?view=` sub-page when the component is only reached through one */
  view: string | null;
  /** `?tab=` sub-page (DataCenter, Governance, Memory, Observability, Skill Lab, …) */
  tab: string | null;
  file: string;
  line: number;
  tag: string;
  kind: "button" | "link" | "nav-link" | "tab" | "toggle" | "row-action" | "option" | "dialog-action" | "clickable";
  testId: string | null;
  selector: string;
  label: string | null;
  effect: ControlEffect | null;
  effects: ControlEffect[];
  guards: string[];
  roles: string[];
  destructive: boolean;
  disabledWithoutHint: boolean;
}

export interface Inventory {
  generatedFrom: string;
  count: number;
  controls: ControlRow[];
}

export interface UntestableEntry {
  id: string;
  file: string;
  line: number;
  reason: "no-testid" | "no-effect" | "no-testid+no-effect";
  register?: string;
  resolved?: boolean;
}

export const FRONTEND_ROOT: string;
export const INVENTORY_DIR: string;
export const CONTROLS_JSON: string;
export const UNTESTABLE_JSON: string;
export function generate(root?: string): Inventory;
export function untestable(inv: Inventory): UntestableEntry[];
/** the drift-guard payload: every row minus `line` (a reformat must not fail the guard) */
export function semantic(inv: Inventory): { count: number; controls: Omit<ControlRow, "line">[] };
export function stableJson(obj: unknown): string;
export function buildApiMap(apiFile?: string): Map<string, string | null>;
export function buildRouteMap(appFile?: string): Map<string, string>;
