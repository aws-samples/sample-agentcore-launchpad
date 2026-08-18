import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Chip } from "../../components";
import type { ChipTone } from "../../components";
import type { SkillLabTrainStep } from "../../lib/api";

/**
 * Gate verdict for a step. The trainer writes composite action strings
 * ("accept", "reject_gate", "skip_no_patches", …), so this matches substrings
 * the way the backend's own totals do rather than enumerating them.
 */
function verdict(action: string | null): { key: string; tone: ChipTone; icon: string } {
  const value = (action ?? "").toLowerCase();
  if (value.includes("accept")) return { key: "accept", tone: "good", icon: "✓" };
  if (value.includes("reject")) return { key: "reject", tone: "crit", icon: "✕" };
  if (value.includes("skip")) return { key: "skip", tone: "muted", icon: "–" };
  return { key: "other", tone: "warn", icon: "?" };
}

const score = (value: number | null) => (typeof value === "number" ? value.toFixed(3) : "—");

const wall = (value: number | null) => {
  if (typeof value !== "number") return "—";
  return value < 90 ? `${value.toFixed(0)}s` : `${(value / 60).toFixed(1)}m`;
};

/** Empty-ish extras (null, "", [], {}) must not open an expander for nothing. */
function extra(value: unknown): string | null {
  if (value === null || value === undefined || value === "") return null;
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  if (Array.isArray(value)) {
    if (value.length === 0) return null;
    return value.every((item) => typeof item === "string")
      ? (value as string[]).join("\n")
      : JSON.stringify(value, null, 2);
  }
  if (typeof value === "object") {
    if (Object.keys(value as object).length === 0) return null;
    return JSON.stringify(value, null, 2);
  }
  return String(value);
}

/** Per-step optimizer timeline: one row per gate decision. */
export function TrainTimeline({
  steps,
  bestStep,
}: {
  steps: SkillLabTrainStep[];
  bestStep: number | null;
}) {
  const { t } = useTranslation();
  const [open, setOpen] = useState<Set<number>>(new Set());

  const toggle = (index: number) =>
    setOpen((prev) => {
      const next = new Set(prev);
      if (next.has(index)) next.delete(index);
      else next.add(index);
      return next;
    });

  return (
    <table data-testid="train-timeline">
      <thead>
        <tr>
          <th>{t("skillLab.train.col.step")}</th>
          <th>{t("skillLab.train.col.gate")}</th>
          <th>{t("skillLab.train.col.hard")}</th>
          <th>{t("skillLab.train.col.soft")}</th>
          <th>{t("skillLab.train.col.skillLen")}</th>
          <th>{t("skillLab.train.col.wall")}</th>
        </tr>
      </thead>
      <tbody>
        {steps.map((step, index) => {
          const chip = verdict(step.action);
          const reasons = extra(step.gate_reasons);
          const excluded = extra(step.excluded_failures);
          const expandable = reasons !== null || excluded !== null;
          const isOpen = open.has(index);
          const isBest = step.step !== null && step.step === bestStep;
          return [
            <tr
              key={`s-${index}`}
              data-testid={`train-step-row-${step.step ?? index}`}
              style={{ cursor: expandable ? "pointer" : "default" }}
              onClick={() => expandable && toggle(index)}
            >
              <td className="pri mono">
                {expandable ? (isOpen ? "▾ " : "▸ ") : ""}
                {t("skillLab.train.stepLabel", { n: step.step ?? index + 1 })}
                {step.epoch !== null && (
                  <span className="dim" style={{ marginLeft: 6, fontSize: 10.5 }}>
                    {t("skillLab.train.epochLabel", { n: step.epoch })}
                  </span>
                )}
                {isBest && (
                  <span style={{ marginLeft: 6, color: "var(--good)", fontSize: 10.5 }}>
                    ★ {t("skillLab.train.bestTag")}
                  </span>
                )}
              </td>
              <td>
                <Chip tone={chip.tone} icon={chip.icon}>
                  {t(`skillLab.train.gate.${chip.key}`)}
                </Chip>
              </td>
              <td className="mono">{score(step.selection_hard)}</td>
              <td className="mono">{score(step.selection_soft)}</td>
              <td className="mono dim">{step.skill_len ?? "—"}</td>
              <td className="mono dim">{wall(step.wall_time_s)}</td>
            </tr>,
            isOpen && (
              <tr key={`s-${index}-detail`} data-testid={`train-step-detail-${step.step ?? index}`}>
                <td colSpan={6} style={{ background: "rgba(255,255,255,.015)" }}>
                  {([
                    ["gateReasons", reasons],
                    ["excludedFailures", excluded],
                  ] as [string, string | null][])
                    .filter((entry): entry is [string, string] => entry[1] !== null)
                    .map(([key, value]) => (
                      <div key={key} style={{ marginBottom: 8 }}>
                        <div className="mono dim" style={{ fontSize: 10, letterSpacing: ".08em" }}>
                          {t(`skillLab.train.detail.${key}`)}
                        </div>
                        <pre
                          className="code"
                          style={{
                            margin: "3px 0 0",
                            maxHeight: 180,
                            overflow: "auto",
                            whiteSpace: "pre-wrap",
                            overflowWrap: "anywhere",
                            fontSize: 10.5,
                          }}
                        >
                          {value}
                        </pre>
                      </div>
                    ))}
                </td>
              </tr>
            ),
          ];
        })}
        {steps.length === 0 && (
          <tr>
            <td colSpan={6} className="dim mono" style={{ textAlign: "center" }}>
              {t("skillLab.train.curve.waiting")}
            </td>
          </tr>
        )}
      </tbody>
    </table>
  );
}
