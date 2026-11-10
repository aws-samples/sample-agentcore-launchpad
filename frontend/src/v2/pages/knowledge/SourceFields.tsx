import { FileText, Upload, X } from "lucide-react";
import { type ChangeEvent, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import {
  formatBytes,
  KB_FILE_ACCEPT,
  KB_MAX_DOC_BYTES,
  KB_SUPPORTED_EXT,
  mergeFiles,
  unsupportedFileNames,
} from "../../../lib/knowledgeBases";
import { Alert, Button, Field, OptionCard, Tag } from "../../ui";
import type { SourceDraft } from "./source";

/** Upload files to the artifacts bucket, or point at an existing S3 location. The form
 *  states the document types the ingestion Lambda indexes and its limits (user decision
 *  2026-10-21, N17-obs240 — this replaced the "coming soon" connector list), and the
 *  picker refuses an unsupported extension before anything is uploaded (N17-obs239). */
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
  // the pick-time refusal (N17-obs239); cleared by the next accepted pick or a mode change
  const [pickError, setPickError] = useState<string | null>(null);
  const set = (patch: Partial<SourceDraft>) => onChange({ ...value, ...patch });
  const setMode = (mode: SourceDraft["mode"]) => {
    setPickError(null);
    set({ mode });
  };
  const supportedList = KB_SUPPORTED_EXT.join(" ");

  const handlePick = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? []);
    if (fileRef.current) fileRef.current.value = "";
    if (!picked.length) return;
    const rejected = unsupportedFileNames(picked);
    if (rejected.length) {
      // the whole pick is refused — nothing reaches the draft, nothing is uploaded
      setPickError(
        t("knowledge.source.unsupportedPicked", { names: rejected.join(", "), supported: supportedList }),
      );
      return;
    }
    setPickError(null);
    set({ files: mergeFiles(value.files, picked) });
  };

  // a warning only: the 2 MB limit is on the extracted text, so a larger PDF may still index
  const large = value.files.filter((f) => f.size > KB_MAX_DOC_BYTES).map((f) => f.name);

  return (
    <div className="v2-form" data-testid="v2-kb-source">
      <Field label={t("knowledge.source.label")} required>
        <div className="v2-options">
          <OptionCard
            title={t("knowledge.source.upload")}
            desc={t("v2.knowledge.sourceUploadDesc")}
            on={value.mode === "upload"}
            onClick={() => setMode("upload")}
            testId="v2-kb-mode-upload"
          />
          <OptionCard
            title={t("knowledge.source.existing")}
            desc={t("v2.knowledge.sourceExistingDesc")}
            on={value.mode === "existing"}
            onClick={() => setMode("existing")}
            testId="v2-kb-mode-existing"
          />
        </div>
        <div className="v2-knowledge-supported" data-testid="v2-kb-supported">
          <span>{t("knowledge.source.supportedLabel")}</span>
          {KB_SUPPORTED_EXT.map((ext) => (
            <Tag key={ext} tone="gray">
              {ext}
            </Tag>
          ))}
        </div>
        <span className="hint" data-testid="v2-kb-supported-hint">
          {t("knowledge.source.supportedHint")}
        </span>
      </Field>

      {value.mode === "upload" ? (
        <Field
          label={t("knowledge.source.uploadLabel")}
          required
          error={error ?? pickError}
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
              accept={KB_FILE_ACCEPT}
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
          {large.length > 0 && (
            <span className="hint" data-testid="v2-kb-large-hint">
              {t("knowledge.source.largeFileHint", { names: large.join(", ") })}
            </span>
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
