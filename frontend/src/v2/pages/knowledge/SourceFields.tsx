import { FileText, Upload, X } from "lucide-react";
import { type ChangeEvent, useRef } from "react";
import { useTranslation } from "react-i18next";

import { formatBytes, KB_COMING_SOON, mergeFiles } from "../../../lib/knowledgeBases";
import { Alert, Button, Field, OptionCard, Tag } from "../../ui";
import type { SourceDraft } from "./source";

/** Upload files to the artifacts bucket, or point at an existing S3 location;
 *  the other connectors are shown as "coming soon" (v1 is S3-only). */
export function SourceFields({
  value,
  onChange,
  error,
}: {
  value: SourceDraft;
  onChange: (next: SourceDraft) => void;
  /** a validation message for the active mode's required field */
  error?: string | null;
}) {
  const { t } = useTranslation();
  const fileRef = useRef<HTMLInputElement>(null);
  const set = (patch: Partial<SourceDraft>) => onChange({ ...value, ...patch });

  const handlePick = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? []);
    if (fileRef.current) fileRef.current.value = "";
    if (picked.length) set({ files: mergeFiles(value.files, picked) });
  };

  return (
    <div className="v2-form" data-testid="v2-kb-source">
      <Field label={t("knowledge.source.label")} required>
        <div className="v2-options">
          <OptionCard
            title={t("knowledge.source.upload")}
            desc={t("v2.knowledge.sourceUploadDesc")}
            on={value.mode === "upload"}
            onClick={() => set({ mode: "upload" })}
            testId="v2-kb-mode-upload"
          />
          <OptionCard
            title={t("knowledge.source.existing")}
            desc={t("v2.knowledge.sourceExistingDesc")}
            on={value.mode === "existing"}
            onClick={() => set({ mode: "existing" })}
            testId="v2-kb-mode-existing"
          />
        </div>
        <div className="v2-knowledge-soon">
          <span>{t("v2.knowledge.comingSoonLabel")}</span>
          {KB_COMING_SOON.map((key) => (
            <Tag key={key} tone="gray">
              {t(`knowledge.source.connector.${key}`)}
            </Tag>
          ))}
        </div>
      </Field>

      {value.mode === "upload" ? (
        <Field
          label={t("knowledge.source.uploadLabel")}
          required
          error={error}
          hint={t("knowledge.source.uploadHint")}
        >
          <div>
            <Button onClick={() => fileRef.current?.click()} testId="v2-kb-pick">
              <Upload size={14} aria-hidden="true" />
              {t("knowledge.source.uploadPick")}
            </Button>
            <input
              ref={fileRef}
              type="file"
              multiple
              hidden
              data-testid="v2-kb-file-input"
              onChange={handlePick}
            />
          </div>
          {value.files.length > 0 && (
            <ul className="v2-knowledge-files" data-testid="v2-kb-files">
              {value.files.map((f, i) => (
                <li key={`${f.name}:${f.size}`}>
                  <FileText size={14} aria-hidden="true" />
                  <span className="n">{f.name}</span>
                  <span className="s">{formatBytes(f.size)}</span>
                  <button
                    type="button"
                    onClick={() => set({ files: value.files.filter((_, j) => j !== i) })}
                    aria-label={t("knowledge.source.removeFile")}
                    title={t("knowledge.source.removeFile")}
                  >
                    <X size={14} />
                  </button>
                </li>
              ))}
            </ul>
          )}
        </Field>
      ) : (
        <>
          <div className="v2-form cols-2">
            <Field label={t("knowledge.source.bucket")} required error={error}>
              <input
                className="v2-input mono"
                value={value.bucket}
                onChange={(e) => set({ bucket: e.target.value })}
                placeholder="my-corpus-bucket"
                data-testid="v2-kb-bucket"
              />
            </Field>
            <Field label={t("knowledge.source.prefix")}>
              <input
                className="v2-input mono"
                value={value.prefix}
                onChange={(e) => set({ prefix: e.target.value })}
                placeholder="docs/"
                data-testid="v2-kb-prefix"
              />
            </Field>
          </div>
          <Alert>{t("knowledge.source.existingHint")}</Alert>
        </>
      )}
    </div>
  );
}
