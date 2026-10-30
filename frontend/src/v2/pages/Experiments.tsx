import { useEffect } from "react";
import { useTranslation } from "react-i18next";
import { Navigate, useLocation, useSearchParams } from "react-router-dom";

import { api } from "../../lib/api";
import { v2ExperimentsRedirect } from "../../lib/evaluation-url";
import { useLoad } from "../hooks";
import { PageHeader } from "../ui";
import { CanaryDetail } from "./canary/CanaryDetail";
import { CanaryList } from "./canary/CanaryList";
import { CanaryStart } from "./canary/CanaryStart";

const LIST_POLL_MS = 8000;

/**
 * 实验 — runtime canaries only. Sub-pages are `?canary=` states: `canary=new` (with the
 * optional `champion=` preselection) or `canary=<id>`; `mode=canary` is accepted for old
 * links but no longer needed.
 *
 * N12-obs49 (user decision 2026-10-16) hid the configuration-bundle experiment; N13-obs2
 * (user decision 2026-10-17) deleted it — pages, API routes and all. Its deep links
 * (`?mode=configuration`, `?view=new|detail&id=`) still land on the canary list.
 */
export function V2Experiments() {
  const { t } = useTranslation();
  const { search } = useLocation();
  const [params] = useSearchParams();
  const redirect = v2ExperimentsRedirect(search);
  const canaryParam = params.get("canary");
  const canaries = useLoad(() => api.listRuntimeCanaries(), "v2-canaries");
  const reloadCanaries = canaries.reload;
  useEffect(() => {
    const timer = window.setInterval(() => reloadCanaries(), LIST_POLL_MS);
    return () => window.clearInterval(timer);
  }, [reloadCanaries]);

  if (redirect) return <Navigate to={redirect} replace />;
  if (canaryParam === "new") return <CanaryStart />;
  if (canaryParam) return <CanaryDetail key={canaryParam} id={canaryParam} />;

  return (
    <>
      <PageHeader title={t("v2.experiments.title")} desc={t("v2.experiments.desc")} />
      <CanaryList
        canaries={canaries.data?.canaries ?? []}
        loading={canaries.loading}
        error={canaries.error}
        reload={reloadCanaries}
      />
    </>
  );
}
