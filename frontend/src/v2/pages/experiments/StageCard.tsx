import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";

import { Tag } from "../../ui";

export type CardState = "active" | "done" | "pending";

/** One numbered step of an experiment or canary flow; the active step is highlighted. */
export function StageCard({
  id,
  index,
  title,
  state,
  children,
}: {
  id: string;
  index: number;
  title: string;
  state: CardState;
  children: ReactNode;
}) {
  const { t } = useTranslation();
  return (
    <section className={`v2-card v2-exp-stage ${state}`} data-testid={`v2-exp-card-${id}`}>
      <div className="v2-card-body">
        <h2 className="v2-sec-title">
          <span className="v2-exp-num">{String(index).padStart(2, "0")}</span>
          {title}
          <span className="end">
            <Tag tone={state === "done" ? "green" : state === "active" ? "blue" : "gray"}>{t(`v2.experiments.cardState.${state}`)}</Tag>
          </span>
        </h2>
        {children}
      </div>
    </section>
  );
}
