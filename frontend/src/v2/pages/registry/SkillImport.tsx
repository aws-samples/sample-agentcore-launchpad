import { Upload } from "lucide-react";
import { type ChangeEvent, useCallback, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  api,
  errorMessage,
  type RegistryImportSelection,
  type RegistryInspectedSkill,
  type RegistryInspectResponse,
} from "../../../lib/api";
import { REGISTRY_NAME_RE } from "../../../lib/registry";
import { useLoad } from "../../hooks";
import { Alert, Button, Card, type Column, Field, Table, Tag } from "../../ui";
import { isStagingExpired, type RegistryRecord } from "./common";

const HTTPS_RE = /^https:\/\/.+/i;

export type OnImported = (record: RegistryRecord | null, name: string) => void;

/** Shared preview of one staged skill (zip upload or url fetch): editable name +
 *  description, version, file list, validation, then import. */
function StagedPreview({
  stagingId,
  skill,
  onReset,
  onImported,
  onExpired,
}: {
  stagingId: string;
  skill: RegistryInspectedSkill;
  onReset: () => void;
  onImported: OnImported;
  onExpired: () => void;
}) {
  const { t } = useTranslation();
  const [name, setName] = useState(skill.name ?? "");
  const [desc, setDesc] = useState(skill.description ?? "");
  const [importing, setImporting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const nameValid = REGISTRY_NAME_RE.test(name);

  const run = async () => {
    setImporting(true);
    setError(null);
    try {
      const selection: RegistryImportSelection = { index: skill.index ?? 0, name: skill.name };
      if (name !== skill.name) selection.name_override = name;
      if (desc !== skill.description) selection.description_override = desc;
      const body = await api.registryImport(stagingId, [selection]);
      const result = body.records?.[0];
      if (!result || !result.ok) {
        setError(result?.error ?? t("registry.register.importFailed", { msg: "unknown" }));
        return;
      }
      onImported(result.record ?? null, name);
    } catch (err) {
      if (isStagingExpired(err)) onExpired();
      else setError(errorMessage(err));
    } finally {
      setImporting(false);
    }
  };

  return (
    <Card
      title={t("v2.registry.preview")}
      sub={t("v2.registry.previewSub")}
      testId="v2-registry-preview"
      end={
        <span className="v2-row">
          <Button size="sm" disabled={importing} onClick={onReset}>
            {t("v2.registry.chooseAnother")}
          </Button>
          <Button
            kind="primary"
            size="sm"
            disabled={importing || !skill.valid || !nameValid}
            title={!skill.valid ? t("registry.register.invalidBundle") : !nameValid ? t("registry.register.disabledName") : undefined}
            onClick={() => void run()}
            testId="v2-registry-import"
          >
            {importing ? t("v2.registry.importing") : t("v2.registry.register")}
          </Button>
        </span>
      }
    >
      {(!skill.valid || skill.errors.length > 0) && (
        <Alert tone="error">{skill.errors.join("; ") || t("registry.register.invalidBundle")}</Alert>
      )}
      {error && <Alert tone="error">{error}</Alert>}
      <div className="v2-form cols-2">
        <Field
          label={t("v2.registry.field.name")}
          required
          error={name && !nameValid ? t("registry.register.disabledName") : null}
          hint={t("v2.registry.field.nameHint")}
        >
          <input className="v2-input mono" value={name} onChange={(e) => setName(e.target.value)} data-testid="v2-registry-preview-name" />
        </Field>
        <Field label={t("v2.registry.field.version")}>
          <input className="v2-input mono" value={skill.version || "—"} disabled />
        </Field>
        <Field label={t("v2.registry.field.description")} full>
          <input className="v2-input" value={desc} onChange={(e) => setDesc(e.target.value)} />
        </Field>
        <Field label={t("v2.registry.files", { n: skill.files.length })} full>
          <pre className="v2-pre" style={{ maxHeight: 180 }} data-testid="v2-registry-preview-files">{skill.files.join("\n")}</pre>
        </Field>
      </div>
    </Card>
  );
}

/** Zip upload or url fetch → inspect (staging) → preview → import. */
export function StagedSkillImport({ source, onImported }: { source: "zip" | "url"; onImported: OnImported }) {
  const { t } = useTranslation();
  const fileRef = useRef<HTMLInputElement>(null);
  const [staged, setStaged] = useState<{ stagingId: string; skill: RegistryInspectedSkill } | null>(null);
  const [inspecting, setInspecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [url, setUrl] = useState("");

  const reset = useCallback(() => {
    setStaged(null);
    setInspecting(false);
    setError(null);
    setUrl("");
    if (fileRef.current) fileRef.current.value = "";
  }, []);
  // each source starts fresh; leaving zip for url (or back) drops the staged preview
  useEffect(() => reset(), [source, reset]);

  const accept = (body: RegistryInspectResponse, noSkill: string) => {
    const skill = body.skills?.[0];
    if (!skill) {
      setError(noSkill);
      return;
    }
    setStaged({ stagingId: body.staging_id, skill });
  };

  const inspect = async (fetcher: () => Promise<RegistryInspectResponse>, noSkill: string) => {
    setError(null);
    setStaged(null);
    setInspecting(true);
    try {
      accept(await fetcher(), noSkill);
    } catch (err) {
      setError(isStagingExpired(err) ? t("registry.register.previewExpired") : errorMessage(err));
    } finally {
      setInspecting(false);
    }
  };

  const pick = (e: ChangeEvent<HTMLInputElement>) => {
    const file = e.target.files?.[0];
    if (fileRef.current) fileRef.current.value = "";
    if (file) void inspect(() => api.registryInspectZip(file), t("registry.register.zipNoSkill"));
  };

  const trimmed = url.trim();
  const urlValid = HTTPS_RE.test(trimmed);
  const fetchUrl = () => {
    if (urlValid) void inspect(() => api.registryInspectSource({ kind: "url", url: trimmed }), t("registry.register.urlNoSkill"));
  };

  if (staged) {
    return (
      <StagedPreview
        key={staged.stagingId}
        stagingId={staged.stagingId}
        skill={staged.skill}
        onReset={reset}
        onImported={onImported}
        onExpired={() => {
          reset();
          setError(t("registry.register.previewExpired"));
        }}
      />
    );
  }

  return (
    <Card title={t(source === "zip" ? "v2.registry.zipTitle" : "v2.registry.urlTitle")} sub={t(source === "zip" ? "v2.registry.zipSub" : "v2.registry.urlSub")}>
      {error && <Alert tone="error">{error}</Alert>}
      {source === "zip" ? (
        <label className="v2-registry-drop" data-testid="v2-registry-zip-pick">
          <Upload size={22} aria-hidden="true" />
          <span>{inspecting ? t("registry.register.zipInspecting") : t("v2.registry.zipPick")}</span>
          <span className="v2-muted">{t("v2.registry.zipLimit")}</span>
          <input
            ref={fileRef}
            type="file"
            accept=".zip"
            disabled={inspecting}
            onChange={pick}
            data-testid="v2-registry-zip-input"
          />
        </label>
      ) : (
        <div className="v2-form">
          <Field
            label={t("v2.registry.field.url")}
            required
            error={trimmed && !urlValid ? t("registry.register.urlInvalid") : null}
            hint={t("v2.registry.field.urlHint")}
          >
            <div className="v2-row" style={{ flexWrap: "nowrap" }}>
              <input
                className="v2-input mono"
                value={url}
                onChange={(e) => setUrl(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && fetchUrl()}
                placeholder="https://example.com/SKILL.md · https://example.com/bundle.zip"
                data-testid="v2-registry-url-input"
              />
              <Button kind="primary" disabled={inspecting || !urlValid} onClick={fetchUrl} testId="v2-registry-url-fetch">
                {inspecting ? t("registry.register.urlFetching") : t("v2.registry.fetch")}
              </Button>
            </div>
          </Field>
        </div>
      )}
    </Card>
  );
}

// ─── git: scan a repo (one or many SKILL.md), batch-import the selected ones ───
type GitSkill = RegistryInspectedSkill & { index: number };
type RowResult = { ok: boolean; error?: string };

export function GitSkillImport({ onImported }: { onImported: OnImported }) {
  const { t } = useTranslation();
  const [installTick, setInstallTick] = useState(0);
  // capabilities unknown (older server / failure) → no banner, scanning still allowed
  const caps = useLoad(async () => {
    try {
      return (await api.registrySkillCapabilities()).git ?? null;
    } catch {
      return null;
    }
  }, `registry-git-caps:${installTick}`);
  const [url, setUrl] = useState("");
  const [ref, setRef] = useState("");
  const [subdir, setSubdir] = useState("");
  const [token, setToken] = useState("");
  const [scanning, setScanning] = useState(false);
  const [scanError, setScanError] = useState<string | null>(null);
  const [stagingId, setStagingId] = useState<string | null>(null);
  const [skills, setSkills] = useState<GitSkill[] | null>(null);
  const [checked, setChecked] = useState<Record<number, boolean>>({});
  const [names, setNames] = useState<Record<number, string>>({});
  const [results, setResults] = useState<Record<number, RowResult>>({});
  const [importing, setImporting] = useState(false);
  const [installing, setInstalling] = useState(false);
  const [installMsg, setInstallMsg] = useState<{ ok: boolean; text: string } | null>(null);

  const scan = async () => {
    if (!url.trim()) return;
    setScanning(true);
    setScanError(null);
    setSkills(null);
    setStagingId(null);
    setResults({});
    try {
      const body = await api.registryInspectSource({
        kind: "git",
        url: url.trim(),
        ...(ref.trim() ? { ref: ref.trim() } : {}),
        ...(subdir.trim() ? { subdir: subdir.trim() } : {}),
        ...(token.trim() ? { token: token.trim() } : {}),
      });
      const list = (body.skills ?? []).map((s, i) => ({ ...s, index: s.index ?? i }));
      if (list.length === 0) {
        setScanError(t("registry.register.gitNoSkill"));
        return;
      }
      setStagingId(body.staging_id);
      setSkills(list);
      setNames(Object.fromEntries(list.map((s) => [s.index, s.name])));
      setChecked({});
    } catch (err) {
      setScanError(errorMessage(err));
    } finally {
      setScanning(false);
    }
  };

  const selectable = (skills ?? []).filter((s) => s.valid && results[s.index]?.ok !== true);
  const targets = selectable.filter((s) => checked[s.index]);
  const namesValid = targets.every((s) => REGISTRY_NAME_RE.test(names[s.index] ?? s.name));
  const canImport = !importing && targets.length > 0 && namesValid;
  const allChecked = selectable.length > 0 && selectable.every((s) => checked[s.index]);

  const runImport = async () => {
    if (!stagingId || targets.length === 0) return;
    setImporting(true);
    try {
      const selections: RegistryImportSelection[] = targets.map((s) => {
        const nm = names[s.index] ?? s.name;
        return nm !== s.name ? { index: s.index, name: s.name, name_override: nm } : { index: s.index, name: s.name };
      });
      const body = await api.registryImport(stagingId, selections);
      const items = body.records ?? [];
      // response order mirrors the selections order → map back onto the sent targets
      const next = { ...results };
      items.forEach((item, i) => {
        const target = targets[i];
        if (target) next[target.index] = { ok: item.ok, error: item.error };
      });
      setResults(next);
      if (items.length > 0 && items.every((r) => r.ok)) {
        const first = items.find((r) => r.record) ?? items[0];
        onImported(first.record ?? null, first.name);
      }
    } catch (err) {
      if (isStagingExpired(err)) {
        setScanError(t("registry.register.previewExpired"));
        setSkills(null);
        setStagingId(null);
      } else {
        setScanError(errorMessage(err));
      }
    } finally {
      setImporting(false);
    }
  };

  const install = async () => {
    setInstalling(true);
    setInstallMsg(null);
    try {
      const body = await api.registryGitInstall();
      if (body.ok) {
        setInstallMsg({ ok: true, text: t("registry.register.gitInstallOk", { version: body.git_version ?? "" }) });
        setInstallTick((n) => n + 1);
      } else {
        setInstallMsg({ ok: false, text: t("registry.register.gitInstallFailed", { msg: body.error ?? body.hint ?? "—" }) });
      }
    } catch (err) {
      setInstallMsg({ ok: false, text: t("registry.register.gitInstallFailed", { msg: errorMessage(err) }) });
    } finally {
      setInstalling(false);
    }
  };

  const git = caps.data;
  const noGit = git != null && git.available === false;
  const fallbackHosts = git?.fallback_hosts ?? [];

  const columns: Column<GitSkill>[] = [
    {
      key: "check",
      title: (
        <input
          type="checkbox"
          className="v2-registry-checkbox"
          checked={allChecked}
          disabled={selectable.length === 0}
          aria-label={t("registry.register.gitSelectAll")}
          title={t("registry.register.gitSelectAll")}
          onChange={(e) => {
            const on = e.target.checked;
            setChecked((c) => {
              const nextChecked = { ...c };
              selectable.forEach((s) => {
                nextChecked[s.index] = on;
              });
              return nextChecked;
            });
          }}
          data-testid="v2-registry-git-all"
        />
      ),
      width: 44,
      render: (s) => {
        const done = results[s.index]?.ok === true;
        return (
          <input
            type="checkbox"
            className="v2-registry-checkbox"
            checked={Boolean(checked[s.index]) || done}
            disabled={!s.valid || done}
            onChange={(e) => setChecked((c) => ({ ...c, [s.index]: e.target.checked }))}
            aria-label={s.name}
            data-testid={`v2-registry-git-check-${s.index}`}
          />
        );
      },
    },
    {
      key: "name",
      title: t("v2.registry.field.name"),
      width: 260,
      render: (s) => {
        const nm = names[s.index] ?? s.name;
        const done = results[s.index]?.ok === true;
        const invalid = checked[s.index] && !done && !REGISTRY_NAME_RE.test(nm);
        return (
          <>
            <input
              className="v2-input mono"
              value={nm}
              disabled={done || !s.valid}
              onChange={(e) => setNames((n) => ({ ...n, [s.index]: e.target.value }))}
              data-testid={`v2-registry-git-name-${s.index}`}
            />
            {invalid && <span className="sub" style={{ color: "var(--v2-danger)" }}>{t("registry.register.gitNameInvalid")}</span>}
          </>
        );
      },
    },
    { key: "desc", title: t("v2.registry.field.description"), render: (s) => <span className="clip">{s.description || "—"}</span> },
    {
      key: "meta",
      title: t("v2.registry.field.version"),
      render: (s) => (
        <>
          <span className="mono">{s.version || "—"}</span>
          <span className="sub">{t("v2.registry.fileCount", { n: s.files.length })}</span>
        </>
      ),
    },
    {
      key: "state",
      title: t("v2.registry.col.status"),
      className: "right",
      render: (s) => {
        const res = results[s.index];
        if (res?.ok) return <Tag tone="green">{t("v2.registry.imported")}</Tag>;
        if (res && !res.ok)
          return (
            <Tag tone="red" title={res.error}>
              {t("v2.registry.importFailedShort")}
            </Tag>
          );
        return s.valid ? (
          <Tag tone="blue">{t("v2.registry.valid")}</Tag>
        ) : (
          <Tag tone="red" title={s.errors.join("; ")}>
            {t("v2.registry.invalid")}
          </Tag>
        );
      },
    },
  ];

  const rowErrors = (skills ?? []).flatMap((s) => {
    const res = results[s.index];
    if (res && !res.ok) return [`${names[s.index] ?? s.name}: ${res.error ?? t("registry.register.importFailed", { msg: "unknown" })}`];
    if (!s.valid && s.errors.length) return [`${s.name}: ${s.errors.join("; ")}`];
    return [];
  });

  return (
    <>
      <Card title={t("v2.registry.gitTitle")} sub={t("v2.registry.gitSub")} testId="v2-registry-git">
        {noGit && (
          <Alert
            tone="warn"
            action={
              git?.install?.auto_installable ? (
                <Button size="sm" disabled={installing} onClick={() => void install()} testId="v2-registry-git-install">
                  {installing ? t("registry.register.gitInstalling") : t("registry.register.gitAutoInstall")}
                </Button>
              ) : undefined
            }
          >
            <div>
              {t("registry.register.gitNoGit")}
              {fallbackHosts.length > 0 && <> {t("registry.register.gitFallbackHint", { hosts: fallbackHosts.join(", ") })}</>}
            </div>
            {!git?.install?.auto_installable && git?.install?.hint && (
              <div style={{ marginTop: 6 }}>
                {t("registry.register.gitInstallHint")}
                <code
                  className="v2-registry-hint"
                  onClick={() => void navigator.clipboard?.writeText(git.install?.hint ?? "").catch(() => undefined)}
                >
                  {git.install.hint}
                </code>
              </div>
            )}
            {installMsg && (
              <div style={{ marginTop: 6, color: installMsg.ok ? "var(--v2-success)" : "var(--v2-danger)" }}>{installMsg.text}</div>
            )}
          </Alert>
        )}
        {scanError && <Alert tone="error">{scanError}</Alert>}
        <div className="v2-form cols-2">
          <Field label={t("v2.registry.field.gitUrl")} required full>
            <input
              className="v2-input mono"
              value={url}
              onChange={(e) => setUrl(e.target.value)}
              placeholder="https://github.com/anthropics/skills"
              data-testid="v2-registry-git-url"
            />
          </Field>
          <Field label={t("v2.registry.field.gitRef")}>
            <input className="v2-input mono" value={ref} onChange={(e) => setRef(e.target.value)} placeholder="main" />
          </Field>
          <Field label={t("v2.registry.field.gitSubdir")}>
            <input className="v2-input mono" value={subdir} onChange={(e) => setSubdir(e.target.value)} placeholder="skills/" />
          </Field>
          <Field label={t("v2.registry.field.gitToken")} hint={t("v2.registry.field.gitTokenHint")}>
            <input
              className="v2-input mono"
              type="password"
              autoComplete="off"
              value={token}
              onChange={(e) => setToken(e.target.value)}
              placeholder="••••••••"
            />
          </Field>
          <Field label=" ">
            <div>
              <Button kind="primary" disabled={scanning || !url.trim()} onClick={() => void scan()} testId="v2-registry-git-scan">
                {scanning ? t("registry.register.gitScanning") : t("v2.registry.gitScan")}
              </Button>
            </div>
          </Field>
        </div>
      </Card>
      {skills && (
        <Card
          title={t("v2.registry.gitFound", { n: skills.length })}
          sub={t("v2.registry.gitFoundSub")}
          testId="v2-registry-git-list"
          end={
            <Button kind="primary" size="sm" disabled={!canImport} onClick={() => void runImport()} testId="v2-registry-git-import">
              {importing ? t("v2.registry.importing") : t("v2.registry.gitImport", { n: targets.length })}
            </Button>
          }
        >
          {rowErrors.length > 0 && (
            <Alert tone="error">
              <ul className="v2-list">
                {rowErrors.map((m) => (
                  <li key={m}>{m}</li>
                ))}
              </ul>
            </Alert>
          )}
          <Table columns={columns} rows={skills} rowKey={(s) => String(s.index)} density="dense" />
        </Card>
      )}
    </>
  );
}
