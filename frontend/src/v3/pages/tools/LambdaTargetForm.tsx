import { type FormEvent, useState } from "react";
import { useTranslation } from "react-i18next";

import { ErrorCallout } from "../../../components/ErrorCallout";
import { createLambdaTarget, lambdaArnPlaceholder, type LambdaToolTarget, parseToolSchema, validateLambdaForm } from "../../../lib/api/mcpReviews";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useFailure } from "../../failure";
import { useToast } from "../../hooks";
import { Btn, Notice } from "../../ui";

const EXAMPLE = JSON.stringify(
  [{ name: "lookup_customer", description: "Look one customer up by id", inputSchema: { type: "object", properties: { id: { type: "string" } }, required: ["id"] } }],
  null,
  2,
);

/**
 * Put a Lambda function on the workspace Gateway as MCP tools (projects §15.12). The
 * tool schema is reviewed like a hosted server's tool list before the target exists:
 * a member holding `mcp.approve` — not the creator — approves it, then the Gateway role
 * is granted `lambda:InvokeFunction` and the target is created.
 */
export function LambdaTargetForm({ onCreated }: { onCreated?: (target: LambdaToolTarget) => void }) {
  const { t } = useTranslation();
  const toast = useToast();
  const failed = useFailure();
  // the function must be in this workspace's account and region — the example says so
  const { current: workspace } = useWorkspace();
  const [arn, setArn] = useState("");
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [schema, setSchema] = useState(EXAMPLE);
  const [busy, setBusy] = useState(false);
  const [touched, setTouched] = useState(false);
  const errors = validateLambdaForm(arn, name, schema);
  const invalid = Object.keys(errors).length > 0;

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setTouched(true);
    if (invalid) return;
    const parsed = parseToolSchema(schema);
    if ("error" in parsed) return;
    setBusy(true);
    try {
      failed.clear();
      const created = await createLambdaTarget({
        lambda_arn: arn.trim(),
        target_name: name.trim(),
        tool_schema: parsed.tools,
        description: description.trim(),
      });
      toast("ok", t("lambdaTool.form.created"));
      setArn("");
      setName("");
      setDescription("");
      setTouched(false);
      onCreated?.(created);
    } catch (err) {
      failed.fail(err, t("lambdaTool.form.failed"));
    } finally {
      setBusy(false);
    }
  };

  const error = (key?: string) => (touched && key ? <div className="v3-hint" style={{ color: "var(--v3-act)" }}>{t(key)}</div> : null);

  return (
    <form onSubmit={submit} style={{ display: "grid", gap: 10 }} data-testid="v3-lambda-form">
      <Notice s="info">{t("lambdaTool.form.intro")}</Notice>
      <label className="v3-hint" htmlFor="lambda-arn">{t("lambdaTool.form.arn")}</label>
      <input
        id="lambda-arn"
        className="v3-input mono"
        placeholder={lambdaArnPlaceholder(workspace)}
        value={arn}
        onChange={(e) => setArn(e.target.value)}
      />
      {error(errors.arn)}
      <label className="v3-hint" htmlFor="lambda-target-name">{t("lambdaTool.form.targetName")}</label>
      <input id="lambda-target-name" className="v3-input mono" placeholder="crm-tools" value={name} onChange={(e) => setName(e.target.value)} />
      <div className="v3-hint">{t("lambdaTool.form.targetNameHint", { name: name.trim() || "crm-tools" })}</div>
      {error(errors.name)}
      <label className="v3-hint" htmlFor="lambda-description">{t("lambdaTool.form.description")}</label>
      <input id="lambda-description" className="v3-input" maxLength={500} value={description} onChange={(e) => setDescription(e.target.value)} />
      <label className="v3-hint" htmlFor="lambda-schema">{t("lambdaTool.form.schema")}</label>
      <textarea id="lambda-schema" className="v3-input mono" rows={10} value={schema} onChange={(e) => setSchema(e.target.value)} />
      <div className="v3-hint">{t("lambdaTool.form.schemaHint")}</div>
      {error(errors.schema)}
      <div>
        <Btn kind="primary" type="submit" busy={busy} disabled={touched && invalid} disabledReason={t("story.reason.fixErrors")}>
          {t("lambdaTool.form.submit")}
        </Btn>
      </div>
      <ErrorCallout error={failed.error} onDismiss={failed.clear} data-testid="v3-lambda-form-error" />
    </form>
  );
}
