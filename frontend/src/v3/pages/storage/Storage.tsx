import "../users.css";

import { Archive, HardDrive, Pencil, Plus, RefreshCw, ShieldAlert, ShieldCheck, Trash2 } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useAuth } from "../../../auth/auth-context";
import { ErrorCallout } from "../../../components/ErrorCallout";
import { networkApi } from "../../../lib/api/networkProfiles";
import {
  checksOf,
  countChecks,
  emptyStorageForm,
  provisionFormToBody,
  type StorageEntry,
  type StorageForm,
  type StorageProvisionBody,
  storageApi,
  storageFormToBody,
  visibleJobs,
} from "../../../lib/api/storage";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { Btn, Chip, Confirm, Empty, Notice, PageHead, Panel, Skeleton } from "../../ui";
import { STATUS_SIGNAL } from "./model";
import { ProvisionDialog } from "./ProvisionDialog";
import { ProvisionJobs, TeardownDialog } from "./ProvisionJobs";
import { RegisterDialog } from "./RegisterDialog";
import { StorageAccess } from "./StorageAccess";
import { StorageDetail } from "./StorageDetail";

const toForm = (e: StorageEntry): StorageForm => ({
  id: e.id,
  name: e.name,
  access_point_arn: e.access_point_arn,
  max_mode: e.max_mode,
  data_class: e.data_class,
  network_profile_id: e.network_profile_id ?? "",
});

/**
 * Configure → Storage (storage-mounts §10; platform administrators): the workspace's
 * storage catalog — register an S3 Files / EFS access point, run its check and read the
 * results, edit / archive it, the shared view (who mounts it, how), and the per-project
 * allowed set. Workspace-scoped (`X-Workspace`); `?id=` opens one entry's detail.
 */
