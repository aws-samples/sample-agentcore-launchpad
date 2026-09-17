import { useId } from "react";
import { useTranslation } from "react-i18next";

const STAGES = ["define", "build", "evaluate", "release", "observe", "learn"] as const;

/** The methodology is explanatory; its arrows do not represent completed jobs. */
export function AdlcMethodology({ compact }: { compact: boolean }) {
  const { t } = useTranslation();
  const id = useId().replace(/:/g, "");
  return (
    <details className="assist-adlc" open={compact ? undefined : true}>
      <summary>
        <span className="mono">ADLC</span>
        <strong>{t("assistantAdlc.title")}</strong>
        <span className="dim">{t("assistantAdlc.expand")}</span>
      </summary>
      <figure>
        <p className="assist-adlc-mobile-hint">{t("assistantAdlc.swipe")}</p>
        <div className="assist-adlc-scroll" tabIndex={0}
          role="region" aria-label={t("assistantAdlc.title")}>
          <svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1360 272"
            role="img" aria-labelledby={`${id}-title ${id}-desc`}
            data-testid="adlc-diagram">
            <title id={`${id}-title`}>{t("assistantAdlc.title")}</title>
            <desc id={`${id}-desc`}>{t("assistantAdlc.description")}</desc>
            <defs>
              <marker id={`${id}-arrow`} markerWidth="8" markerHeight="8"
                refX="7" refY="4" orient="auto">
                <path d="M0 0 L8 4 L0 8 Z" fill="var(--amber)" />
              </marker>
            </defs>
            {STAGES.map((stage, index) => {
              const x = 40 + index * 220;
              return (
                <g key={stage}>
                  <rect x={x} y="24" width="180" height="148" rx="8"
                    fill="var(--panel-2)" stroke={index === 5 ? "var(--amber)" : "var(--line-2)"} />
                  <circle cx={x + 28} cy="55" r="17" fill="var(--amber)" />
                  <text x={x + 28} y="61" textAnchor="middle" fontSize="18"
                    fontWeight="700" fill="#141210">{index + 1}</text>
                  <text x={x + 16} y="101" fontSize="18" fontWeight="600" fill="var(--ink)">
                    {t(`assistantAdlc.stages.${stage}.title`)}
                  </text>
                  <text x={x + 16} y="129" fontSize="14" fill="var(--ink-2)">
                    {t(`assistantAdlc.stages.${stage}.detail`)}
                  </text>
                  <text x={x + 16} y="151" fontSize="14" fill="var(--ink-2)">
                    {t(`assistantAdlc.stages.${stage}.extra`)}
                  </text>
                  {index < STAGES.length - 1 && (
                    <path d={`M${x + 184} 98 H${x + 212}`} fill="none"
                      stroke="var(--amber)" strokeWidth="2" markerEnd={`url(#${id}-arrow)`} />
                  )}
                </g>
              );
            })}
            <path d="M1230 180 V212 H130 V180" fill="none" stroke="var(--amber)"
              strokeWidth="2" markerEnd={`url(#${id}-arrow)`} />
            <text x="680" y="248" textAnchor="middle" fontSize="17" fontWeight="600"
              fill="var(--amber)">{t("assistantAdlc.feedback")}</text>
          </svg>
        </div>
        <figcaption>{t("assistantAdlc.principle")}</figcaption>
      </figure>
    </details>
  );
}
