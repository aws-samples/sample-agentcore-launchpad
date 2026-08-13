import { type CSSProperties, type FormEvent, useEffect, useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Panel, ViewHead } from "../../components";
import { api, type Workspace } from "../../lib/api";

/**
 * Regions AgentCore serves. The list is a convenience, not the authority: the
 * bootstrap job's `validate-access` stage probes the target for real, so an
 * operator can type a region this build has not heard of.
 */
const REGIONS = [
  "us-west-2",
  "us-east-1",
  "us-east-2",
  "eu-central-1",
  "eu-west-1",
  "ap-southeast-2",
  "ap-northeast-1",
] as const;

const OTHER = "__other__";

const ROLE_ARN = /^arn:aws[a-z-]*:iam::(\d{12}):role\/.+$/;

/**
 * A suggestion for the ExternalId, not a secret the backend knows: the operator
 * has to deploy the spoke stack with the same value, so it is theirs to keep.
 * `crypto.randomUUID` is available in every browser this console supports.
 */
function suggestExternalId(): string {
  return `launchpad-${crypto.randomUUID().replace(/-/g, "").slice(0, 20)}`;
}

export function CreateWorkspaceView({
  hubAccountId,
  takenRegions,
  onBack,
  onDone,
}: {
  /** The hub's own account: the default, and the only one for a local workspace. */
  hubAccountId: string;
  takenRegions: string[];
  onBack: () => void;
  onDone: (created: Workspace) => void | Promise<void>;
}) {
  const { t } = useTranslation();
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

  const region = (choice === OTHER ? freeRegion : choice).trim();
  // Only the hub's own account can collide on a region here; a spoke account has
  // its own region space, and the backend's UNIQUE(account, region) decides.
  const taken = !external && takenRegions.includes(region);

  // The spoke's trust policy has to name this hub, so the form shows it. Read
  // once, and only when it is actually needed.
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

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
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
        ...(external
          ? { role_arn: roleArn.trim(), external_id: externalId.trim() }
          : {}),
      });
      await onDone(created);
    } catch (err) {
      setError(err instanceof Error ? err.message : String(err));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <ViewHead
        kicker={t("workspacesPage.kicker")}
        title={t("workspacesPage.create.title")}
        meta={t("workspacesPage.create.meta")}
      />
      <div className="eval-grid">
        <Panel brk title={t("workspacesPage.create.formTitle")} style={{ "--i": 0 } as CSSProperties}>
          <form onSubmit={submit} noValidate>
            <div className="field">
              <label htmlFor="ws-id">{t("workspacesPage.create.idLabel")}</label>
              <input
                id="ws-id"
                className="input"
                value={id}
                onChange={(event) => setId(event.target.value)}
                placeholder="acct2-usw2"
                disabled={submitting}
                data-testid="ws-id-input"
                autoFocus
              />
              <span className="fhint">{t("workspacesPage.create.idHint")}</span>
            </div>
            <div className="field">
              <label htmlFor="ws-name">{t("workspacesPage.create.nameLabel")}</label>
              <input
                id="ws-name"
                className="input"
                value={name}
                onChange={(event) => setName(event.target.value)}
                placeholder={t("workspacesPage.create.namePlaceholder")}
                disabled={submitting}
                data-testid="ws-name-input"
              />
            </div>
            <div className="field">
              <label className="studio-check" style={{ cursor: "pointer" }}>
                <input
                  type="checkbox"
                  checked={external}
                  disabled={submitting}
                  onChange={(event) => setExternal(event.target.checked)}
                  data-testid="ws-external-toggle"
                />
                <span>{t("workspacesPage.create.externalLabel")}</span>
              </label>
              <span className="fhint">{t("workspacesPage.create.externalHint")}</span>
            </div>
            <div className="field">
              <label htmlFor="ws-account">{t("workspacesPage.create.accountLabel")}</label>
              <input
                id="ws-account"
                className="input mono"
                value={external ? accountId : hubAccountId || "—"}
                onChange={(event) => setAccountId(event.target.value)}
                placeholder="123456789012"
                readOnly={!external}
                disabled={!external || submitting}
                data-testid="ws-account-input"
              />
              <span className="fhint">
                {t(
                  external
                    ? "workspacesPage.create.accountHintExternal"
                    : "workspacesPage.create.accountHint",
                )}
              </span>
            </div>
            {external ? (
              <>
                <div className="field">
                  <label htmlFor="ws-role-arn">{t("workspacesPage.create.roleArnLabel")}</label>
                  <input
                    id="ws-role-arn"
                    className="input mono"
                    value={roleArn}
                    onChange={(event) => setRoleArn(event.target.value)}
                    placeholder="arn:aws:iam::123456789012:role/LaunchpadWorkspaceRole"
                    disabled={submitting}
                    data-testid="ws-role-arn-input"
                  />
                  <span className="fhint">{t("workspacesPage.create.roleArnHint")}</span>
                </div>
                <div className="field">
                  <label htmlFor="ws-external-id">
                    {t("workspacesPage.create.externalIdLabel")}
                  </label>
                  <div style={{ display: "flex", gap: 8 }}>
                    <input
                      id="ws-external-id"
                      className="input mono"
                      value={externalId}
                      onChange={(event) => setExternalId(event.target.value)}
                      disabled={submitting}
                      data-testid="ws-external-id-input"
                    />
                    <Btn
                      type="button"
                      disabled={submitting}
                      onClick={() => setExternalId(suggestExternalId())}
                      data-testid="ws-external-id-suggest"
                    >
                      {t("workspacesPage.create.externalIdSuggest")}
                    </Btn>
                  </div>
                  <span className="fhint">{t("workspacesPage.create.externalIdHint")}</span>
                </div>
                <div className="note" data-testid="ws-hub-role">
                  <span className="i">[i]</span>
                  <span>
                    {t("workspacesPage.create.hubRoleNote")}{" "}
                    <span className="mono">
                      {hubRoleArn === null
                        ? t("workspacesPage.create.hubRoleLoading")
                        : hubRoleArn || t("workspacesPage.create.hubRoleUnknown")}
                    </span>
                  </span>
                </div>
              </>
            ) : null}
            <div className="field">
              <label htmlFor="ws-region">{t("workspacesPage.create.regionLabel")}</label>
              <select
                id="ws-region"
                className="input"
                value={choice}
                onChange={(event) => setChoice(event.target.value)}
                disabled={submitting}
                data-testid="ws-region-select"
              >
                <option value={OTHER}>{t("workspacesPage.create.regionOther")}</option>
                {REGIONS.map((option) => (
                  <option key={option} value={option}>
                    {option}
                    {/* "in use" is about THIS account's regions; another
                        account's region space is its own */}
                    {!external && takenRegions.includes(option)
                      ? ` · ${t("workspacesPage.create.regionTaken")}`
                      : ""}
                  </option>
                ))}
              </select>
              {choice === OTHER ? (
                <input
                  className="input mono"
                  value={freeRegion}
                  onChange={(event) => setFreeRegion(event.target.value)}
                  placeholder="eu-central-1"
                  disabled={submitting}
                  data-testid="ws-region-input"
                  aria-label={t("workspacesPage.create.regionLabel")}
                />
              ) : null}
              <span className="fhint">{t("workspacesPage.create.regionHint")}</span>
            </div>
            {taken ? (
              <div className="note" data-testid="ws-region-taken">
                <span className="i">[!]</span>
                {t("workspacesPage.create.regionTakenNote", { region })}
              </div>
            ) : null}
            {error ? (
              <div
                className="note"
                style={{ borderColor: "var(--crit)" }}
                role="alert"
                data-testid="ws-create-error"
              >
                <span className="i" style={{ color: "var(--crit)" }}>
                  [✕]
                </span>
                <span className="mono">{error}</span>
              </div>
            ) : null}
            <div style={{ display: "flex", gap: 10, justifyContent: "flex-end", marginTop: 14 }}>
              <Btn type="button" onClick={onBack} disabled={submitting}>
                {t("workspacesPage.back")}
              </Btn>
              <Btn primary type="submit" disabled={submitting || taken} data-testid="ws-create-submit">
                {submitting
                  ? t("workspacesPage.create.submitting")
                  : t("workspacesPage.create.submit")}
              </Btn>
            </div>
          </form>
        </Panel>

        <Panel brk title={t("workspacesPage.create.howTitle")} style={{ "--i": 1 } as CSSProperties}>
          {[1, 2, 3, 4].map((step) => (
            <div className="kv" key={step}>
              <span className="k">{step}</span>
              <span className="v">{t(`workspacesPage.create.how${step}`)}</span>
            </div>
          ))}
          <div className="note">
            <span className="i">[i]</span>
            {t("workspacesPage.create.howNote")}
          </div>
          {external ? (
            <div className="note" data-testid="ws-external-how">
              <span className="i">[!]</span>
              {t("workspacesPage.create.externalHowNote")}
            </div>
          ) : null}
        </Panel>
      </div>
    </>
  );
}
