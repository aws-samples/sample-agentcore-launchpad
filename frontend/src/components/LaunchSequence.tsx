import { useTranslation } from "react-i18next";

import type { DeploymentInfo, JobInfo, StageInfo } from "../lib/api";
import { Btn } from "./Btn";
import { Chip } from "./Chip";
import { Panel } from "./Panel";

type StageKey = "generate" | "package" | "provision" | "deploy" | "register";

function stageClass(stage: StageInfo): string {
  if (stage.status === "succeeded" || stage.status === "skipped") return " done";
  if (stage.status === "running") return " now";
  if (stage.status === "failed") return " fail";
  return "";
}

function stageNode(stage: StageInfo, index: number): string {
  if (stage.status === "succeeded" || stage.status === "skipped") return "✓";
  if (stage.status === "running") return "●";
  if (stage.status === "failed") return "✕";
  return String(index + 1);
}

export function LaunchSequence({
  deployment,
  job,
  agentStatus,
  detailsMode,
  onRestart,
}: {
  deployment: DeploymentInfo | null;
  job: JobInfo | null;
  agentStatus: string;
  detailsMode: boolean;
  onRestart: () => void;
}) {
  const { t } = useTranslation();
  const stages = deployment?.stages ?? [];
  return (
    <div className="cfg-grid">
      <Panel
        brk
        title={t(detailsMode ? "create.sequence.detailsTitle" : "create.sequence.title")}
        sub={job ? `job #${job.id.slice(0, 8)}` : undefined}
        end={
          agentStatus === "active" ? (
            <Chip tone="good" icon="●">
              {t("status.active")}
            </Chip>
          ) : agentStatus === "failed" ? (
            <Chip tone="crit" icon="✕">
              {t("status.failed")}
            </Chip>
          ) : (
            <Chip tone="warn" icon="◐">
              {t("status.deploying")}
            </Chip>
          )
        }
        pad={false}
      >
        <div className="pipeline">
          {stages.map((s, i) => (
            <div key={s.name} className={`pstage${stageClass(s)}`}>
              <div className="node">{stageNode(s, i)}</div>
              <div className="pn">{t(`create.stages.${s.name as StageKey}`)}</div>
              <div className="pt">{s.detail || "—"}</div>
            </div>
          ))}
        </div>
        {job?.error && (
          <div className="pbody" style={{ paddingTop: 0 }}>
            <div className="note" style={{ borderColor: "var(--crit)" }}>
              <span className="i" style={{ color: "var(--crit)" }}>
                [✕]
              </span>
              <span className="mono">{job.error}</span>
            </div>
          </div>
        )}
      </Panel>

      <div>
        <Panel title={t("create.sequence.logTitle")} pad={false}>
          <div
            className="code"
            style={{
              border: 0,
              maxHeight: 320,
              overflowY: "auto",
              margin: 0,
              // Log lines carry ARNs/ids far wider than the panel — wrap them
              // instead of relying on .code's pre + horizontal scroll, which
              // reads as truncation with overlay scrollbars.
              whiteSpace: "pre-wrap",
              overflowWrap: "anywhere",
              overflowX: "hidden",
            }}
            data-testid="job-log"
          >
            {(job?.events ?? []).map((e, i) => (
              <div key={i}>
                <span className="cm">{e.ts.slice(11, 19)}</span>{" "}
                <span className={e.level === "error" ? "k1" : "k2"}>{e.stage}</span> {e.msg}
              </div>
            ))}
            {!job?.events?.length && <span className="cm">{t("create.sequence.waiting")}</span>}
          </div>
        </Panel>
        <div style={{ height: 14 }} />
        <Panel>
          <div style={{ display: "flex", gap: 10, justifyContent: "flex-end" }}>
            <Btn onClick={onRestart}>
              {t(detailsMode ? "create.sequence.backToList" : "create.sequence.newAgent")}
            </Btn>
          </div>
        </Panel>
      </div>
    </div>
  );
}
