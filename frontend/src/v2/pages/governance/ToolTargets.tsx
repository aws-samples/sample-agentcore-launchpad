import { type FormEvent, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { errorMessage } from "../../../lib/api";
import {
  adoptExternalTarget,
  adoptUnmanagedTarget,
  createLambdaTarget,
  deleteLambdaTarget,
  type ExternalMcpTarget,
  lambdaArnPlaceholder,
  type LambdaToolStatus,
  type LambdaToolTarget,
  listExternalTargets,
  listLambdaTargets,
  listUnmanagedTargets,
  parseToolSchema,
  targetKindLabel,
  type UnmanagedTarget,
  validateLambdaForm,
} from "../../../lib/api/mcpReviews";
import { sharedFromOf } from "../../../lib/api/sharedFrom";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, type Column, Confirm, Drawer, Field, LinkButton, Table, Tag, type TagTone } from "../../ui";
import { GATE_TONE } from "./common";
import { ToolReview } from "./ToolReview";

const STATUS_TONE: Record<LambdaToolStatus, TagTone> = {
  reviewing: "orange",
  publishing: "blue",
  live: "green",
  rejected: "red",
  failed: "red",
};

const EXAMPLE = JSON.stringify(
  [{ name: "lookup_customer", description: "Look one customer up by id", inputSchema: { type: "object", properties: { id: { type: "string" } }, required: ["id"] } }],
  null,
  2,
);

/** "From project X" on a row another project lent this one; nothing on the project's own. */
function LentTag({ row }: { row: unknown }) {
  const { t } = useTranslation();
  const lent = sharedFromOf(row);
  if (!lent) return null;
  return <Tag tone="outline">{t("v3.projects.shares.label.from", { name: lent.name })}</Tag>;
}

/**
 * Put a Lambda function on the workspace Gateway as MCP tools (projects §15.12): the
 * schema is reviewed before the target exists — a member holding `mcp.approve`, not
 * the creator, approves it, then the Gateway role is granted the invoke and the
 * target is created.
 */
