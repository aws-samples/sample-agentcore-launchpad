import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { useAuth } from "../../../auth/auth-context";
import { codingApi, type CodingRuntimeState, type CodingRuntimeStatus, errorMessage } from "../../../lib/api";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Confirm, Descriptions, LinkButton, Spin, Tag, type TagTone } from "../../ui";

const TRANSIENT: CodingRuntimeState[] = ["installing", "uninstalling"];

function runtimeTone(status: CodingRuntimeState): TagTone {
  return status === "ready" ? "green" : status === "failed" ? "red" : TRANSIENT.includes(status) ? "blue" : "gray";
}

export function RuntimeStatusTag({ status }: { status: CodingRuntimeState }) {
  const { t } = useTranslation();
  return (
    <Tag tone={runtimeTone(status)} dot testId="v2-coding-runtime-status">
      {t(`v2.coding.runtime.status.${status}`)}
    </Tag>
  );
}

/**
 * The workspace's coding runtime: status for everyone; install / repair / uninstall
 * and the job log for administrators. Nothing installs on its own — an install is
 * always an admin action. Refreshes itself while an install or uninstall runs.
 */
export function RuntimeCard({
  runtime,
  loading,
  error,
  reload,
}: {
  runtime: CodingRuntimeStatus | null;
  loading: boolean;
  error: string | null;
  reload: () => void;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const { isAdmin } = useAuth();
  const [busy, setBusy] = useState(false);
  const [askUninstall, setAskUninstall] = useState(false);
  const status = runtime?.status ?? null;
  const transient = status !== null && TRANSIENT.includes(status);

  useEffect(() => {
    if (!transient) return;
    const timer = window.setInterval(reload, 5000);
    return () => window.clearInterval(timer);
  }, [transient, reload]);

  const act = async (kind: "install" | "uninstall") => {
    setBusy(true);
    try {
      if (kind === "install") await codingApi.installRuntime();
      else await codingApi.uninstallRuntime();
      toast("success", t(kind === "install" ? "v2.coding.runtime.installStarted" : "v2.coding.runtime.uninstallStarted"));
      setAskUninstall(false);
      reload();
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  if (!runtime) {
    return (
      <Card title={t("v2.coding.runtime.title")} testId="v2-coding-runtime">
        {loading ? (
          <Spin />
        ) : (
          <Alert tone="error" action={<LinkButton onClick={reload}>{t("v2.common.retry")}</LinkButton>}>
            {error ?? t("v2.common.empty")}
          </Alert>
        )}
      </Card>
    );
  }

  const job = runtime.job;
  const logTail = job?.log_tail ?? [];
  const actions = isAdmin ? (
    <div className="v2-row">
      {(status === "not_installed" || status === "failed") && (
        <Button kind="primary" size="sm" disabled={busy} onClick={() => void act("install")} testId="v2-coding-runtime-install">
          {t(status === "failed" ? "v2.coding.runtime.retryInstall" : "v2.coding.runtime.install")}
        </Button>
      )}
      {status === "ready" && (
        <Button size="sm" disabled={busy} onClick={() => void act("install")} testId="v2-coding-runtime-repair">
          {t("v2.coding.runtime.repair")}
        </Button>
      )}
      {(status === "ready" || status === "failed") && runtime.installed && (
        <Button kind="danger" size="sm" disabled={busy} onClick={() => setAskUninstall(true)} testId="v2-coding-runtime-uninstall">
          {t("v2.coding.runtime.uninstall")}
        </Button>
      )}
    </div>
  ) : null;

  return (
    <Card
      title={t("v2.coding.runtime.title")}
      sub={status ? <RuntimeStatusTag status={status} /> : undefined}
      end={actions}
      testId="v2-coding-runtime"
    >
      {status === "not_installed" && (
        <Alert tone={isAdmin ? "info" : "warn"}>
          {t(isAdmin ? "v2.coding.runtime.adminNotInstalled" : "v2.coding.runtime.memberNotInstalled")}
        </Alert>
      )}
      {status === "failed" && (
        <Alert tone="error">
          {t(isAdmin ? "v2.coding.runtime.failedAdmin" : "v2.coding.runtime.failedMember")}
          {job?.error && <div className="mono v2-coding-runtime-err">{job.error}</div>}
        </Alert>
      )}
      {transient && <Alert>{t(`v2.coding.runtime.${status}Note`)}</Alert>}
      <Descriptions
        items={[
          { label: t("v2.coding.runtime.models"), value: runtime.models.length ? <span className="mono">{runtime.models.join(", ")}</span> : "—" },
          { label: t("v2.coding.runtime.image"), value: runtime.image_tag ? <span className="mono">{runtime.image_tag}</span> : "—" },
          ...(isAdmin
            ? [{ label: t("v2.coding.runtime.arn"), value: runtime.runtime_arn ? <span className="mono">{runtime.runtime_arn}</span> : "—" }]
            : []),
        ]}
      />
      {isAdmin && job && logTail.length > 0 && (
        <details className="v2-coding-runtime-log" open={transient || status === "failed"}>
          <summary>
            {t("v2.coding.runtime.log")} · <span className="mono">{job.id}</span> · {job.status}
          </summary>
          <pre className="v2-pre" data-testid="v2-coding-runtime-log">
            {logTail.join("\n")}
          </pre>
        </details>
      )}
      <Confirm
        open={askUninstall}
        title={t("v2.coding.runtime.uninstallTitle")}
        body={t("v2.coding.runtime.uninstallBody")}
        confirmLabel={t("v2.coding.runtime.uninstall")}
        danger
        busy={busy}
        onConfirm={() => void act("uninstall")}
        onClose={() => setAskUninstall(false)}
      />
    </Card>
  );
}
