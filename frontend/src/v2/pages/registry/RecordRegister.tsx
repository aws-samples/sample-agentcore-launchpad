import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { api, errorMessage } from "../../../lib/api";
import { REGISTRY_NAME_RE } from "../../../lib/registry";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader, OptionCard } from "../../ui";
import type { RegistryRecord } from "./common";
import { GitSkillImport, StagedSkillImport } from "./SkillImport";

type RegType = "MCP" | "AGENT_SKILLS";
type RegSource = "inline" | "zip" | "git" | "url";
const SOURCES: RegSource[] = ["inline", "zip", "git", "url"];

/** How registration works — DRAFT → submit → approve → attach. */
function HowItWorks() {
  const { t } = useTranslation();
  return (
    <Card title={t("v2.registry.how.title")}>
      <ol className="v2-registry-how">
        {(["s1", "s2", "s3", "s4"] as const).map((step, i) => (
          <li key={step}>
            <span className="n">{i + 1}</span>
            <span>{t(`v2.registry.how.${step}`)}</span>
          </li>
        ))}
      </ol>
      <p className="v2-muted" style={{ fontSize: 13, margin: "12px 0 0" }}>{t("registry.register.how.note")}</p>
    </Card>
  );
}

/** `?view=register[&type=MCP|AGENT_SKILLS]` — register an MCP server or a skill. */
export function RecordRegister() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [params, setParams] = useSearchParams();
  const [regType, setRegType] = useState<RegType>(params.get("type") === "AGENT_SKILLS" ? "AGENT_SKILLS" : "MCP");
  const [source, setSource] = useState<RegSource>("inline");
  const [name, setName] = useState("");
  const [desc, setDesc] = useState("");
  const [url, setUrl] = useState("");
  const [md, setMd] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const nameValid = REGISTRY_NAME_RE.test(name);
  const bodyValid = regType === "MCP" ? /^https?:\/\/.+/.test(url) : md.trim().length > 0;
  // the first unmet predicate is the hint on the disabled button
  const disabledReason = !nameValid
    ? t("registry.register.disabledName")
    : !bodyValid
      ? t(regType === "MCP" ? "registry.register.disabledUrl" : "registry.register.disabledMd")
      : undefined;
  const inline = regType === "MCP" || source === "inline";

  // success tail shared by the inline POST and the zip/url/git imports
  const done = (record: RegistryRecord | null, recordName: string) => {
    toast("success", t("registry.register.done", { name: recordName }));
    setParams(record ? { view: "detail", id: record.record_id } : {});
  };

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const record = await api.registryCreate({
        type: regType,
        name,
        description: desc,
        ...(regType === "MCP" ? { url } : { skill_md: md }),
      });
      done(record, name);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={t("v2.registry.registerTitle")}
        onBack={() => setParams({})}
        end={
          inline ? (
            <Button
              kind="primary"
              disabled={busy || Boolean(disabledReason)}
              title={disabledReason}
              onClick={() => void submit()}
              testId="v2-registry-register-submit"
            >
              {busy ? t("v2.registry.registering") : t("v2.registry.register")}
            </Button>
          ) : undefined
        }
      />
      {error && <Alert tone="error">{error}</Alert>}
      <div className="v2-registry-layout">
        <div>
          <Card title={t("v2.registry.recordType")}>
            <div className="v2-options">
              {(["MCP", "AGENT_SKILLS"] as const).map((k) => (
                <OptionCard
                  key={k}
                  title={t(`v2.registry.regType.${k}`)}
                  desc={t(`v2.registry.regTypeDesc.${k}`)}
                  on={regType === k}
                  onClick={() => setRegType(k)}
                  testId={`v2-registry-type-${k}`}
                />
              ))}
            </div>
            {regType === "AGENT_SKILLS" && (
              <>
                <div className="v2-sub-title">{t("v2.registry.source.label")}</div>
                <div className="v2-options v2-registry-sources">
                  {SOURCES.map((s) => (
                    <OptionCard
                      key={s}
                      title={t(`v2.registry.source.${s}`)}
                      desc={t(`v2.registry.sourceDesc.${s}`)}
                      on={source === s}
                      onClick={() => setSource(s)}
                      testId={`v2-registry-source-${s}`}
                    />
                  ))}
                </div>
              </>
            )}
          </Card>

          {inline && (
            <Card title={t("v2.registry.basic")}>
              <div className="v2-form cols-2">
                <Field
                  label={t("v2.registry.field.name")}
                  required
                  error={name && !nameValid ? t("registry.register.disabledName") : null}
                  hint={t("v2.registry.field.nameHint")}
                >
                  <input
                    className="v2-input mono"
                    value={name}
                    onChange={(e) => setName(e.target.value)}
                    placeholder={regType === "MCP" ? "team-search-mcp" : "report-writer"}
                    data-testid="v2-registry-name"
                  />
                </Field>
                <Field label={t("v2.registry.field.description")}>
                  <input className="v2-input" value={desc} onChange={(e) => setDesc(e.target.value)} data-testid="v2-registry-desc" />
                </Field>
                {regType === "MCP" ? (
                  <Field
                    label={t("v2.registry.field.mcpUrl")}
                    required
                    full
                    hint={t("v2.registry.field.mcpUrlHint")}
                    error={url && !/^https?:\/\/.+/.test(url) ? t("registry.register.disabledUrl") : null}
                  >
                    <input
                      className="v2-input mono"
                      value={url}
                      onChange={(e) => setUrl(e.target.value)}
                      placeholder="https://mcp.example.com/sse"
                      data-testid="v2-registry-url"
                    />
                  </Field>
                ) : (
                  <Field label={t("v2.registry.field.skillMd")} required full hint={t("v2.registry.field.skillMdHint")}>
                    <textarea
                      className="v2-textarea mono"
                      rows={12}
                      value={md}
                      onChange={(e) => setMd(e.target.value)}
                      placeholder={"---\nname: report-writer\ndescription: …\n---\n# Instructions"}
                      data-testid="v2-registry-md"
                    />
                  </Field>
                )}
              </div>
              <Alert>{t("registry.register.note")}</Alert>
            </Card>
          )}
          {regType === "AGENT_SKILLS" && (source === "zip" || source === "url") && (
            <StagedSkillImport source={source} onImported={done} />
          )}
          {regType === "AGENT_SKILLS" && source === "git" && <GitSkillImport onImported={done} />}
        </div>
        <HowItWorks />
      </div>
    </>
  );
}
