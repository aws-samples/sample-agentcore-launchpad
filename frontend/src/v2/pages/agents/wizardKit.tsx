import { Upload } from "lucide-react";
import { type Dispatch, type ReactNode, type SetStateAction, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  type AgentForm,
  type AttachableKb,
  type AttachableMcp,
  type AttachableSkill,
  resolveKb,
  selectableKbs,
  skillNameFromPath,
  toggle,
} from "../../../lib/agent-spec";
import { api, type ByocUploadInfo, errorMessage, type InspectedSkill, type MemoryResourceRow } from "../../../lib/api";
import {
  CUSTOM_MODEL_OPTION,
  type ModelSource,
  modelOptionsFor,
  REASONING_EFFORTS,
  supportsReasoningEffort,
} from "../../../lib/models";
import { EFFORT_NONE, type EffortChoice } from "../../../pages/create/presetSettings";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, Segmented, Spin } from "../../ui";

/** The live catalogs the configure step offers (a failed read leaves one empty). */
export interface WizardCatalogs {
  loading: boolean;
  gatewayTargets: AttachableMcp[];
  remoteMcp: AttachableMcp[];
  skills: AttachableSkill[];
  kbCatalog: AttachableKb[];
  memories: MemoryResourceRow[];
}

/** Wizard state that is not part of the spec but must survive a step change. */
export interface WizardUi {
  customModel: boolean;
  /** skill sources attached without a registry record (name shown on the row) */
  customSkills: { name: string; path: string }[];
  byocUpload: ByocUploadInfo | null;
  byocUploading: boolean;
}

export interface SectionProps {
  form: AgentForm;
  /** merge a patch (or a patch computed from the latest form) into the draft */
  set: (patch: Partial<AgentForm> | ((prev: AgentForm) => Partial<AgentForm>)) => void;
  cat: WizardCatalogs;
  /** a validation message for `key`, shown once the member tried to continue */
  err: (key: string) => string | undefined;
  /** re-publish: the name is immutable on the backend */
  nameLocked?: boolean;
}

/** Checkbox list of a catalog; an empty catalog shows `empty`. `extra` are selected
 *  keys the catalog no longer carries (kept checked so a prefill/stored pick stays
 *  visible and removable). */
export function CheckList<T>({
  items,
  keyOf,
  labelOf,
  hintOf,
  disabledOf,
  selected,
  onToggle,
  empty,
  extra,
  testId,
}: {
  items: T[];
  keyOf: (item: T) => string;
  labelOf: (item: T) => ReactNode;
  hintOf?: (item: T) => string | undefined | null;
  disabledOf?: (item: T) => boolean;
  selected: string[];
  onToggle: (key: string) => void;
  empty: string;
  extra?: { key: string; label: ReactNode; hint?: string }[];
  testId?: string;
}) {
  if (items.length === 0 && !extra?.length) return <span className="v2-muted">{empty}</span>;
  return (
    <div className="v2-checks" data-testid={testId}>
      {items.map((item) => {
        const key = keyOf(item);
        const disabled = disabledOf?.(item) ?? false;
        return (
          <label key={key} className={disabled ? "v2-check disabled" : "v2-check"} title={hintOf?.(item) ?? undefined}>
            <input type="checkbox" checked={selected.includes(key)} disabled={disabled} onChange={() => onToggle(key)} />
            {labelOf(item)}
          </label>
        );
      })}
      {extra?.map((x) => (
        <label key={x.key} className="v2-check" title={x.hint}>
          <input type="checkbox" checked onChange={() => onToggle(x.key)} />
          {x.label}
        </label>
      ))}
    </div>
  );
}

