import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { editPath } from "../../../lib/agent-admin/edit";
import { OUTPUTS_RETENTION_DAYS } from "../../../lib/agent-spec";
import type { AgentInfo } from "../../../lib/api";
import { type AgentStorage, bucketUri as entryBucketUri, type LiveMount, storageApi } from "../../../lib/api/storage";
import { useLoad } from "../../hooks";
import { Chip, Notice, Panel, Skeleton } from "../../ui";
import { STATUS_SIGNAL } from "../storage/model";
import { when } from "./format";

const MOUNT_KIND_LABEL: Record<LiveMount["kind"], string> = {
  efs: "EFS",
  s3files: "S3 Files",
  s3bucket: "S3 bucket",
};

/** `s3://bucket/prefix` of a bucket mount (the whole bucket when it has no prefix). */
function bucketUri(m: LiveMount): string {
  return `s3://${m.bucket ?? "—"}/${m.prefix ?? ""}`;
}

/**
 * 存储 (storage-mounts §10): the current version's mounts — each catalog entry with its
 * mode and its last check, the legacy inline access points, and session storage.
 * Changing a mount is a re-publish, so "Change storage…" opens the edit wizard.
 */
export function StorageCard({ agent }: { agent: AgentInfo }) {
  const storage = useLoad(() => storageApi.forAgent(agent.id), `v3-agent-storage:${agent.id}`);
  return <StorageCardView agent={agent} loading={storage.loading} error={storage.error} data={storage.data} />;
}

/** The card itself, from what the container read. */
export function StorageCardView({
  agent,
  loading,
  error,
  data,
}: {
  agent: AgentInfo;
  loading: boolean;
  error: string | null;
  data: AgentStorage | null;
}) {
  const { t } = useTranslation();
  const title = t("v3.storage.card.title");
  if (loading && !data) return <Panel title={title}><Skeleton rows={2} /></Panel>;
  if (error || !data) return <Panel title={title}><Notice s="act">{error ?? t("v3.storage.card.unavailable")}</Notice></Panel>;
  const external = (data.live?.entries ?? []).filter((m) => m.origin === "external");
  // session storage is a row of its own, so the card stays when only that is mounted
  if (!data.session_storage && data.mounts.length === 0 && data.inline.length === 0 && external.length === 0
    && !data.outputs && data.live?.status !== "unavailable") return null;
  const change = <Link to={editPath(agent)} className="v3-inline-link">{t("v3.storage.card.change")}</Link>;
  return (
    <Panel title={title} end={change}>
      <table className="v3-table" data-testid="v3-agent-storage">
        <thead>
          <tr>
            <th>{t("v3.storage.detail.path")}</th>
            <th>{t("v3.storage.card.storage")}</th>
            <th>{t("v3.storage.detail.mode")}</th>
            <th>{t("v3.storage.col.check")}</th>
          </tr>
        </thead>
        <tbody>
          {data.session_storage && (
            <tr data-testid="v3-agent-storage-session">
              <td className="mono">{data.session_storage}</td>
              <td>
                <b>{t("v3.storage.card.session")}</b>
                <div><small>{t("v3.storage.card.sessionHint")}</small></div>
              </td>
              <td><Chip s="wait">{t("v3.storage.mode.rw")}</Chip></td>
              <td>—</td>
            </tr>
          )}
          {data.mounts.map((m) => (
            <tr key={m.storage_id}>
              <td className="mono">{m.mount_path}</td>
              <td>
                {m.entry ? <><b>{m.entry.name}</b> <small className="mono">{m.storage_id}</small></> : <span className="mono">{m.storage_id}</span>}
                {m.missing && <> <Chip s="act">{t("v3.storage.card.missing")}</Chip></>}
                {m.entry?.archived && <> <Chip s="off">{t("v3.storage.card.archived")}</Chip></>}
                {m.entry?.kind === "s3bucket" && (
                  <div data-testid="v3-agent-storage-entry-bucket">
                    <small className="mono">{entryBucketUri(m.entry)}</small>
                    {m.entry.scope && <> <Chip s="off">{t(`v3.storage.scope.${m.entry.scope}`)}</Chip></>}
                  </div>
                )}
              </td>
              <td><Chip s={m.mode === "rw" ? "wait" : "off"}>{t(`v3.storage.mode.${m.mode}`)}</Chip></td>
              <td>
                {m.entry ? (
                  <>
                    <Chip s={STATUS_SIGNAL[m.entry.status] ?? "off"}>{t(`v3.storage.status.${m.entry.status}`)}</Chip>{" "}
                    <small>{m.entry.checked_at ? when(m.entry.checked_at) : t("v3.storage.never")}</small>
                    {m.entry.blocking + m.entry.warnings > 0 && (
                      <small> · {t("v3.storage.counts", { block: m.entry.blocking, warn: m.entry.warnings })}</small>
                    )}
                  </>
                ) : "—"}
              </td>
            </tr>
          ))}
          {data.outputs && (
            <tr data-testid="v3-agent-storage-outputs">
              <td className="mono">{data.outputs.mount_path}</td>
              <td>
                <b>{t("v3.storage.card.outputs")}</b>{" "}
                <Chip s="off">{t("v3.storage.scope.per_session")}</Chip>
                <div><small>{t("v3.storage.card.outputsHint", { days: OUTPUTS_RETENTION_DAYS })}</small></div>
              </td>
              <td><Chip s="wait">{t("v3.storage.mode.rw")}</Chip></td>
              <td>—</td>
            </tr>
          )}
          {data.inline.map((m, i) => (
            <tr key={`inline-${i}`}>
              <td className="mono">{m.mount_path ?? "—"}</td>
              <td><small className="mono">{m.access_point_arn ?? "—"}</small> <Chip s="off">{t("v3.storage.card.inline")}</Chip></td>
              <td><Chip s="wait">{t(`v3.storage.mode.${m.mode}`)}</Chip></td>
              <td>—</td>
            </tr>
          ))}
          {external.map((m) => (
            <tr key={`live-${m.mount_path}`} data-testid="v3-agent-storage-external">
              <td className="mono">{m.mount_path}</td>
              <td>
                {m.kind === "s3bucket" ? (
                  <>
                    <small className="mono" data-testid="v3-agent-storage-bucket">{bucketUri(m)}</small>
                    {(m.bucket_owner || m.bucket_region) && (
                      <small> · {[m.bucket_owner, m.bucket_region].filter(Boolean).join(" · ")}</small>
                    )}
                  </>
                ) : m.storage_id ? <><b>{m.storage_name || m.storage_id}</b> <small className="mono">{m.storage_id}</small></>
                  : <small className="mono">{m.access_point_arn ?? "—"}</small>}
                {" "}<Chip s="info">{t("v3.storage.card.external")}</Chip>
                {" "}<Chip s="off">{MOUNT_KIND_LABEL[m.kind]}</Chip>
              </td>
              <td>—</td>
              <td>—</td>
            </tr>
          ))}
        </tbody>
      </table>
      {data.inline.length > 0 && <small className="v3-hint">{t("v3.storage.card.inlineHint")}</small>}
      {external.length > 0 && <small className="v3-hint">{t("v3.storage.card.externalHint")}</small>}
      {external.some((m) => m.kind === "s3bucket") && (
        <small className="v3-hint">{t("v3.storage.card.externalBucketHint")}</small>
      )}
      {data.live?.status === "unavailable" && (
        <small className="v3-hint">{t("v3.storage.card.liveUnavailable", { error: data.live.error ?? "" })}</small>
      )}
    </Panel>
  );
}
