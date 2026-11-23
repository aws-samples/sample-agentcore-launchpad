import { useState } from "react";
import { useTranslation } from "react-i18next";

import { bucketUri, checksOf, mountedPrefixHint, type StorageCheck, type StorageEntry } from "../../../lib/api/storage";
import { copyText } from "../../../v2/pages/observability/common";
import { Btn, Chip, Lamp, Notice, Panel } from "../../ui";
import { when } from "../agent/format";
import { SEVERITY_SIGNAL } from "./model";

function Rows({ rows }: { rows: StorageCheck[] }) {
  return (
    <ul data-testid="v3-storage-results" style={{ listStyle: "none", margin: 0, padding: 0, display: "grid", gap: 6 }}>
      {rows.map((r, i) => (
        <li key={`${r.check}-${i}`} style={{ display: "flex", gap: 8, alignItems: "baseline" }}>
          <Lamp s={SEVERITY_SIGNAL[r.severity] ?? "off"} title={r.severity} />
          <span className="mono" style={{ minWidth: 180 }}>{r.check}</span>
          <span>{r.message}</span>
        </li>
      ))}
    </ul>
  );
}

/** A JSON document an administrator copies (the role statements, the bucket policy). */
function JsonBlock({ title, hint, value, testId }: { title: string; hint: string; value: unknown; testId: string }) {
  const { t } = useTranslation();
  const [copied, setCopied] = useState(false);
  const text = JSON.stringify(value, null, 2);
  return (
    <Panel
      title={title}
      end={
        <Btn size="sm" kind="ghost" onClick={() => void copyText(text).then(() => setCopied(true))}>
          {t(copied ? "v3.storage.detail.copied" : "v3.storage.detail.copy")}
        </Btn>
      }
    >
      <p className="v3-hint" style={{ marginTop: 0 }}>{hint}</p>
      <pre className="mono" data-testid={testId} style={{ margin: 0, overflow: "auto", maxHeight: 360, fontSize: 12 }}>{text}</pre>
    </Panel>
  );
}

/**
 * An entry's detail (storage-mounts §4.4, §4.5): what the check found (file system,
 * POSIX identity, mount targets — or, for an S3 bucket entry, the bucket, layout and
 * encryption), the check results row by row, the role statements agents get and the
 * bucket policy to apply, and the shared view — the agents mounting it with their
 * version, endpoint and mode, its projects, and the "several writers" warning.
 */
