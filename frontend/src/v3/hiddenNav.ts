import { createContext, useContext } from "react";

/**
 * The hidden-entry list (`/api/nav/hidden-modules`) the Shell already read, shared with
 * the routed page so it issues no second request. The Command center reads its
 * section keys (`HOME_SECTIONS`) from it, and Menu settings calls `reload` after a
 * save so the administrator's own rail follows at once. A steward gets an empty set;
 * a failed read hides nothing.
 */
export interface HiddenNav {
  hidden: ReadonlySet<string>;
  /** the read has answered or failed; before that a section the list may hide is held back */
  ready: boolean;
  isAdmin: boolean;
  reload: () => void;
}

const EMPTY: HiddenNav = { hidden: new Set<string>(), ready: true, isAdmin: false, reload: () => undefined };

export const HiddenNavContext = createContext<HiddenNav>(EMPTY);

export function useHiddenNav(): HiddenNav {
  return useContext(HiddenNavContext);
}
