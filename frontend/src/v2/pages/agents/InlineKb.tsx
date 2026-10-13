import { ExternalLink, Upload, X } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link } from "react-router-dom";

import { resolveKb, selectableKbs, toggle } from "../../../lib/agent-spec";
import { formatBytes, KB_NAME_RE, mergeFiles } from "../../../lib/knowledgeBases";
import { HintLabel } from "../../Glossary";
import { Alert, Button, Field, Spin, Tag } from "../../ui";
import { displayPhase, type InlineKb, PHASE_TONE, phaseLabelKey } from "./inlineKbState";
import type { InlineKbApi } from "./useInlineKb";
import { CheckList, type SectionProps } from "./wizardKit";

/** Status tag of an inline KB: `Ready`, `Ingesting`, … */
export function InlineKbState({ inline }: { inline: InlineKb }) {
  const { t } = useTranslation();
  return (
    <Tag tone={PHASE_TONE[displayPhase(inline)]} dot>
      {t(phaseLabelKey(inline))}
    </Tag>
  );
}

/** "The KB stays in the workspace list" + a link to it (new tab: the wizard keeps
 *  its draft). Shown before a KB is created and while one is mounted. */
function KeptNote() {
  const { t } = useTranslation();
  return (
    <span className="v2-muted" data-testid="v2-agent-inline-kb-kept">
      {t("v2.agents.wizard.inlineKb.keptNote")}{" "}
      <Link to="/v2/knowledge-bases" target="_blank" rel="noopener noreferrer">
        {t("v2.agents.wizard.inlineKb.keptLink")}
        <ExternalLink size={12} aria-hidden="true" />
      </Link>
    </span>
  );
}

/**
 * The wizard's knowledge field: pick existing ACTIVE KBs (as before) or create one
 * inline from a name + files. Name follows the agent's name until edited.
 */
export function KnowledgeField({
  form,
  set,
  cat,
  kb,
  note,
}: Pick<SectionProps, "form" | "set" | "cat"> & { kb: InlineKbApi; note: string }) {
  const { t } = useTranslation();
  const fileRef = useRef<HTMLInputElement>(null);
  const [nameDraft, setNameDraft] = useState<string | null>(null);
  const [files, setFiles] = useState<File[]>([]);
  const [tried, setTried] = useState(false);
  const { inline } = kb;

  const kbs = selectableKbs(cat.kbCatalog);
  const extraKbs = form.selectedKbs
    .filter((id) => id !== inline?.kb_id && !kbs.some((k) => k.kb_id === id))
    .map((id) => {
      const info = resolveKb(id, cat.kbCatalog, []);
      return { key: id, label: info.name, hint: info.description || info.name };
    });
  const pickable = kbs.filter((k) => k.kb_id !== inline?.kb_id);

  const derived = form.name ? `${form.name}-kb`.slice(0, 100) : "";
  const name = (nameDraft ?? derived).trim();
  const nameOk = KB_NAME_RE.test(name);
  const filesOk = files.length > 0;

  const submit = () => {
    setTried(true);
    if (!nameOk || !filesOk) return;
    void kb.create(name, files);
  };

  return (
    <Field label={<HintLabel term="knowledgeBase">{t("v2.agents.kbTitle")}</HintLabel>} hint={note}>
      <CheckList
        items={pickable}
        keyOf={(k) => k.kb_id}
        labelOf={(k) => k.name}
        hintOf={(k) => k.description || k.name}
        selected={form.selectedKbs}
        onToggle={(id) => set({ selectedKbs: toggle(form.selectedKbs, id) })}
        empty={t("v2.agents.wizard.noKbs")}
        extra={extraKbs}
        testId="v2-agent-kbs"
      />
      {inline ? (
        <div className="v2-agents-block" data-testid="v2-agent-inline-kb">
          <div className="v2-row">
            <span>{t("v2.agents.wizard.inlineKb.mounted", { name: inline.name })}</span>
            <InlineKbState inline={inline} />
            {kb.busy && <Spin />}
            <Button size="sm" onClick={kb.discard} testId="v2-agent-inline-kb-discard">
              {t("v2.agents.wizard.inlineKb.discard")}
            </Button>
          </div>
          {inline.uploadError && (
            <Alert tone="error">
              {t("v2.agents.wizard.inlineKb.uploadFailed", { name: inline.name, msg: inline.uploadError })}
              <div className="v2-row">
                <Button size="sm" disabled={kb.busy} onClick={() => void kb.retryUpload()} testId="v2-agent-inline-kb-retry">
                  {t("v2.agents.wizard.inlineKb.retryUpload")}
                </Button>
              </div>
            </Alert>
          )}
          {inline.uploaded && <Alert>{t("v2.agents.wizard.inlineKb.note")}</Alert>}
          <KeptNote />
        </div>
      ) : (
        <div className="v2-agents-block" data-testid="v2-agent-inline-kb-form">
          <h3 className="v2-sub-title">{t("v2.agents.wizard.inlineKb.pickTitle")}</h3>
          <Field
            label={t("v2.agents.wizard.inlineKb.name")}
            hint={t("v2.agents.wizard.inlineKb.nameHint")}
            error={tried && !nameOk ? t("v2.agents.wizard.inlineKb.nameInvalid") : undefined}
          >
            <input
              className="v2-input mono"
              value={nameDraft ?? derived}
              maxLength={100}
              disabled={kb.busy}
              onChange={(e) => setNameDraft(e.target.value)}
              data-testid="v2-agent-inline-kb-name"
            />
          </Field>
          <Field
            label={t("v2.agents.wizard.inlineKb.files")}
            error={tried && !filesOk ? t("v2.agents.wizard.inlineKb.filesRequired") : undefined}
          >
            <div className="v2-row">
              <Button size="sm" disabled={kb.busy} onClick={() => fileRef.current?.click()} testId="v2-agent-inline-kb-pick">
                <Upload size={13} aria-hidden="true" />
                {t("v2.agents.wizard.inlineKb.pickFiles")}
              </Button>
              <input
                ref={fileRef}
                type="file"
                multiple
                hidden
                onChange={(e) => {
                  const picked = Array.from(e.target.files ?? []);
                  e.target.value = "";
                  if (picked.length) setFiles((prev) => mergeFiles(prev, picked));
                }}
                data-testid="v2-agent-inline-kb-file-input"
              />
            </div>
            {files.length > 0 && (
              <ul className="v2-agents-files">
                {files.map((f) => (
                  <li key={`${f.name}:${f.size}`}>
                    <span className="mono">{f.name}</span>
                    <span className="v2-muted"> · {formatBytes(f.size)}</span>
                    <button
                      type="button"
                      className="v2-btn sm"
                      disabled={kb.busy}
                      aria-label={t("v2.agents.wizard.inlineKb.removeFile", { name: f.name })}
                      onClick={() => setFiles((prev) => prev.filter((x) => x !== f))}
                    >
                      <X size={12} aria-hidden="true" />
                    </button>
                  </li>
                ))}
              </ul>
            )}
          </Field>
          {kb.error && (
            <Alert tone="error">
              <span data-testid="v2-agent-inline-kb-error">
                {t("v2.agents.wizard.inlineKb.createFailed", { msg: kb.error })}
              </span>
            </Alert>
          )}
          <KeptNote />
          <Button kind="primary" disabled={kb.busy} onClick={submit} testId="v2-agent-inline-kb-create">
            {kb.busy ? t("v2.agents.wizard.inlineKb.creating") : t("v2.agents.wizard.inlineKb.create")}
          </Button>
        </div>
      )}
    </Field>
  );
}
