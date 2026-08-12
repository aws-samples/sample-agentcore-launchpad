import { type CSSProperties, type FormEvent, useState } from "react";
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

export function CreateWorkspaceView({
  hubAccountId,
  takenRegions,
  onBack,
  onDone,
}: {
  /** Phase 2 registers same-account environments only, so the account is fixed. */
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
  const [error, setError] = useState("");
  const [submitting, setSubmitting] = useState(false);

  const region = (choice === OTHER ? freeRegion : choice).trim();
  const taken = takenRegions.includes(region);

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!id.trim() || !name.trim() || !region) {
      setError(t("workspacesPage.create.missing"));
      return;
    }
    setError("");
    setSubmitting(true);
    try {
      const created = await api.createWorkspace({
        id: id.trim(),
        name: name.trim(),
        account_id: hubAccountId,
        region,
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
              <label htmlFor="ws-account">{t("workspacesPage.create.accountLabel")}</label>
              <input
                id="ws-account"
                className="input mono"
                value={hubAccountId || "—"}
                readOnly
                disabled
                data-testid="ws-account-input"
              />
              <span className="fhint">{t("workspacesPage.create.accountHint")}</span>
            </div>
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
                    {takenRegions.includes(option)
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
        </Panel>
      </div>
    </>
  );
}