/** Model source + model pick (+ the harness-only inference knobs). */
export function ModelCard({
  form,
  set,
  err,
  custom,
  setCustom,
  applySource,
  showSource,
  sourceNote,
  claudeOnly = false,
  knobs = false,
}: Omit<SectionProps, "cat"> & {
  custom: boolean;
  setCustom: (on: boolean) => void;
  applySource: (source: ModelSource) => void;
  showSource: boolean;
  /** why the source is pinned when the control is hidden */
  sourceNote?: string;
  claudeOnly?: boolean;
  knobs?: boolean;
}) {
  const { t } = useTranslation();
  const options = modelOptionsFor(form.modelSource, claudeOnly);
  const effortAllowed = supportsReasoningEffort(form.modelId.trim(), form.modelSource);
  return (
    <Card title={t("v2.agents.model")}>
      <div className="v2-form cols-2">
        {showSource ? (
          <Field
            label={t("v2.agents.wizard.modelSource")}
            full
            hint={t(form.modelSource === "mantle" ? "create.configure.modelSourceMantleDesc" : "create.configure.modelSourceBedrockDesc")}
          >
            <div className="v2-row">
<Segmented
              value={form.modelSource}
              options={(["bedrock", "mantle"] as ModelSource[]).map((s) => ({ value: s, label: t(`v2.agents.wizard.source.${s}`) }))}
              // a benign re-click keeps a custom id; a real switch re-seeds
              onChange={(s) => s !== form.modelSource && applySource(s)}
            />
</div>
          </Field>
        ) : (
          sourceNote && (
            <Field label={t("v2.agents.wizard.modelSource")} full hint={sourceNote}>
              <span>{t(`v2.agents.wizard.source.${form.modelSource}`)}</span>
            </Field>
          )
        )}
        <Field label={t("v2.agents.model")} required error={err("model")}>
          <select
            className="v2-select"
            value={custom ? CUSTOM_MODEL_OPTION : form.modelId}
            onChange={(e) => {
              if (e.target.value === CUSTOM_MODEL_OPTION) setCustom(true);
              else {
                setCustom(false);
                set({ modelId: e.target.value });
              }
            }}
            data-testid="v2-agent-model"
          >
            {options.map((o) => (
              <option key={o.model_id} value={o.model_id}>
                {o.label}
              </option>
            ))}
            <option value={CUSTOM_MODEL_OPTION}>{t("v2.agents.wizard.customModel")}</option>
          </select>
        </Field>
        {custom ? (
          <Field label={t("v2.agents.wizard.customModelId")} required>
            <input
              className="v2-input mono"
              value={form.modelId}
              onChange={(e) => set({ modelId: e.target.value })}
              placeholder={t("create.configure.modelCustomPlaceholder")}
              data-testid="v2-agent-model-custom"
            />
          </Field>
        ) : (
          <Field label={t("v2.agents.wizard.modelId")}>
            <input className="v2-input mono" value={form.modelId} readOnly />
          </Field>
        )}
        {knobs && (
          <>
            <Field label={t("v2.agents.wizard.maxTokens")} hint={t("v2.agents.wizard.maxTokensHint")} error={err("maxTokens")}>
              <input
                className="v2-input"
                inputMode="numeric"
                value={form.maxTokens}
                onChange={(e) => set({ maxTokens: e.target.value })}
                data-testid="v2-agent-max-tokens"
              />
            </Field>
            <Field
              label={t("v2.agents.wizard.effort")}
              hint={effortAllowed ? t("create.system.settings.effortHint") : t("v2.agents.wizard.effortUnsupported")}
            >
              <select
                className="v2-select"
                value={effortAllowed ? form.reasoningEffort : EFFORT_NONE}
                disabled={!effortAllowed}
                onChange={(e) => set({ reasoningEffort: e.target.value as EffortChoice })}
              >
                <option value={EFFORT_NONE}>{t("create.system.settings.effortNone")}</option>
                {REASONING_EFFORTS.map((e) => (
                  <option key={e} value={e}>
                    {t(`create.system.settings.effortLevels.${e}`)}
                  </option>
                ))}
              </select>
            </Field>
          </>
        )}
      </div>
    </Card>
  );
}