export function V3Storage() {
  const { t } = useTranslation();
  const { isAdmin } = useAuth();
  const { current } = useWorkspace();
  const toast = useToast();
  const failed = useFailure();
  const [params, setParams] = useSearchParams();
  const selectedId = params.get("id") ?? "";
  const [version, setVersion] = useState(0);
  const list = useLoad(async () => (isAdmin ? storageApi.list() : { storage: [] }), `v3-storage:${isAdmin}:${current?.id ?? ""}:${version}`);
  const profiles = useLoad(async () => (isAdmin ? networkApi.list() : { profiles: [] }), `v3-storage-profiles:${isAdmin}:${current?.id ?? ""}`);
  const jobsLoad = useLoad(async () => (isAdmin ? storageApi.provisionJobs() : { jobs: [] }), `v3-storage-jobs:${isAdmin}:${current?.id ?? ""}:${version}`);
  const [editing, setEditing] = useState<StorageEntry | "new" | null>(null);
  const [provisioning, setProvisioning] = useState(false);
  const [archiving, setArchiving] = useState<StorageEntry | null>(null);
  const [tearing, setTearing] = useState<StorageEntry | null>(null);
  const [busyId, setBusyId] = useState<string | null>(null);
  const entries = list.data?.storage ?? [];
  const jobs = visibleJobs(jobsLoad.data?.jobs ?? []);
  const running = jobs.some((j) => !j.done);
  const reload = () => setVersion((v) => v + 1);
  // a create or teardown runs for minutes: follow it until it settles
  useEffect(() => {
    if (!running) return undefined;
    const timer = window.setTimeout(() => setVersion((v) => v + 1), 5000);
    return () => window.clearTimeout(timer);
  }, [running, version]);

  if (!isAdmin) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.storage.eyebrow")} title={t("v3.storage.title")} sub={t("auth.adminRequired.meta")} />
        <Panel>
          <Empty title={<span style={{ display: "inline-flex", gap: 8, alignItems: "center" }}><ShieldAlert size={16} /> {t("auth.adminRequired.title")}</span>}>
            {t("usersPage.forbiddenBody")}
          </Empty>
        </Panel>
      </div>
    );
  }

  const run = async (e: StorageEntry, call: () => Promise<unknown>, okKey: string) => {
    setBusyId(e.id);
    try {
      failed.clear();
      await call();
      toast("ok", t(okKey, { name: e.name }));
      reload();
      return true;
    } catch (err) {
      failed.fail(err);
      return false;
    } finally {
      setBusyId(null);
    }
  };
  const selected = entries.find((e) => e.id === selectedId) ?? null;
  const provision = async (body: StorageProvisionBody) => {
    await storageApi.provision(body);
    toast("ok", t("v3.storage.provision.queued", { id: body.id }));
    reload();
  };

  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.storage.eyebrow")}
        title={t("v3.storage.title")}
        sub={t("v3.storage.sub", { workspace: current?.name ?? current?.id ?? "—", region: current?.region ?? "—" })}
        end={
          <>
            <Btn kind="ghost" onClick={reload} disabled={list.loading}><RefreshCw size={14} /> {t("v3.users.refresh")}</Btn>
            <Btn kind="ghost" onClick={() => setEditing("new")}><Plus size={14} /> {t("v3.storage.create")}</Btn>
            <Btn kind="primary" onClick={() => setProvisioning(true)}><HardDrive size={14} /> {t("v3.storage.provision.open")}</Btn>
          </>
        }
      />
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-storage-error" />
      <Panel flush>
        {list.loading && !list.data ? (
          <div style={{ padding: 20 }}><Skeleton rows={4} /></div>
        ) : list.error ? (
          <div style={{ padding: 20 }}><Notice s="act">{list.error}</Notice></div>
        ) : entries.length === 0 ? (
          <Empty title={t("v3.storage.empty")}>{t("v3.storage.emptyBody")}</Empty>
        ) : (
          <StorageTable
            entries={entries}
            selectedId={selectedId}
            busyId={busyId}
            onSelect={(id) => setParams(id === selectedId ? {} : { id })}
            onCheck={(e) => void run(e, () => storageApi.check(e.id), "v3.storage.checked")}
            onEdit={setEditing}
            onArchive={setArchiving}
            onTeardown={setTearing}
          />
        )}
      </Panel>
      <ProvisionJobs
        jobs={jobs}
        onRetry={(job) => void provision({ ...job.request, id: job.storage_id } as StorageProvisionBody).catch((err) => failed.fail(err))}
      />

      {selected && <StorageDetail entry={selected} />}
      <StorageAccess entries={entries} onSaved={reload} />

      {editing && (
        <RegisterDialog
          creating={editing === "new"}
          initial={editing === "new" ? emptyStorageForm() : toForm(editing)}
          profiles={profiles.data?.profiles ?? []}
          onClose={() => setEditing(null)}
          onSubmit={async (form) => {
            const body = storageFormToBody(form);
            if (editing === "new") {
              const created = await storageApi.register(body);
              if (created.check_error) failed.fail(created.check_error.message);
            } else {
              await storageApi.update(editing.id, {
                name: body.name ?? editing.id, max_mode: body.max_mode, data_class: body.data_class,
                network_profile_id: body.network_profile_id ?? "",
              });
            }
            setEditing(null);
            toast("ok", t(editing === "new" ? "v3.storage.created" : "v3.storage.saved", { name: body.name ?? body.id }));
            reload();
          }} />
      )}
      {provisioning && (
        <ProvisionDialog
          profiles={profiles.data?.profiles ?? []}
          onClose={() => setProvisioning(false)}
          onSubmit={async (form) => {
            await provision(provisionFormToBody(form));
            setProvisioning(false);
          }}
        />
      )}
      {tearing && (
        <TeardownDialog
          storageId={tearing.id}
          name={tearing.name}
          onClose={() => setTearing(null)}
          onConfirm={async (confirm) => {
            await storageApi.teardown(tearing.id, confirm);
            setTearing(null);
            toast("ok", t("v3.storage.teardown.queued", { name: tearing.name }));
            reload();
          }}
        />
      )}
      {archiving && (
        <Confirm
          title={t("v3.storage.archiveTitle", { name: archiving.name })}
          confirmLabel={t("v3.structure.archive")}
          cancelLabel={t("v3.common.cancel")}
          danger
          busy={busyId !== null}
          onCancel={() => setArchiving(null)}
          onConfirm={() => {
            const e = archiving;
            void run(e, () => storageApi.archive(e.id), "v3.storage.archived").then((ok) => ok && setArchiving(null));
          }}
        >
          {t("v3.storage.archiveBody", { name: archiving.name })}
        </Confirm>
      )}
    </div>
  );
}

