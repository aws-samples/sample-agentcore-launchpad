import { ExternalLink } from "lucide-react";
import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage, type WorkspacePreflightResult } from "../../../lib/api";
import { CROSS_ACCOUNT_GUIDE_URL, SPOKE_TEMPLATE_URL } from "../../../lib/links";
import { ROLE_ARN, suggestExternalId, WORKSPACE_REGIONS } from "../../../lib/workspaces";
import { useWorkspace } from "../../../workspace/workspace-context";
import { useLoad, useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader, OptionCard } from "../../ui";

const OTHER = "__other__";

function Steps({ prefix, count }: { prefix: string; count: number }) {
  const { t } = useTranslation();
  return (
    <ol className="v2-workspaces-how">
      {Array.from({ length: count }, (_, i) => (
        <li key={i}>
          <span className="n">{i + 1}</span>
          <span>{t(`${prefix}${i + 1}`)}</span>
        </li>
      ))}
    </ol>
  );
}

/**
 * `?view=new` — register a workspace: this deployment's own account in another
 * region, or another account reached through the spoke role (AssumeRole +
 * ExternalId), with an optional access probe before anything is written.
 */
export function WorkspaceCreate() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [, setParams] = useSearchParams();
  const { refresh: refreshSwitcher } = useWorkspace();
  const list = useLoad(() => api.listWorkspaces(), "workspaces-create");
  const rows = list.data?.workspaces ?? [];
  const hubAccountId = rows.find((w) => w.is_default)?.account_id ?? "";
  const takenRegions = rows.map((w) => w.region);

  const [id, setId] = useState("");
  const [name, setName] = useState("");
  const [choice, setChoice] = useState<string>(OTHER);
  const [freeRegion, setFreeRegion] = useState("");
  const [external, setExternal] = useState(false);
  const [accountId, setAccountId] = useState("");
  const [roleArn, setRoleArn] = useState("");
  const [externalId, setExternalId] = useState("");
  const [hubRoleArn, setHubRoleArn] = useState<string | null>(null);
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);
  /** the last access probe's verdict, or a string when the probe itself failed */
  const [probe, setProbe] = useState<WorkspacePreflightResult | string | null>(null);
  const [probing, setProbing] = useState(false);

  const region = (choice === OTHER ? freeRegion : choice).trim();
  // Only the hub's own account can collide on a region here; a spoke account has
  // its own region space, and the backend's UNIQUE(account, region) decides.
  const taken = !external && takenRegions.includes(region);

  // The spoke's trust policy has to name this hub, so the form shows it — read
  // once, and only when it is needed.
  useEffect(() => {
    if (!external || hubRoleArn !== null) return;
    let alive = true;
    void api
      .getHubIdentity()
      .then((identity) => alive && setHubRoleArn(identity.role_arn))
      .catch(() => alive && setHubRoleArn(""));
    return () => {
      alive = false;
    };
  }, [external, hubRoleArn]);

  // A verdict belongs to the values it was measured on: any edit (including
  // SUGGEST replacing the ExternalId) drops it.
  useEffect(() => {
    setProbe(null);
  }, [accountId, roleArn, externalId, external, region]);

  // The region is part of the probe: STS is called through the target region's
  // own endpoint, so a probe in another region could pass where this one fails.
  const canProbe =
    external &&
    Boolean(accountId.trim() && roleArn.trim() && externalId.trim() && region) &&
    !probing &&
    !submitting;

  const testAccess = async () => {
    setProbing(true);
    try {
      setProbe(
        await api.preflightWorkspace({
          account_id: accountId.trim(),
          region,
          role_arn: roleArn.trim(),
          external_id: externalId.trim(),
        }),
      );
    } catch (err) {
      setProbe(errorMessage(err));
    } finally {
      setProbing(false);
    }
  };

  const submit = async () => {
    const account = external ? accountId.trim() : hubAccountId;
    if (!id.trim() || !name.trim() || !region || !account) {
      setError(t("workspacesPage.create.missing"));
      return;
    }
    if (external) {
      if (!roleArn.trim() || !externalId.trim()) {
        setError(t("workspacesPage.create.missingCrossAccount"));
        return;
      }
      // Caught here as well as by the backend: the account mismatch is the
      // likeliest typo, and the form is where it can still be corrected in place.
      const match = ROLE_ARN.exec(roleArn.trim());
      if (!match) {
        setError(t("workspacesPage.create.badRoleArn"));
        return;
      }
      if (match[1] !== account) {
        setError(t("workspacesPage.create.roleAccountMismatch"));
        return;
      }
    }
    setError("");
    setSubmitting(true);
    try {
      const created = await api.createWorkspace({
        id: id.trim(),
        name: name.trim(),
        account_id: account,
        region,
        ...(external ? { role_arn: roleArn.trim(), external_id: externalId.trim() } : {}),
      });
      await refreshSwitcher();
      toast("success", t("workspacesPage.created", { name: created.name }));
      setParams({ view: "detail", id: created.id }, { replace: true });
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setSubmitting(false);
    }
  };

  const back = () => setParams({});

  return (
    <>
      <FlowHeader
        title={t("v2.workspaces.createTitle")}
        onBack={back}
        end={
          <>
            <Button onClick={back} disabled={submitting}>
              {t("v2.common.cancel")}
            </Button>
            <Button kind="primary" onClick={() => void submit()} disabled={submitting || taken} testId="v2-ws-submit">
              {t(submitting ? "v2.workspaces.submitting" : "v2.workspaces.submit")}
            </Button>
          </>
        }
      />
      {error && (
        <Alert tone="error">
          <span data-testid="v2-ws-create-error">{error}</span>
        </Alert>
      )}
      <div className="v2-workspaces-layout">
        <div className="v2-stack" style={{ gap: 16 }}>
          <Card title={t("v2.workspaces.basic")}>
            <div className="v2-form cols-2">
              <Field label={t("v2.workspaces.field.id")} required hint={t("workspacesPage.create.idHint")}>
                <input
                  className="v2-input mono"
                  value={id}
                  onChange={(e) => setId(e.target.value)}
                  placeholder="acct2-usw2"
                  disabled={submitting}
                  data-testid="v2-ws-id"
                  autoFocus
                />
              </Field>
              <Field label={t("v2.workspaces.field.name")} required>
                <input
                  className="v2-input"
                  value={name}
                  onChange={(e) => setName(e.target.value)}
                  placeholder={t("workspacesPage.create.namePlaceholder")}
                  disabled={submitting}
                  data-testid="v2-ws-name"
                />
              </Field>
            </div>
          </Card>

          <Card title={t("v2.workspaces.target")}>
            <div className="v2-options">
              <OptionCard
                title={t("v2.workspaces.mode.local")}
                desc={t("v2.workspaces.mode.localDesc")}
                on={!external}
                disabled={submitting}
                onClick={() => setExternal(false)}
                testId="v2-ws-mode-local"
              />
              <OptionCard
                title={t("v2.workspaces.mode.external")}
                desc={t("v2.workspaces.mode.externalDesc")}
                on={external}
                disabled={submitting}
                onClick={() => setExternal(true)}
                testId="v2-ws-mode-external"
              />
            </div>
            <div className="v2-form cols-2" style={{ marginTop: 18 }}>
              <Field
                label={t("v2.workspaces.field.account")}
                required
                hint={t(external ? "workspacesPage.create.accountHintExternal" : "v2.workspaces.accountHint")}
              >
                <input
                  className="v2-input mono"
                  value={external ? accountId : hubAccountId || "—"}
                  onChange={(e) => setAccountId(e.target.value)}
                  placeholder="123456789012"
                  readOnly={!external}
                  disabled={!external || submitting}
                  data-testid="v2-ws-account"
                />
              </Field>
              <Field label={t("v2.workspaces.field.region")} required hint={t("workspacesPage.create.regionHint")}>
                <div className="v2-stack">
                  <select
                    className="v2-select"
                    value={choice}
                    onChange={(e) => setChoice(e.target.value)}
                    disabled={submitting}
                    data-testid="v2-ws-region-select"
                  >
                    <option value={OTHER}>{t("v2.workspaces.regionOther")}</option>
                    {WORKSPACE_REGIONS.map((option) => (
                      <option key={option} value={option}>
                        {option}
                        {/* "in use" is about THIS account's regions */}
                        {!external && takenRegions.includes(option) ? ` · ${t("workspacesPage.create.regionTaken")}` : ""}
                      </option>
                    ))}
                  </select>
                  {choice === OTHER && (
                    <input
                      className="v2-input mono"
                      value={freeRegion}
                      onChange={(e) => setFreeRegion(e.target.value)}
                      placeholder="eu-central-1"
                      disabled={submitting}
                      aria-label={t("v2.workspaces.field.region")}
                      data-testid="v2-ws-region"
                    />
                  )}
                </div>
              </Field>
              {external && (
                <>
                  <Field label={t("v2.workspaces.field.roleArn")} required full hint={t("workspacesPage.create.roleArnHint")}>
                    <input
                      className="v2-input mono"
                      value={roleArn}
                      onChange={(e) => setRoleArn(e.target.value)}
                      placeholder="arn:aws:iam::123456789012:role/LaunchpadWorkspaceRole"
                      disabled={submitting}
                      data-testid="v2-ws-role-arn"
                    />
                  </Field>
                  <Field label={t("v2.workspaces.field.externalId")} required full hint={t("workspacesPage.create.externalIdHint")}>
                    <div className="v2-row" style={{ flexWrap: "nowrap" }}>
                      <input
                        className="v2-input mono"
                        value={externalId}
                        onChange={(e) => setExternalId(e.target.value)}
                        disabled={submitting}
                        data-testid="v2-ws-external-id"
                      />
                      <Button
                        disabled={submitting}
                        title={t("workspacesPage.create.externalIdSuggestTitle")}
                        onClick={() => setExternalId(suggestExternalId())}
                        testId="v2-ws-external-id-suggest"
                      >
                        {t("v2.workspaces.suggest")}
                      </Button>
                    </div>
                  </Field>
                </>
              )}
            </div>
            {external && (
              <div className="v2-stack" style={{ marginTop: 16 }}>
                {/* SUGGEST reads like "fill this in for me", so an operator joining an
                    existing stack would only learn of the mismatch at bootstrap. */}
                <Alert tone="warn">
                  <span data-testid="v2-ws-suggest-note">{t("workspacesPage.create.externalIdSuggestNote")}</span>
                </Alert>
                <Alert>
                  <span data-testid="v2-ws-hub-role">
                    {t("workspacesPage.create.hubRoleNote")}{" "}
                    <span className="mono v2-workspaces-break">
                      {hubRoleArn === null
                        ? t("workspacesPage.create.hubRoleLoading")
                        : hubRoleArn || t("workspacesPage.create.hubRoleUnknown")}
                    </span>
                  </span>
                </Alert>
              </div>
            )}
            {taken && (
              <Alert tone="warn">
                <span data-testid="v2-ws-region-taken">{t("workspacesPage.create.regionTakenNote", { region })}</span>
              </Alert>
            )}
          </Card>

          {/* Below the region on purpose: the probe assumes the role through the
              target region's own STS endpoint, so every value it needs is above. */}
          {external && (
            <Card title={t("v2.workspaces.probeTitle")} sub={t("workspacesPage.create.testAccessHint")}>
              <div className="v2-row">
                <Button disabled={!canProbe} onClick={() => void testAccess()} testId="v2-ws-preflight">
                  {t(probing ? "v2.workspaces.testing" : "v2.workspaces.testAccess")}
                </Button>
              </div>
              {probe !== null && (
                <div style={{ marginTop: 12 }}>
                  {typeof probe === "string" || !probe.ok ? (
                    <Alert tone="error">
                      <span className="mono v2-workspaces-break" data-testid="v2-ws-preflight-fail">
                        {typeof probe === "string" ? probe : probe.diagnostic}
                      </span>
                    </Alert>
                  ) : (
                    <Alert tone="success">
                      <span data-testid="v2-ws-preflight-ok">
                        {t("workspacesPage.create.testAccessOk", {
                          account: probe.caller_account ?? accountId.trim(),
                        })}
                      </span>
                    </Alert>
                  )}
                </div>
              )}
            </Card>
          )}
        </div>

        <div className="v2-stack" style={{ gap: 16 }}>
          <Card title={t("v2.workspaces.howTitle")}>
            <Steps prefix="workspacesPage.create.how" count={4} />
            <p className="v2-workspaces-note">{t("workspacesPage.create.howNote")}</p>
          </Card>
          {/* The cross-account flow spans two accounts and a file this console
              cannot hand over — the spoke deployer is often someone else. */}
          {external && (
            <Card title={t("v2.workspaces.xaTitle")} sub={t("workspacesPage.create.xaSub")} testId="v2-ws-xa">
              <Steps prefix="workspacesPage.create.xaStep" count={3} />
              <Alert tone="warn">{t("workspacesPage.create.xaRevoke")}</Alert>
              <div className="v2-row">
                <a className="v2-workspaces-ext" href={CROSS_ACCOUNT_GUIDE_URL} target="_blank" rel="noreferrer">
                  {t("v2.workspaces.guideLink")} <ExternalLink size={12} aria-hidden="true" />
                </a>
                <a className="v2-workspaces-ext" href={SPOKE_TEMPLATE_URL} target="_blank" rel="noreferrer">
                  {t("v2.workspaces.templateLink")} <ExternalLink size={12} aria-hidden="true" />
                </a>
              </div>
            </Card>
          )}
        </div>
      </div>
    </>
  );
}
