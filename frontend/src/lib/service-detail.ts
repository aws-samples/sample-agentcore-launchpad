import type { TFunction } from "i18next";

import type { OverviewServiceDetail, OverviewServiceId } from "./api";

/**
 * The one-line detail next to a READY service-health row (classic Overview appends it
 * after "READY ·", the V2 home card shows it as the row's tooltip). Bootstrap rows carry
 * a resource id straight from the backend; the evaluation row is structured
 * (`{ runs, pass_rate }`) and formatted here with the viewer's plural rules — "15 runs ·
 * 85 % pass" / "15 次运行 · 通过率 85 %" (WB-001, r7-e: the backend used to send the
 * English string). Empty when there is nothing to say.
 */
export function serviceDetailText(
  t: TFunction,
  id: OverviewServiceId,
  detail: OverviewServiceDetail,
): string {
  if (id === "policy") return detail.policy ?? "";
  if (id !== "evaluation") return detail[id] ?? "";
  const ev = detail.evaluation;
  if (!ev || ev.runs <= 0) return "";
  const runs = t("overview.health.evalRuns", { count: ev.runs });
  if (ev.pass_rate === null || ev.pass_rate === undefined) return runs;
  return t("overview.health.evalDetail", {
    runs,
    pass: t("overview.health.evalPass", { percent: Math.round(ev.pass_rate * 100) }),
  });
}