export function StorageDetail({ entry }: { entry: StorageEntry }) {
  const { t } = useTranslation();
  const results = checksOf(entry);
  const agents = entry.agents ?? [];
  const isBucket = entry.kind === "s3bucket";
  return (
    <div style={{ display: "grid", gap: 12 }} data-testid="v3-storage-detail">
      <Panel title={t("v3.storage.detail.basic")}>
        <dl className="v3-kv">
          <dt>ID</dt><dd className="mono">{entry.id}</dd>
          <dt>{t("v3.storage.col.kind")}</dt><dd>{t(`v3.storage.kind.${entry.kind}`)}</dd>
          {isBucket ? (
            <>
              <dt>{t("v3.storage.detail.bucketUri")}</dt><dd className="mono" data-testid="v3-storage-bucket-uri">{bucketUri(entry)}</dd>
              <dt>{t("v3.storage.detail.scope")}</dt>
              <dd>{entry.scope ? t(`v3.storage.scope.${entry.scope}`) : "—"}</dd>
              <dt>{t("v3.storage.detail.mountedAs")}</dt><dd className="mono">{mountedPrefixHint(entry)}</dd>
              <dt>{t("v3.storage.detail.encryption")}</dt>
              <dd className="mono">{entry.encryption ?? "—"}{entry.kms_key_arn ? ` · ${entry.kms_key_arn}` : ""}</dd>
              <dt>{t("v3.storage.detail.owner")}</dt><dd className="mono">{entry.bucket_owner || "—"}</dd>
              <dt>{t("v3.storage.detail.region")}</dt><dd className="mono">{entry.bucket_region || "—"}</dd>
            </>
          ) : (
            <>
              <dt>{t("v3.storage.form.arn")}</dt><dd className="mono">{entry.access_point_arn}</dd>
              <dt>{t("v3.storage.detail.fileSystem")}</dt><dd className="mono">{entry.file_system_arn || "—"}</dd>
              {entry.bucket_arn && (<><dt>{t("v3.storage.detail.bucket")}</dt><dd className="mono">{entry.bucket_arn}</dd></>)}
              <dt>{t("v3.storage.detail.posix")}</dt>
              <dd className="mono">{entry.posix_uid == null ? "—" : `${entry.posix_uid}:${entry.posix_gid ?? "—"}`}{entry.root_directory ? ` · ${entry.root_directory}` : ""}</dd>
              <dt>VPC</dt><dd className="mono">{entry.vpc_id || "—"}</dd>
              <dt>{t("v3.storage.detail.mountTargets")}</dt>
              <dd className="mono">
                {entry.mount_targets.length
                  ? entry.mount_targets.map((m) => `${m.availability_zone_id} (${m.subnet_id})`).join(", ")
                  : "—"}
              </dd>
            </>
          )}
          <dt>{t("v3.storage.form.maxMode")}</dt><dd>{t(`v3.storage.mode.${entry.max_mode}`)}</dd>
          <dt>{t("v3.storage.form.dataClass")}</dt><dd>{t(`v3.storage.dataClass.${entry.data_class}`)}</dd>
          <dt>{t("v3.storage.form.profile")}</dt><dd className="mono">{entry.network_profile_id || "—"}</dd>
          <dt>{t("v3.storage.detail.checkedAt")}</dt><dd>{entry.checked_at ? when(entry.checked_at) : t("v3.storage.never")}</dd>
        </dl>
      </Panel>
      <Panel title={t("v3.storage.detail.checks")}>
        {results.length ? <Rows rows={results} /> : <small className="v3-hint">{t("v3.storage.detail.noChecks")}</small>}
      </Panel>
      {isBucket && entry.iam_preview && (
        <JsonBlock title={t("v3.storage.detail.iam")} hint={t("v3.storage.detail.iamHint")}
          value={{ Version: "2012-10-17", Statement: entry.iam_preview }} testId="v3-storage-iam" />
      )}
      {isBucket && entry.bucket_policy && (
        <JsonBlock title={t("v3.storage.detail.bucketPolicy")} hint={t("v3.storage.detail.bucketPolicyHint")}
          value={entry.bucket_policy} testId="v3-storage-bucket-policy" />
      )}
      <Panel title={t("v3.storage.detail.shared")}>
        {entry.multi_writer && <Notice s="wait">{t("v3.storage.detail.multiWriter", { count: entry.writers ?? 0 })}</Notice>}
        <p className="v3-hint" style={{ marginTop: 0 }}>
          {t("v3.storage.detail.projects", { projects: (entry.projects ?? []).join(", ") || "—" })}
        </p>
        {agents.length === 0 ? <small className="v3-hint">{t("v3.storage.detail.noAgents")}</small> : (
          <table className="v3-table" data-testid="v3-storage-users">
            <thead>
              <tr>
                <th>{t("v3.storage.detail.agent")}</th>
                <th>{t("v3.storage.detail.project")}</th>
                <th>{t("v3.storage.detail.version")}</th>
                <th>{t("v3.storage.detail.endpoint")}</th>
                <th>{t("v3.storage.detail.path")}</th>
                <th>{t("v3.storage.detail.mode")}</th>
              </tr>
            </thead>
            <tbody>
              {agents.map((a) => (
                <tr key={a.agent_id}>
                  <td><b>{a.name}</b> <small className="mono">{a.agent_id}</small></td>
                  <td>{a.project_id ?? "—"}</td>
                  <td>{a.version ?? "—"}</td>
                  <td>{a.endpoint_mode}</td>
                  <td className="mono">{a.mount_path}</td>
                  <td><Chip s={a.mode === "rw" ? "wait" : "off"}>{t(`v3.storage.mode.${a.mode}`)}</Chip></td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
      </Panel>
    </div>
  );
}
