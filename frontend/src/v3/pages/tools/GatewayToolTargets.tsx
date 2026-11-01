import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { ApiErrorDetail } from "../../../components/ApiErrorDetail";
import { ErrorCallout } from "../../../components/ErrorCallout";
import {
  adoptExternalTarget,
  deleteLambdaTarget,
  type ExternalMcpTarget,
  type LambdaToolStatus,
  type LambdaToolTarget,
  listExternalTargets,
  listLambdaTargets,
  toolReviewApi,
} from "../../../lib/api/mcpReviews";
import { sharedFromOf } from "../../../lib/api/sharedFrom";
import { useFailure } from "../../failure";
import { useLoad, useToast } from "../../hooks";
import { SharedFromTag } from "../../shares/SharedFromTag";
import { Btn, Chip, Confirm, Empty, Notice, Panel, type Signal, Skeleton } from "../../ui";
import { McpReviewSection } from "../agent/McpReviewSection";
import { LambdaTargetForm } from "./LambdaTargetForm";

const STATUS_SIGNAL: Record<LambdaToolStatus, Signal> = {
  reviewing: "wait",
  publishing: "wait",
  live: "ok",
  rejected: "act",
  failed: "act",
};

function ToolReview({ toolId, onChanged }: { toolId: string; onChanged: () => void }) {
  const api = useMemo(() => toolReviewApi(toolId), [toolId]);
  return <McpReviewSection agentId={toolId} drift={false} onChanged={onChanged} api={api} />;
}

/**
 * The Gateway's tool targets besides hosted MCP servers (projects §15.12, §15.15):
 * console-created Lambda targets (with the form and each one's schema review) and
 * hand-made MCP-server targets, listed as externally hosted and adoptable under review.
 * The 工具目标 tab of Gateway governance (`/v3/governance?tab=targets`); the page head
 * there carries the "new" button, and each press bumps `formRequest` to bring the
 * create form into view.
 */