/** The catalog table: one row per entry, with its check status and who mounts it. */
export function StorageTable({
  entries,
  selectedId,
  busyId,
  onSelect,
  onCheck,
  onEdit,
  onArchive,
  onTeardown,
}: {
  entries: StorageEntry[];
  selectedId: string;
  busyId: string | null;
  onSelect: (id: string) => void;
  onCheck: (e: StorageEntry) => void;
  onEdit: (e: StorageEntry) => void;
  onArchive: (e: StorageEntry) => void;
  /** console-made entries only (`origin: "platform"`): delete the resources too */
  onTeardown?: (e: StorageEntry) => void;
}) {
  const { t } = useTranslation();
  return (
    <table className="v3-table" data-testid="v3-storage-table">
      <thead>
        <tr>
          <th>{t("v3.storage.col.name")}</th>
          <th>{t("v3.storage.col.kind")}</th>
          <th>{t("v3.storage.col.azs")}</th>
          <th>{t("v3.storage.col.mode")}</th>
          <th>{t("v3.storage.col.check")}</th>
          <th>{t("v3.storage.col.users")}</th>
          <th />
        </tr>
      </thead>
      <tbody>
        {entries.map((e) => {
          const busy = busyId === e.id;
          const counts = countChecks(checksOf(e));
          const agents = e.agents?.length ?? 0;
          return (
            <tr key={e.id} aria-selected={e.id === selectedId || undefined}>
              <td>
                <button type="button" className="v3-inline-link" style={{ background: "none", border: 0, padding: 0, cursor: "pointer" }}
                  onClick={() => onSelect(e.id)}>
                  <b>{e.name}</b>
                </button>
                <div>
                  <small className="mono">{e.id}</small>
                  {e.origin === "platform" && <> <Chip s="info">{t("v3.storage.provision.madeHere")}</Chip></>}
                </div>
              </td>
              <td>{t(`v3.storage.kind.${e.kind}`)}</td>
              <td className="mono">{e.mount_target_azs.join(", ") || "—"}</td>
              <td>
                {t(`v3.storage.mode.${e.max_mode}`)} · {t(`v3.storage.dataClass.${e.data_class}`)}
              </td>
              <td>
                <Chip s={STATUS_SIGNAL[e.status] ?? "off"}>{t(`v3.storage.status.${e.status}`, { defaultValue: e.status })}</Chip>
                {counts.block + counts.warn > 0 && <small> {t("v3.storage.counts", { block: counts.block, warn: counts.warn })}</small>}
              </td>
              <td>
                {t("v3.storage.usage", { projects: e.projects?.length ?? 0, agents })}
                {e.multi_writer && <> <Chip s="wait">{t("v3.storage.multiWriterChip")}</Chip></>}
              </td>
              <td style={{ width: 1, whiteSpace: "nowrap" }}>
                <div style={{ display: "flex", gap: 4, justifyContent: "flex-end" }}>
                  <Btn size="sm" kind="ghost" disabled={busy} onClick={() => onCheck(e)}><ShieldCheck size={13} /> {t("v3.storage.check")}</Btn>
                  <Btn size="sm" kind="ghost" disabled={busy} onClick={() => onEdit(e)}><Pencil size={13} /> {t("v3.structure.edit")}</Btn>
                  <Btn size="sm" kind="ghost" busy={busy} disabled={agents > 0} disabledReason={t("v3.storage.inUse", { count: agents })}
                    title={t("v3.structure.archive")} aria-label={t("v3.structure.archive")}
                    onClick={() => onArchive(e)}>
                    <Archive size={13} />
                  </Btn>
                  {e.origin === "platform" && onTeardown && (
                    <Btn size="sm" kind="ghost" busy={busy} disabled={agents > 0} disabledReason={t("v3.storage.inUse", { count: agents })}
                      title={t("v3.storage.teardown.action")} aria-label={t("v3.storage.teardown.action")}
                      onClick={() => onTeardown(e)}>
                      <Trash2 size={13} />
                    </Btn>
                  )}
                </div>
              </td>
            </tr>
          );
        })}
      </tbody>
    </table>
  );
}