function LambdaForm({ onCreated }: { onCreated: (target: LambdaToolTarget) => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [arn, setArn] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [schema, setSchema] = useState(EXAMPLE);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const errors = validateLambdaForm(arn, name, schema);
  const invalid = Object.keys(errors).length > 0;
  const shown = (key?: string) => (touched && key ? t(key) : null);
  // the function must be in this workspace's account and region — the example says so
  const { current: workspace } = useWorkspace();

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    const parsed = parseToolSchema(schema);
    if (invalid || "error" in parsed) return;
    setBusy(true);
    try {
      const created = await createLambdaTarget({
        lambda_arn: arn.trim(),
        target_name: name.trim(),
        tool_schema: parsed.tools,
        description: description.trim(),
      });
      toast("success", t("lambdaTool.form.created"));
      setArn("");
      setName("");
      setDescription("");
      setTouched(false);
      onCreated(created);
    } catch (err) {
      toast("error", `${t("lambdaTool.form.failed")}: ${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  };

  return (
    <form onSubmit={submit} className="v2-form" data-testid="v2-lambda-form">
      <Alert>{t("lambdaTool.form.intro")}</Alert>
      <div className="v2-form cols-2">
        <Field label={t("lambdaTool.form.arn")} required error={shown(errors.arn)}>
          <input
            id="v2-lambda-arn"
            className="v2-input mono"
            placeholder={lambdaArnPlaceholder(workspace)}
            value={arn}
            onChange={(e) => setArn(e.target.value)}
          />
        </Field>
        <Field
          label={t("lambdaTool.form.targetName")}
          required
          error={shown(errors.name)}
          hint={t("lambdaTool.form.targetNameHint", { name: name.trim() || "crm-tools" })}
        >
          <input className="v2-input mono" placeholder="crm-tools" value={name} onChange={(e) => setName(e.target.value)} />
        </Field>
        <Field label={t("lambdaTool.form.description")} full>
          <input className="v2-input" maxLength={500} value={description} onChange={(e) => setDescription(e.target.value)} />
        </Field>
        <Field label={t("lambdaTool.form.schema")} required full error={shown(errors.schema)} hint={t("lambdaTool.form.schemaHint")}>
          <textarea className="v2-textarea code" rows={10} value={schema} onChange={(e) => setSchema(e.target.value)} />
        </Field>
      </div>
      <div>
        <Button kind="primary" type="submit" disabled={busy || (touched && invalid)} testId="v2-lambda-submit">
          {t("lambdaTool.form.submit")}
        </Button>
      </div>
    </form>
  );
}

/**
 * The Gateway's other targets: made by hand (a Lambda function put on the Gateway
 * outside the console), by bootstrap, or bound to a Connection. A hand-made Lambda
 * target with an inline schema can be adopted — its tool list today becomes the
 * approved baseline and it then sits in the Lambda list above; the others say who
 * manages them.
 */
function UnmanagedTargets({ nonce, onAdopted }: { nonce: number; onAdopted: () => void }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const list = useLoad(() => listUnmanagedTargets(), `v2-unmanaged-tools:${nonce}`);
  const [adopting, setAdopting] = useState<UnmanagedTarget | null>(null);
  const [busy, setBusy] = useState(false);

  const adopt = async () => {
    if (!adopting) return;
    setBusy(true);
    try {
      await adoptUnmanagedTarget(adopting.target_id);
      toast("success", t("lambdaTool.unmanaged.adopted", { name: adopting.target_name }));
      setAdopting(null);
      onAdopted();
    } catch (err) {
      toast("error", `${t("lambdaTool.unmanaged.adoptFailed")}: ${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const columns: Column<UnmanagedTarget>[] = [
    {
      key: "name",
      title: t("v2.governance.toolTargets.colTarget"),
      render: (row) => (
        <>
          <span className="v2-row">
            <span className="mono">{row.target_name}</span>
            <Tag tone="outline">{targetKindLabel(row.kind)}</Tag>
            {row.system && <Tag tone="gray">{t("lambdaTool.unmanaged.system")}</Tag>}
          </span>
          {row.lambda_arn && <span className="sub mono v2-governance-break">{row.lambda_arn}</span>}
          {!row.lambda_arn && row.description && <span className="sub">{row.description}</span>}
        </>
      ),
    },
    { key: "status", title: t("v2.common.status"), render: (row) => row.status ?? "—" },
    { key: "tools", title: t("v2.governance.toolTargets.colTools"), className: "num", render: (row) => (row.kind === "lambda" ? row.tool_names.length : "—") },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) =>
        row.reason === null ? (
          <LinkButton disabled={busy} title={t("lambdaTool.unmanaged.adoptHint")} onClick={() => setAdopting(row)} testId={`v2-unmanaged-adopt-${row.target_id}`}>
            {t("lambdaTool.unmanaged.adopt")}
          </LinkButton>
        ) : (
          <span className="v2-muted" data-testid={`v2-unmanaged-reason-${row.target_id}`}>
            {t(`lambdaTool.unmanaged.reason.${row.reason}`, { connection: row.connection ?? "", kind: targetKindLabel(row.kind) })}
          </span>
        ),
    },
  ];

  return (
    <Card title={t("lambdaTool.unmanaged.title")}>
      <p className="v2-tool-targets-intro">{t("lambdaTool.unmanaged.intro")}</p>
      <Table
        columns={columns}
        rows={list.data?.targets ?? []}
        rowKey={(row) => row.target_id}
        loading={list.loading && !list.data}
        error={list.error}
        onRetry={list.reload}
        empty={t("lambdaTool.unmanaged.empty")}
        testId="v2-unmanaged-targets"
      />
      <Confirm
        open={adopting !== null}
        title={t("lambdaTool.unmanaged.adoptTitle", { name: adopting?.target_name ?? "" })}
        body={t("lambdaTool.unmanaged.adoptBody", { count: adopting?.tool_names.length ?? 0 })}
        confirmLabel={t("lambdaTool.unmanaged.adopt")}
        busy={busy}
        onConfirm={() => void adopt()}
        onClose={() => setAdopting(null)}
      />
    </Card>
  );
}

/**
 * Gateway governance's 工具目标 tab (`?tab=targets`), the V2 twin of V3's
 * `GatewayToolTargets`: console-created Lambda targets and hand-made MCP-server
 * targets (externally hosted, adoptable under review), each one's schema review in a
 * drawer, and the Lambda create form. The page head's "new" button bumps
 * `formRequest` to bring the form into view.
 */
export function ToolTargets({ formRequest = 0 }: { formRequest?: number }) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [nonce, setNonce] = useState(0);
  const [reviewing, setReviewing] = useState<{ id: string; name: string } | null>(null);
  const [deleting, setDeleting] = useState<LambdaToolTarget | null>(null);
  const [busy, setBusy] = useState(false);
  const lambdas = useLoad(() => listLambdaTargets(), `v2-lambda-tools:${nonce}`);
  const external = useLoad(() => listExternalTargets(), `v2-external-tools:${nonce}`);
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
      await adoptExternalTarget(target.target_id);
      toast("success", t("lambdaTool.external.adopted", { name: target.target_name }));
      reload();
    } catch (err) {
      toast("error", `${t("lambdaTool.external.adoptFailed")}: ${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const remove = async () => {
    if (!deleting) return;
    setBusy(true);
    try {
      await deleteLambdaTarget(deleting.id);
      toast("success", t("lambdaTool.list.deleted", { name: deleting.target_name }));
      setDeleting(null);
      reload();
    } catch (err) {
      toast("error", `${t("lambdaTool.list.deleteFailed")}: ${errorMessage(err)}`);
    } finally {
      setBusy(false);
    }
  };

  const lambdaColumns: Column<LambdaToolTarget>[] = [
    {
      key: "name",
      title: t("v2.governance.toolTargets.colTarget"),
      render: (row) => (
        <>
          <span className="v2-row">
            <span className="mono">{row.target_name}</span>
            <LentTag row={row} />
          </span>
          <span className="sub mono v2-governance-break">{row.lambda_arn}</span>
          {row.error && <span className="sub v2-governance-err">{row.error}</span>}
        </>
      ),
    },
    {
      key: "status",
      title: t("v2.common.status"),
      render: (row) => <Tag tone={STATUS_TONE[row.status]} dot>{t(`lambdaTool.status.${row.status}`)}</Tag>,
    },
    { key: "tools", title: t("v2.governance.toolTargets.colTools"), className: "num", render: (row) => row.tool_names.length },
    { key: "creator", title: t("v2.common.creator"), render: (row) => row.created_by || "—" },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) => (
        <div className="v2-actions">
          <LinkButton onClick={() => setReviewing({ id: row.id, name: row.target_name })} testId={`v2-lambda-review-${row.id}`}>
            {t("lambdaTool.list.showReview")}
          </LinkButton>
          {/* a target another project lent is its owner's to delete */}
          {!sharedFromOf(row) && (
            <LinkButton
              danger
              disabled={busy || row.status === "publishing"}
              title={row.status === "publishing" ? t("story.reason.publishingNow") : undefined}
              onClick={() => setDeleting(row)}
            >
              {t("lambdaTool.list.delete")}
            </LinkButton>
          )}
        </div>
      ),
    },
  ];

  const externalColumns: Column<ExternalMcpTarget>[] = [
    {
      key: "name",
      title: t("v2.governance.toolTargets.colTarget"),
      render: (row) => (
        <>
          <span className="v2-row">
            <span className="mono">{row.target_name}</span>
            <LentTag row={row} />
            <Tag tone="blue">{t("lambdaTool.external.hosted")}</Tag>
          </span>
          <span className="sub mono v2-governance-break">{row.endpoint}</span>
        </>
      ),
    },
    {
      key: "review",
      title: t("v2.governance.toolTargets.colReview"),
      render: (row) =>
        row.review ? <Tag tone={GATE_TONE[row.review.state]} dot>{t(`mcpReview.state.${row.review.state}`)}</Tag> : "—",
    },
    {
      key: "health",
      title: t("v2.governance.toolTargets.colHealth"),
      render: (row) => (row.health?.status ? t(`mcpReview.ops.healthState.${row.health.status}`) : "—"),
    },
    {
      key: "ops",
      title: t("v2.common.actions"),
      className: "right",
      render: (row) => (
        <div className="v2-actions">
          {row.tool_id ? (
            <LinkButton onClick={() => setReviewing({ id: row.tool_id as string, name: row.target_name })}>
              {t("lambdaTool.list.showReview")}
            </LinkButton>
          ) : row.adopted_elsewhere ? (
            <span className="v2-muted">{t("lambdaTool.external.elsewhere")}</span>
          ) : (
            <LinkButton disabled={busy} title={t("lambdaTool.external.adoptHint")} onClick={() => adopt(row)} testId={`v2-external-adopt-${row.target_id}`}>
              {t("lambdaTool.external.adopt")}
            </LinkButton>
          )}
        </div>
      ),
    },
  ];

  return (
    <div data-testid="v2-tool-targets">
      <Card title={t("lambdaTool.list.title")}>
        <Table
          columns={lambdaColumns}
          rows={lambdas.data?.targets ?? []}
          rowKey={(row) => row.id}
          loading={lambdas.loading && !lambdas.data}
          error={lambdas.error}
          onRetry={lambdas.reload}
          empty={t("lambdaTool.list.empty")}
          testId="v2-lambda-targets"
        />
      </Card>
      <div ref={formRef} style={{ scrollMarginTop: 16 }}>
        <Card title={t("lambdaTool.form.title")}>
          <LambdaForm onCreated={(row) => { reload(); setReviewing({ id: row.id, name: row.target_name }); }} />
        </Card>
      </div>
      <Card title={t("lambdaTool.external.title")}>
        <p className="v2-tool-targets-intro">{t("lambdaTool.external.intro")}</p>
        <Table
          columns={externalColumns}
          rows={external.data?.targets ?? []}
          rowKey={(row) => row.target_id}
          loading={external.loading && !external.data}
          error={external.error}
          onRetry={external.reload}
          empty={t("lambdaTool.external.empty")}
          testId="v2-external-targets"
        />
      </Card>
      <UnmanagedTargets nonce={nonce} onAdopted={reload} />
      <Drawer open={reviewing !== null} title={reviewing?.name ?? ""} onClose={() => setReviewing(null)} testId="v2-tool-review-drawer">
        {reviewing && <ToolReview key={reviewing.id} toolId={reviewing.id} onChanged={reload} />}
      </Drawer>
      <Confirm
        open={deleting !== null}
        title={t("lambdaTool.list.deleteTitle", { name: deleting?.target_name ?? "" })}
        body={t("lambdaTool.list.deleteBody")}
        confirmLabel={t("lambdaTool.list.delete")}
        danger
        busy={busy}
        onConfirm={remove}
        onClose={() => setDeleting(null)}
      />
    </div>
  );
}
