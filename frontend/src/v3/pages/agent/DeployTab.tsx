import { useTranslation } from "react-i18next";

import type { AgentPermissions } from "../../../lib/agent-admin/permissions";
import type { AgentInfo, AgentVersionsInfo, JobInfo } from "../../../lib/api";
import type { Loaded } from "../../hooks";
import { DeployCard } from "./DeployCard";
import { PromptDriftCard } from "./PromptDriftCard";
import { SnapshotsCard } from "./SnapshotsCard";
import type { DetailSections } from "./tabs";
import { VersionsCard } from "./VersionsCard";

/** 部署与版本: the latest deploy (live while it runs), AWS versions, snapshots and rollback. */
export function DeployTab({
  agent,
  job,
  sections,
  versions,
  perms,
  focusSnapshots,
  onChanged,
}: {
  agent: AgentInfo;
  job: JobInfo | null;
  sections: DetailSections;
  versions: Loaded<AgentVersionsInfo>;
  perms: AgentPermissions;
  focusSnapshots: boolean;
  onChanged: () => void;
}) {
  const { t } = useTranslation();
  const canRollback = perms.canEdit(agent) && !perms.locked;
  const blockedTitle = perms.locked
    ? perms.lockTitle
    : agent.status === "deploying"
      ? t("v3.agent.editBlocked.deploying")
      : t("v3.agent.snapshots.blocked");
  return (
    <>
      <div className="v3-grid v3-split">
        <DeployCard agent={agent} job={job} />
        {sections.versions && <VersionsCard versions={versions} />}
      </div>
      {sections.versions && agent.method === "harness" && agent.status !== "deploying" && (
        <PromptDriftCard
          agentId={agent.id}
          refreshKey={`${agent.status}:${agent.version ?? ""}:${agent.deployments?.length ?? 0}`}
          canAdopt={canRollback}
          blockedTitle={perms.locked ? perms.lockTitle : t("v3.agent.promptDrift.blocked")}
          onAdopted={onChanged}
        />
      )}
      {sections.snapshots && (
        <SnapshotsCard
          agentId={agent.id}
          refreshKey={`${agent.status}:${agent.deployments?.length ?? 0}`}
          canRollback={canRollback}
          blockedTitle={blockedTitle}
          focus={focusSnapshots}
          onRolledBack={onChanged}
        />
      )}
    </>
  );
}