/** Skills (registry catalog + custom zip / git sources) and managed KBs. */
export function SkillsKbCard({
  form,
  set,
  cat,
  customSkills,
  setCustomSkills,
  kbNote,
}: SectionProps & {
  customSkills: WizardUi["customSkills"];
  setCustomSkills: Dispatch<SetStateAction<WizardUi["customSkills"]>>;
  kbNote: string;
}) {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const fileRef = useRef<HTMLInputElement>(null);
  const [gitOpen, setGitOpen] = useState(false);
  const [gitUrl, setGitUrl] = useState("");
  const [busy, setBusy] = useState(false);
  const [pending, setPending] = useState<{ stagingId: string; skills: InspectedSkill[]; picked: number[] } | null>(null);

  const kbs = selectableKbs(cat.kbCatalog);
  const extraSkills = form.skills
    .filter((path) => !cat.skills.some((s) => s.path === path))
    .map((path) => {
      const custom = customSkills.find((c) => c.path === path);
      return {
        key: path,
        hint: path,
        label: (
          <>
            {custom ? custom.name : skillNameFromPath(path)}
            <span className="v2-muted"> · {t(custom ? "v2.agents.wizard.skillCustom" : "v2.agents.wizard.skillRegistry")}</span>
          </>
        ),
      };
    });
  const extraKbs = form.selectedKbs
    .filter((id) => !kbs.some((k) => k.kb_id === id))
    .map((id) => {
      const info = resolveKb(id, cat.kbCatalog, []);
      return { key: id, label: info.name, hint: info.description || info.name };
    });

  const toggleSkill = (path: string) => {
    if (form.skills.includes(path)) {
      set((prev) => ({ skills: prev.skills.filter((s) => s !== path) }));
      setCustomSkills((prev) => prev.filter((c) => c.path !== path));
    } else set((prev) => ({ skills: [...prev.skills, path] }));
  };

  // attach staged skills; returns whether every selection attached
  const attachStaged = async (stagingId: string, indices: number[]) => {
    const res = await api.attachSkillSources(stagingId, indices.map((index) => ({ index })));
    const attached = res.skills.filter((s) => s.ok && s.path);
    const failed = res.skills.filter((s) => !s.ok);
    if (attached.length) {
      set((prev) => ({ skills: [...prev.skills, ...attached.map((s) => s.path as string)] }));
      setCustomSkills((prev) => [...prev, ...attached.map((s) => ({ name: s.name, path: s.path as string }))]);
    }
    for (const item of failed) toast("error", `${item.name}: ${item.error ?? "attach failed"}`);
    return failed.length === 0;
  };

  const inspect = async (input: File | { url: string }) => {
    setBusy(true);
    try {
      const res = input instanceof File ? await api.inspectSkillZip(input) : await api.inspectSkillGit(input.url);
      const valid = res.skills.filter((s) => s.valid);
      if (valid.length === 1 && res.skills.length === 1) {
        // single-skill source (typical zip) — attach straight away
        if (await attachStaged(res.staging_id, [valid[0].index])) {
          setGitOpen(false);
          setGitUrl("");
        }
      } else {
        // monorepo — let the member pick which skills to attach
        setPending({ stagingId: res.staging_id, skills: res.skills, picked: [] });
      }
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const attachPicked = async () => {
    if (!pending || pending.picked.length === 0) return;
    setBusy(true);
    try {
      if (await attachStaged(pending.stagingId, pending.picked)) {
        setPending(null);
        setGitOpen(false);
        setGitUrl("");
      }
    } catch (err) {
      toast("error", errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  const gitValid = gitUrl.trim().startsWith("https://");
  return (
    <Card title={t("v2.agents.wizard.skillsKb")}>
      <div className="v2-form">
        <Field label={t("v2.agents.wizard.skills")} hint={t("v2.agents.wizard.skillsHintSources")}>
          {cat.loading ? (
            <Spin />
          ) : (
            <CheckList
              items={cat.skills}
              keyOf={(s) => s.path}
              labelOf={(s) => s.name}
              hintOf={(s) => s.description || s.path}
              selected={form.skills}
              onToggle={toggleSkill}
              empty={t("v2.agents.wizard.noSkills")}
              extra={extraSkills}
              testId="v2-agent-skills"
            />
          )}
          <div className="v2-row">
            <Button size="sm" disabled={busy} onClick={() => fileRef.current?.click()} testId="v2-agent-skill-zip">
              <Upload size={13} aria-hidden="true" />
              {t("v2.agents.wizard.skillUpload")}
            </Button>
            <Button size="sm" kind={gitOpen ? "soft" : undefined} disabled={busy} onClick={() => setGitOpen((v) => !v)} testId="v2-agent-skill-git">
              {t("v2.agents.wizard.skillGit")}
            </Button>
            {busy && <span className="v2-muted">{t("v2.agents.wizard.skillInspecting")}</span>}
            <input
              ref={fileRef}
              type="file"
              accept=".zip"
              hidden
              onChange={(e) => {
                const file = e.target.files?.[0];
                e.target.value = "";
                if (file) void inspect(file);
              }}
              data-testid="v2-agent-skill-zip-input"
            />
          </div>
          {gitOpen && (
            <div className="v2-row v2-agents-nowrap">
              <input
                className="v2-input mono"
                value={gitUrl}
                onChange={(e) => setGitUrl(e.target.value)}
                placeholder="https://github.com/org/repo[/subdir][@ref]"
                data-testid="v2-agent-skill-git-url"
              />
              <Button disabled={busy || !gitValid} onClick={() => void inspect({ url: gitUrl.trim() })} testId="v2-agent-skill-git-fetch">
                {t("create.configure.skillsGitFetch")}
              </Button>
            </div>
          )}
          {pending && (
            <div className="v2-agents-pending" data-testid="v2-agent-skill-pending">
              <span className="v2-muted">{t("v2.agents.wizard.skillPending")}</span>
              <div className="v2-checks">
                {pending.skills.map((s) => (
                  <label key={s.index} className={s.valid ? "v2-check" : "v2-check disabled"} title={s.valid ? s.description : s.errors.join("; ")}>
                    <input
                      type="checkbox"
                      disabled={!s.valid}
                      checked={pending.picked.includes(s.index)}
                      onChange={() => setPending({ ...pending, picked: toggle(pending.picked, s.index) })}
                    />
                    {s.name}
                  </label>
                ))}
              </div>
              <div className="v2-row">
                <Button size="sm" kind="primary" disabled={busy || pending.picked.length === 0} onClick={() => void attachPicked()}>
                  {t("create.configure.skillsAttach", { n: pending.picked.length })}
                </Button>
                <Button size="sm" onClick={() => setPending(null)}>
                  {t("v2.common.cancel")}
                </Button>
              </div>
            </div>
          )}
        </Field>
        <Field label={t("v2.agents.kbTitle")} hint={kbNote}>
          <CheckList
            items={kbs}
            keyOf={(kb) => kb.kb_id}
            labelOf={(kb) => kb.name}
            hintOf={(kb) => kb.description || kb.name}
            selected={form.selectedKbs}
            onToggle={(id) => set({ selectedKbs: toggle(form.selectedKbs, id) })}
            empty={t("v2.agents.wizard.noKbs")}
            extra={extraKbs}
            testId="v2-agent-kbs"
          />
        </Field>
      </div>
    </Card>
  );
}

/** Long-term memory + the per-agent memory pin (+ the harness loop bounds). */
export function MemoryCard({ form, set, cat, err, loop, note }: SectionProps & { loop: boolean; note?: string }) {
  const { t } = useTranslation();
  const options = cat.memories.filter((m) => m.id && !m.is_default);
  return (
    <Card title={t(loop ? "v2.agents.wizard.memory" : "v2.agents.wizard.memoryOnly")}>
      <div className="v2-form cols-2">
        <Field label={t("v2.agents.wizard.longTerm")} hint={t("v2.agents.wizard.longTermHint")}>
          <label className="v2-check">
            <input type="checkbox" checked={form.longTerm} onChange={(e) => set({ longTerm: e.target.checked })} data-testid="v2-agent-long-term" />
            {t("v2.agents.wizard.longTermOn")}
          </label>
        </Field>
        <Field label={t("v2.agents.wizard.memoryResource")} hint={t("create.configure.memoryResourceHint")}>
          <select className="v2-select" value={form.memoryId} onChange={(e) => set({ memoryId: e.target.value })} data-testid="v2-agent-memory">
            <option value="">{t("v2.agents.wizard.memoryDefault")}</option>
            {options.map((m) => (
              <option key={m.id ?? ""} value={m.id ?? ""} disabled={m.status !== "ACTIVE"}>
                {m.name ?? m.id}
                {m.status !== "ACTIVE" ? ` (${m.status ?? "?"})` : ""}
              </option>
            ))}
            {/* a pin the list no longer carries stays selectable */}
            {form.memoryId && !cat.memories.some((m) => m.id === form.memoryId) && (
              <option value={form.memoryId}>{form.memoryId}</option>
            )}
          </select>
        </Field>
        {loop && (
          <>
            <Field label={t("v2.agents.wizard.maxIterations")} hint={t("v2.agents.wizard.maxIterationsHint")} error={err("maxIterations")}>
              <input
                className="v2-input"
                inputMode="numeric"
                value={form.maxIterations}
                onChange={(e) => set({ maxIterations: e.target.value })}
                data-testid="v2-agent-max-iterations"
              />
            </Field>
            <Field label={t("v2.agents.wizard.timeout")} hint={t("v2.agents.wizard.timeoutHint")} error={err("timeout")}>
              <input
                className="v2-input"
                inputMode="numeric"
                value={form.timeoutSeconds}
                onChange={(e) => set({ timeoutSeconds: e.target.value })}
                data-testid="v2-agent-timeout"
              />
            </Field>
          </>
        )}
      </div>
      {note && (
        <div className="v2-agents-foot">
          <Alert>{note}</Alert>
        </div>
      )}
    </Card>
  );
}
