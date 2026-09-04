import type { CSSProperties } from "react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Panel, useToast, ViewHead } from "../../components";
import type { RegistryRecord } from "../Registry";
import type { RegSource } from "./SkillSourceForm";
import { SkillSourceForm } from "./SkillSourceForm";

interface RegisterViewProps {
  /** success tail: parent returns to the list, toasts, reloads, and selects the record */
  onDone: (record: RegistryRecord | null, name: string) => void;
  onBack: () => void;
  /** preselect the record type from the active list tab (A2A tab → default MCP) */
  initialType?: "MCP" | "AGENT_SKILLS";
}

/**
 * Standalone `?view=register` sub-page (Evaluation `?view=new` layout): the
 * register form on the left, a DRAFT→approve flow explainer on the right.
 * Owns its own form state — the inline/MCP POST lives here; zip/git/url import
 * routes through SkillSourceForm's onImported. Both success paths call onDone.
 */
export function RegisterView({ onDone, onBack, initialType = "MCP" }: RegisterViewProps) {
  const { t } = useTranslation();
  const toast = useToast();
  const [regType, setRegType] = useState<"MCP" | "AGENT_SKILLS">(initialType);
  const [regSource, setRegSource] = useState<RegSource>("inline");
  const [regName, setRegName] = useState("");
  const [regDesc, setRegDesc] = useState("");
  const [regUrl, setRegUrl] = useState("");
  const [regMd, setRegMd] = useState("");
  const [busy, setBusy] = useState(false);

  const regNameValid = /^[a-z][a-z0-9-]{2,63}$/.test(regName);
  const regBodyValid = regType === "MCP" ? /^https?:\/\/.+/.test(regUrl) : regMd.trim().length > 0;
  const regValid = regNameValid && regBodyValid;
  // Same predicates as `regValid`, read in order: the first unmet one is the hint.
  const regDisabledReason = !regNameValid
    ? t("registry.register.disabledName")
    : !regBodyValid
      ? t(regType === "MCP" ? "registry.register.disabledUrl" : "registry.register.disabledMd")
      : undefined;

  const submitRegistration = async () => {
    setBusy(true);
    try {
      const res = await fetch("/api/registry/records", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          type: regType,
          name: regName,
          description: regDesc,
          ...(regType === "MCP" ? { url: regUrl } : { skill_md: regMd }),
        }),
      });
      const body = (await res.json().catch(() => ({}))) as RegistryRecord & {
        message?: string;
      };
      if (!res.ok) {
        toast(t("common.actionFailed", { msg: body.message ?? `HTTP ${res.status}` }));
        return;
      }
      onDone(body, regName);
    } catch (err) {
      toast(t("common.actionFailed", { msg: String(err) }));
    } finally {
      setBusy(false);
    }
  };

  // zip/git/url render their own submit inside SkillSourceForm; the inline/MCP
  // branch owns the name/desc fields + note + register button.
  const inlineBranch = regType === "MCP" || regSource === "inline";

  return (
    <section>
      <ViewHead
        kicker={t("registry.kicker")}
        title={t("registry.register.pageTitle")}
        meta={t("registry.register.pageMeta")}
      />
      <div style={{ marginBottom: 14 }}>
        <Btn onClick={onBack}>◂ {t("registry.title")}</Btn>
      </div>
      <div className="eval-grid">
        <Panel
          brk
          title={t("registry.register.title")}
          sub={t("registry.register.pageMeta")}
          style={{ "--i": 0 } as CSSProperties}
        >
          <div data-testid="register-form">
            <div className="field">
              <label>{t("registry.register.type")}</label>
              <div className="selchips">
                {(["MCP", "AGENT_SKILLS"] as const).map((k) => (
                  <button
                    key={k}
                    type="button"
                    className={`selchip${regType === k ? " on" : ""}`}
                    style={{ cursor: "pointer" }}
                    onClick={() => setRegType(k)}
                  >
                    {k === "MCP"
                      ? t("registry.register.typeMcp")
                      : t("registry.register.typeSkill")}
                  </button>
                ))}
              </div>
            </div>
            {inlineBranch && (
              <>
                <div className="field">
                  <label htmlFor="reg-name">{t("registry.register.name")}</label>
                  <input
                    id="reg-name"
                    className="input mono"
                    value={regName}
                    onChange={(e) => setRegName(e.target.value)}
                    placeholder={regType === "MCP" ? "team-search-mcp" : "report-writer"}
                  />
                </div>
                <div className="field">
                  <label htmlFor="reg-desc">{t("registry.register.description")}</label>
                  <input
                    id="reg-desc"
                    className="input"
                    value={regDesc}
                    onChange={(e) => setRegDesc(e.target.value)}
                  />
                </div>
              </>
            )}
            {regType === "MCP" ? (
              <div className="field">
                <label htmlFor="reg-url">{t("registry.register.url")}</label>
                <input
                  id="reg-url"
                  className="input mono"
                  value={regUrl}
                  onChange={(e) => setRegUrl(e.target.value)}
                  placeholder="https://mcp.example.com/sse"
                />
              </div>
            ) : (
              <SkillSourceForm
                source={regSource}
                onSourceChange={setRegSource}
                md={regMd}
                onMdChange={setRegMd}
                onImported={(record, name) => onDone(record, name)}
              />
            )}
            {inlineBranch && (
              <>
                <div className="note">
                  <span className="i">[i]</span>
                  <span>{t("registry.register.note")}</span>
                </div>
                <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
                  <Btn
                    primary
                    disabled={busy || !regValid}
                    disabledReason={regDisabledReason}
                    onClick={() => void submitRegistration()}
                  >
                    ▲ {t("registry.register.submit")}
                  </Btn>
                </div>
              </>
            )}
          </div>
        </Panel>

        <Panel
          title={t("registry.register.how.title")}
          sub={t("registry.register.how.sub")}
          style={{ "--i": 1 } as CSSProperties}
        >
          {(["s1", "s2", "s3", "s4"] as const).map((step, i) => (
            <div className="kv" key={step}>
              <span className="k mono">{`0${i + 1}`}</span>
              <span className="v" style={{ textAlign: "left", flex: 1, marginLeft: 12 }}>
                {t(`registry.register.how.${step}`)}
              </span>
            </div>
          ))}
          <div className="note" style={{ marginTop: 10 }}>
            <span className="i">[i]</span>
            <span>{t("registry.register.how.note")}</span>
          </div>
        </Panel>
      </div>
    </section>
  );
}
