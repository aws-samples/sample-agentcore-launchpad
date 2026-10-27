import { useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { AGENT_NAME_RE, BYOC_DEFAULT_ENTRYPOINT } from "../../../lib/agent-spec";
import {
  type ByocPythonVersion,
  codingApi,
  type CodingDeployRequest,
  type CodingDeployResult,
  type CodingRepo,
} from "../../../lib/api";
import { useAgentProjectPick } from "../../../lib/useAgentProjectPick";
import { Alert, Button, Field, Modal, Select } from "../../ui";
import { ProjectField } from "../agents/ProjectCard";
import { deployFailureText } from "./deployError";
import { agentNameFrom } from "./prefs";

type KindChoice = "auto" | "code_zip" | "container_source";
const KINDS: KindChoice[] = ["auto", "code_zip", "container_source"];
const PYTHONS: ByocPythonVersion[] = ["PYTHON_3_13", "PYTHON_3_12", "PYTHON_3_11", "PYTHON_3_10"];

/**
 * Hand the repo to Launchpad: the backend snapshots and exports it, stages the
 * archive as a BYOC upload and creates an Agent — or, when the repo already
 * deployed one, redeploys that Agent — through the ordinary byoc pipeline. The deploy
 * itself runs as the Agent's job; this modal links to its detail page.
 */
export function DeployModal({
  repo,
  open,
  onClose,
  onDeployed,
}: {
  repo: CodingRepo;
  open: boolean;
  onClose: () => void;
  onDeployed: (result: CodingDeployResult) => void;
}) {
  const { t } = useTranslation();
  const redeploy = !!repo.agent_id;
  const [name, setName] = useState(() => agentNameFrom(repo.name));
  const [description, setDescription] = useState(repo.description ?? "");
  const [kind, setKind] = useState<KindChoice>("auto");
  const [entrypoint, setEntrypoint] = useState(BYOC_DEFAULT_ENTRYPOINT);
  const [python, setPython] = useState<ByocPythonVersion>("PYTHON_3_13");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<CodingDeployResult | null>(null);
  const nameValid = redeploy || AGENT_NAME_RE.test(name);
  // a first deploy creates a NEW agent: it names its business project (#356)
  const project = useAgentProjectPick("agents.deploy", open && !redeploy && !result);
  const zipFields = kind !== "container_source";
  const ready = nameValid && !(zipFields && !entrypoint.trim()) && (redeploy || !!project.projectId);

  const submit = async () => {
    if (!ready) return;
    setBusy(true);
    setError(null);
    const body: CodingDeployRequest = {};
    if (!redeploy) {
      body.agent_name = name;
      body.project_id = project.projectId;
    }
    if (description.trim()) body.description = description.trim();
    if (kind !== "auto") body.artifact_kind = kind;
    if (zipFields) {
      body.entrypoint = entrypoint.trim();
      body.python_version = python;
    }
    try {
      const res = await codingApi.deploy(repo.id, body);
      setResult(res);
      onDeployed(res);
    } catch (err) {
      setError(deployFailureText(err, t));
    } finally {
      setBusy(false);
    }
  };

  const agentUrl = (id: string) => `/v2/agents?view=detail&id=${encodeURIComponent(id)}`;

  return (
    <Modal
      open={open}
      title={t(redeploy ? "v2.coding.deploy.redeployTitle" : "v2.coding.deploy.title")}
      onClose={onClose}
      testId="v2-coding-deploy-modal"
      footer={
        result ? (
          <Button kind="primary" onClick={onClose}>
            {t("v2.common.done")}
          </Button>
        ) : (
          <>
            <Button onClick={onClose}>{t("v2.common.cancel")}</Button>
            <Button kind="primary" disabled={busy || !ready} onClick={() => void submit()} testId="v2-coding-deploy-submit">
              {t(busy ? "v2.coding.deploy.deploying" : redeploy ? "v2.coding.deploy.redeploy" : "v2.coding.deploy.submit")}
            </Button>
          </>
        )
      }
    >
      {result ? (
        <Alert tone="success">
          {t(result.created ? "v2.coding.deploy.created" : "v2.coding.deploy.updated", { id: result.agent_id })}{" "}
          <Link to={agentUrl(result.agent_id)} data-testid="v2-coding-deploy-agent">
            {t("v2.coding.deploy.openAgent")}
          </Link>
        </Alert>
      ) : (
        <div className="v2-stack">
          <Alert>{t("v2.coding.deploy.body")}</Alert>
          {error && <Alert tone="error">{error}</Alert>}
          {redeploy ? (
            <Alert>
              {t("v2.coding.deploy.redeployNote")}{" "}
              <Link to={agentUrl(repo.agent_id!)} className="mono">
                {repo.agent_id}
              </Link>
            </Alert>
          ) : (
            <Field
              label={t("v2.coding.deploy.agentName")}
              required
              error={nameValid ? null : t("v2.coding.deploy.nameInvalid")}
              hint={t("v2.coding.deploy.agentNameHint")}
            >
              <input
                className="v2-input mono"
                value={name}
                maxLength={48}
                aria-label={t("v2.coding.deploy.agentName")}
                onChange={(e) => setName(e.target.value.toLowerCase())}
                data-testid="v2-coding-deploy-name"
              />
            </Field>
          )}
          {!redeploy && <ProjectField pick={project} />}
          <Field label={t("v2.coding.deploy.description")}>
            <textarea
              className="v2-textarea"
              rows={2}
              value={description}
              maxLength={500}
              aria-label={t("v2.coding.deploy.description")}
              onChange={(e) => setDescription(e.target.value)}
            />
          </Field>
          <Field label={t("v2.coding.deploy.artifactKind")} hint={t(`v2.coding.deploy.kindHint.${kind}`)}>
            <Select
              value={kind}
              options={KINDS.map((k) => ({ value: k, label: t(`v2.coding.deploy.kind.${k}`) }))}
              onChange={(v) => setKind(v as KindChoice)}
              ariaLabel={t("v2.coding.deploy.artifactKind")}
              testId="v2-coding-deploy-kind"
            />
          </Field>
          {zipFields && (
            <div className="v2-grid-2">
              <Field label={t("v2.coding.deploy.entrypoint")} required>
                <input
                  className="v2-input mono"
                  value={entrypoint}
                  maxLength={255}
                  aria-label={t("v2.coding.deploy.entrypoint")}
                  onChange={(e) => setEntrypoint(e.target.value)}
                />
              </Field>
              <Field label={t("v2.coding.deploy.python")}>
                <Select
                  value={python}
                  options={PYTHONS.map((p) => ({ value: p, label: p.replace("PYTHON_", "Python ").replace("_", ".") }))}
                  onChange={(v) => setPython(v as ByocPythonVersion)}
                  ariaLabel={t("v2.coding.deploy.python")}
                />
              </Field>
            </div>
          )}
        </div>
      )}
    </Modal>
  );
}
