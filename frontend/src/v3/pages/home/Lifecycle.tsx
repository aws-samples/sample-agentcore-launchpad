import { Sparkles } from "lucide-react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { Panel } from "../../ui";
import { LIFECYCLE } from "./modules";

/** The Agent lifecycle (create → chat → observe → evaluate → optimise → govern), each step a link to its V3 page. */
export function Lifecycle() {
  const { t } = useTranslation();
  return (
    <div data-testid="v3-home-lifecycle">
      <Panel
        title={t("v2.home.flowTitle")}
        end={<Link to="/v3/assistant" className="v3-btn ghost sm"><Sparkles size={13} aria-hidden="true" /> {t("v2.home.assistant")}</Link>}
      >
        <div style={{ display: "grid", gap: 12 }}>
          <p className="v3-home-flow-sub">{t("v2.home.flowSub")}</p>
          <ol className="v3-home-flow">
            {LIFECYCLE.map((step, i) => (
              <li key={step.key}>
                <Link to={step.to} data-testid={`v3-home-step-${step.key}`}>
                  <span className="n mono">{i + 1}</span>
                  <span className="t">{t(`v2.home.step.${step.key}`)}</span>
                  <span className="d">{t(`v2.home.step.${step.key}Desc`)}</span>
                </Link>
              </li>
            ))}
          </ol>
        </div>
      </Panel>
    </div>
  );
}
