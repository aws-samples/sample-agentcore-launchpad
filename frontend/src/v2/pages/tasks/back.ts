// Sub-pages of 评估任务 (task detail, the wizard) return to where they were opened from.
// The opener puts its own search string in the history state; Back restores it (the
// 全部任务 tab, an agent's scorecard page, the detail a task was copied from). The
// wizard hands its state on to the task it creates, so that task's Back skips the
// finished form. Without one (a deep link) Back lands on the default tab, as before.
// Back always pushes a page with no state of its own, so a chain of Backs ends there.
// A page outside 评估任务 (the architect's step 2) names its own path instead: Back on
// the page it opened returns there.
export interface TasksBackState {
  tasksBack?: string;
  returnTo?: string;
}

/** The history state an opener passes along (`setParams(next, { state })`). */
export function backState(search: string): TasksBackState {
  return { tasksBack: search };
}

/** Where Back goes: the opener's search params, else none (the default tab). */
export function backTarget(state: unknown): URLSearchParams {
  const search = (state as TasksBackState | null | undefined)?.tasksBack;
  return new URLSearchParams(typeof search === "string" ? search : "");
}

/** The history state of a link from a page outside 评估任务: Back returns to `path`. */
export function returnState(path: string): TasksBackState {
  return { returnTo: path };
}

/** The outside page to go back to — an in-app path only — else null. */
export function returnPath(state: unknown): string | null {
  const to = (state as TasksBackState | null | undefined)?.returnTo;
  return typeof to === "string" && to.startsWith("/") && !to.startsWith("//") ? to : null;
}
