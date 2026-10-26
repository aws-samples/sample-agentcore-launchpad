import { Navigate, useLocation } from "react-router-dom";

import { v3OnlineToTasks } from "./online/actions";

/**
 * The former 在线评估 page: its configs are the 持续评估 tab of 评估任务 now. Old links
 * and bookmarks (`?view=new|detail|edit&id=`) land on the same state there, in one hop.
 */
export function V3Online() {
  const { search } = useLocation();
  return <Navigate to={v3OnlineToTasks(search)} replace />;
}
