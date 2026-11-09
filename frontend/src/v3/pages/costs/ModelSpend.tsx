import { useTranslation } from "react-i18next";

import type { CostModelRow } from "../../../lib/api";
import { Chip, Empty, Panel } from "../../ui";

function usd(value: number | null | undefined): string {
  if (value == null) return "—";
  return value > 0 && value < 0.01 ? `$${value.toFixed(4)}` : `$${value.toFixed(2)}`;
}

/** Spend per model: every call priced at its own model's rate (`by_model`). A model
 *  the price map has no rate for shows "—" and a chip, never $0. */
export function ModelSpendPanel({ rows, total }: { rows: CostModelRow[]; total: number }) {
  const { t } = useTranslation();
  const max = Math.max(...rows.map((r) => r.est_cost_usd ?? 0), 0) || 1;
  return (
    <Panel flush title={t("v3.costs.byModel")}>
      {rows.length === 0 ? (
        <Empty title={t("v2.costs.empty")} />
      ) : (
        <table className="v3-table" data-testid="v3-costs-by-model">
          <thead>
            <tr>
              <th>{t("v3.costs.colModel")}</th>
              <th className="num">{t("v3.costs.colShare")}</th>
              <th className="num">{t("v3.costs.colIn")}</th>
              <th className="num">{t("v3.costs.colOut")}</th>
              <th className="num">{t("v2.costs.colCalls")}</th>
              <th className="num">{t("v2.costs.kpiAgents")}</th>
            </tr>
          </thead>
          <tbody>
            {rows.map((row) => (
              <tr key={row.model}>
                <td>
                  <div className="v3-cost-bar">
                    <span style={{ display: "inline-flex", gap: 8, alignItems: "center", minWidth: 0 }}>
                      <span className="mono" style={{ overflowWrap: "anywhere" }}>{row.model}</span>
                      {row.est_cost_usd == null && <Chip s="wait">{t("v3.costs.unpricedChip")}</Chip>}
                    </span>
                    <span className="mono">{usd(row.est_cost_usd)}</span>
                    <span className="track"><i className={row.est_cost_usd == null ? "none" : undefined}
                      style={{ width: `${((row.est_cost_usd ?? 0) / max) * 100}%` }} /></span>
                  </div>
                </td>
                <td className="num">
                  {row.est_cost_usd != null && total > 0 ? `${((row.est_cost_usd / total) * 100).toFixed(1)}%` : "—"}
                </td>
                <td className="num">{row.tokens_in.toLocaleString()}</td>
                <td className="num">{row.tokens_out.toLocaleString()}</td>
                <td className="num">{row.llm_calls.toLocaleString()}</td>
                <td className="num">{row.agents}</td>
              </tr>
            ))}
          </tbody>
        </table>
      )}
    </Panel>
  );
}
