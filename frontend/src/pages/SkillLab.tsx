import type { CSSProperties } from "react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { Btn, Panel, ViewHead } from "../components";
import { SkillLabNav } from "../components/SkillLabNav";
import type { SkillLabStatus } from "../lib/api";
import { api } from "../lib/api";
import { SkillLabEval } from "./SkillLabEval";
import { SkillLabTasksets } from "./SkillLabTasksets";

/**
 * Skill Lab shell: the module head, the `?view=` switcher and the worker
 * provisioning banner. Task sets are the default view; optimization arrives in
 * the next slice and renders a placeholder until then.
 */
export function SkillLab() {
  const { t } = useTranslation();
  const [searchParams] = useSearchParams();
  const view = searchParams.get("view") ?? "";
  const [status, setStatus] = useState<SkillLabStatus | null>(null);
  const [dismissed, setDismissed] = useState(false);

  // one shot: provisioning only changes when someone runs bootstrap
  useEffect(() => {
    api
      .skillLabStatus()
      .then(setStatus)
      .catch(() => {
        /* backend offline or unprovisioned — the views degrade on their own */
      });
  }, []);

  // Two independent gaps, and the second one blocks task-set authoring too (the
  // validator runs on that interpreter), so it cannot stay silent.
  const showBanner = status !== null && (!status.provisioned || !status.venv_ready) && !dismissed;

  return (
    <section>
      <ViewHead
        kicker={t("skillLab.kicker")}
        title={t("skillLab.title")}
        meta={t("skillLab.meta")}
      />
      <SkillLabNav />

      {showBanner && (
        <div
          className="note"
          data-testid="skill-lab-unprovisioned"
          style={{ marginBottom: 14, alignItems: "flex-start" }}
        >
          <span className="i">[!]</span>
          <span style={{ flex: 1 }}>
            {!status.provisioned && t("skillLab.unprovisioned.body")}
            {status.missing.length > 0 && (
              <>
                <br />
                <span className="mono dim" style={{ fontSize: 10.5 }}>
                  {status.missing.join(" · ")}
                </span>
              </>
            )}
            {!status.venv_ready && (
              <>
                {!status.provisioned && <br />}
                <span data-testid="skill-lab-no-venv">{t("skillLab.unprovisioned.venv")}</span>
              </>
            )}
          </span>
          <Btn onClick={() => setDismissed(true)}>{t("common.close")}</Btn>
        </div>
      )}

      {view === "eval" ? (
        <SkillLabEval status={status} />
      ) : view === "train" ? (
        <Panel
          brk
          title={t("skillLab.train.title")}
          sub={t("skillLab.train.sub")}
          style={{ "--i": 0 } as CSSProperties}
        >
          <div className="empty" data-testid="skill-lab-train-placeholder">
            {t("skillLab.comingSoon")}
          </div>
        </Panel>
      ) : (
        <SkillLabTasksets />
      )}
    </section>
  );
}
