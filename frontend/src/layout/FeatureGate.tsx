import type { ReactNode } from "react";
import { useLocation } from "react-router-dom";

import { featureForPath, hasFeature, useFeatures } from "../lib/capabilities";
import { V2NotFound } from "../v2/pages/NotFound";

/** A module route whose feature this deployment does not expose renders NotFound. */
export function FeatureGate({ children }: { children: ReactNode }) {
  const { pathname } = useLocation();
  const { features } = useFeatures();
  const feature = featureForPath(pathname);
  if (feature && !hasFeature(features, feature)) return <V2NotFound />;
  return <>{children}</>;
}
