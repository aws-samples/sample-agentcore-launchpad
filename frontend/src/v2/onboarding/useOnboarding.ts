// The help menu's and the palette's way into onboarding: ask for it, then go to
// Home (where the tour and the checklist live) unless already there.
import { useCallback } from "react";
import { useLocation, useNavigate } from "react-router-dom";

import { useWorkspace } from "../../workspace/workspace-context";
import { requestLaunchChecklist, requestTour } from "./state";
import { isTourHome, TOUR_HOME } from "./stops";

/** Replay the guided tour from its first stop. */
export function useReplayTour(): () => void {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  return useCallback(() => {
    requestTour();
    if (!isTourHome(pathname)) navigate(TOUR_HOME);
  }, [navigate, pathname]);
}

/** Un-hide the current workspace's launch checklist and scroll to it. */
export function useShowLaunchChecklist(): () => void {
  const navigate = useNavigate();
  const { pathname } = useLocation();
  const { current } = useWorkspace();
  const workspaceId = current?.id;
  return useCallback(() => {
    if (workspaceId) requestLaunchChecklist(workspaceId);
    if (!isTourHome(pathname)) navigate(TOUR_HOME);
  }, [navigate, pathname, workspaceId]);
}