export function GatewayToolTargets({ formRequest = 0 }: { formRequest?: number }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  const [nonce, setNonce] = useState(0);
  const [open, setOpen] = useState<string | null>(null);
  const [deleting, setDeleting] = useState<LambdaToolTarget | null>(null);
  const [busy, setBusy] = useState(false);
  const lambdas = useLoad(() => listLambdaTargets(), `v3-lambda-tools:${nonce}`);
  const external = useLoad(() => listExternalTargets(), `v3-external-tools:${nonce}`);
  const reload = () => setNonce((n) => n + 1);
  const formRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (formRequest === 0) return;
    formRef.current?.scrollIntoView({ behavior: "smooth", block: "start" });
    formRef.current?.querySelector<HTMLElement>("input, textarea")?.focus({ preventScroll: true });
  }, [formRequest]);

  const adopt = async (target: ExternalMcpTarget) => {
    setBusy(true);
    try {
      failed.clear();
      await adoptExternalTarget(target.target_id);
      toast("ok", t("lambdaTool.external.adopted", { name: target.target_name }));
      reload();
    } catch (err) {
      failed.fail(err, t("lambdaTool.external.adoptFailed"));
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      failed.clear();
      await deleteLambdaTarget(deleting.id);
      toast("ok", t("lambdaTool.list.deleted", { name: deleting.target_name }));
      setDeleting(null);
      reload();
    } catch (err) {
      failed.fail(err, t("lambdaTool.list.deleteFailed"));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div style={{ display: "grid", gap: 16 }} data-testid="v3-gateway-tools">
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-gateway-tools-error" />
      <Panel title={t("lambdaTool.list.title")}>
        {lambdas.error ? (
          <Notice s="act"><ApiErrorDetail error={lambdas.failure ?? lambdas.error} /></Notice>
        ) : !lambdas.data ? (
          <Skeleton rows={2} />
        ) : lambdas.data.targets.length === 0 ? (
          <Empty title={t("lambdaTool.list.empty")} />
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            {lambdas.data.targets.map((row) => (
              <div key={row.id} style={{ display: "grid", gap: 8 }}>
                <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                  <span className="mono">{row.target_name}</span>
                  <SharedFromTag row={row} />
                  <Chip s={STATUS_SIGNAL[row.status]}>{t(`lambdaTool.status.${row.status}`)}</Chip>
                  <span className="v3-hint mono" style={{ wordBreak: "break-all" }}>{row.lambda_arn}</span>
                  <span className="v3-hint">{t("lambdaTool.list.tools", { count: row.tool_names.length })}</span>
                  <span style={{ flex: 1 }} />
                  <Btn size="sm" kind="ghost" onClick={() => setOpen(open === row.id ? null : row.id)}>
                    {t(open === row.id ? "lambdaTool.list.hideReview" : "lambdaTool.list.showReview")}
                  </Btn>
                  {/* a target another project lent is its owner's to delete */}
                  {!sharedFromOf(row) && (
                    <Btn size="sm" kind="danger" busy={busy} disabled={row.status === "publishing"} disabledReason={t("story.reason.publishingNow")} onClick={() => setDeleting(row)}>
                      {t("lambdaTool.list.delete")}
                    </Btn>
                  )}
                </div>
                {row.error && <Notice s="act">{row.error}</Notice>}
                {open === row.id && <ToolReview toolId={row.id} onChanged={reload} />}
              </div>
            ))}
          </div>
        )}
      </Panel>
      <div ref={formRef} style={{ scrollMarginTop: 16 }}>
        <Panel title={t("lambdaTool.form.title")}>
          <LambdaTargetForm onCreated={(row) => { setOpen(row.id); reload(); }} />
        </Panel>
      </div>
      <Panel title={t("lambdaTool.external.title")}>
        <div className="v3-hint" style={{ marginBottom: 8 }}>{t("lambdaTool.external.intro")}</div>
        {external.error ? (
          <Notice s="act"><ApiErrorDetail error={external.failure ?? external.error} /></Notice>
        ) : !external.data ? (
          <Skeleton rows={2} />
        ) : external.data.targets.length === 0 ? (
          <Empty title={t("lambdaTool.external.empty")} />
        ) : (
          <div style={{ display: "grid", gap: 12 }}>
            {external.data.targets.map((target) => (
              <div key={target.target_id} style={{ display: "grid", gap: 8 }}>
                <div style={{ display: "flex", gap: 10, alignItems: "center", flexWrap: "wrap" }}>
                  <span className="mono">{target.target_name}</span>
                  <SharedFromTag row={target} />
                  <Chip s="info">{t("lambdaTool.external.hosted")}</Chip>
                  {target.review && <Chip s={target.review.state === "approved" ? "ok" : "wait"}>{t(`mcpReview.state.${target.review.state}`)}</Chip>}
                  {target.health?.status && <span className="v3-hint">{t(`mcpReview.ops.healthState.${target.health.status}`)}</span>}
                  <span className="v3-hint mono" style={{ wordBreak: "break-all" }}>{target.endpoint}</span>
                  <span style={{ flex: 1 }} />
                  {target.tool_id ? (
                    <Btn size="sm" kind="ghost" onClick={() => setOpen(open === target.tool_id ? null : target.tool_id)}>
                      {t(open === target.tool_id ? "lambdaTool.list.hideReview" : "lambdaTool.list.showReview")}
                    </Btn>
                  ) : target.adopted_elsewhere ? (
                    <span className="v3-hint">{t("lambdaTool.external.elsewhere")}</span>
                  ) : (
                    <Btn size="sm" disabled={busy} onClick={() => adopt(target)} title={t("lambdaTool.external.adoptHint")}>
                      {t("lambdaTool.external.adopt")}
                    </Btn>
                  )}
                </div>
                {target.tool_id && open === target.tool_id && <ToolReview toolId={target.tool_id} onChanged={reload} />}
              </div>
            ))}
          </div>
        )}
      </Panel>
      {deleting && (
        <Confirm
          title={t("lambdaTool.list.deleteTitle", { name: deleting.target_name })}
          confirmLabel={t("lambdaTool.list.delete")}
          cancelLabel={t("mcpReview.ops.cancel")}
          danger
          busy={busy}
          onCancel={() => setDeleting(null)}
          onConfirm={remove}
        >
          {t("lambdaTool.list.deleteBody")}
        </Confirm>
      )}
    </div>
  );
}
