import { type ChangeEvent, useRef } from "react";
import { useTranslation } from "react-i18next";

import { Btn } from "../../components";

export type SourceMode = "upload" | "existing";

interface SourcePickerProps {
  mode: SourceMode;
  onModeChange: (m: SourceMode) => void;
  files: File[];
  onFilesChange: (f: File[]) => void;
  bucket: string;
  onBucketChange: (v: string) => void;
  prefix: string;
  onPrefixChange: (v: string) => void;
}

// The five connectors that are UI placeholders for v1 (S3-only). Mirrors the
// "coming soon" treatment of the skill sources in SkillSourceForm.
const COMING_SOON = ["webCrawler", "sharepoint", "confluence", "googleDrive", "oneDrive"] as const;

export function SourcePicker({
  mode,
  onModeChange,
  files,
  onFilesChange,
  bucket,
  onBucketChange,
  prefix,
  onPrefixChange,
}: SourcePickerProps) {
  const { t } = useTranslation();
  const fileRef = useRef<HTMLInputElement>(null);

  const handlePick = (e: ChangeEvent<HTMLInputElement>) => {
    const picked = Array.from(e.target.files ?? []);
    if (fileRef.current) fileRef.current.value = "";
    if (picked.length === 0) return;
    // append across picks, de-duping by name+size so the same file isn't added twice
    const seen = new Set(files.map((f) => `${f.name}:${f.size}`));
    const merged = [...files];
    for (const f of picked) {
      const key = `${f.name}:${f.size}`;
      if (!seen.has(key)) {
        seen.add(key);
        merged.push(f);
      }
    }
    onFilesChange(merged);
  };

  const removeFile = (idx: number) => onFilesChange(files.filter((_, i) => i !== idx));

  return (
    <div data-testid="kb-source-picker">
      <div className="field">
        <label>{t("knowledge.source.label")}</label>
        <div className="selchips">
          <button
            type="button"
            className={`selchip${mode === "upload" ? " on" : ""}`}
            style={{ cursor: "pointer" }}
            data-testid="source-mode-upload"
            onClick={() => onModeChange("upload")}
          >
            {t("knowledge.source.upload")}
          </button>
          <button
            type="button"
            className={`selchip${mode === "existing" ? " on" : ""}`}
            style={{ cursor: "pointer" }}
            data-testid="source-mode-existing"
            onClick={() => onModeChange("existing")}
          >
            {t("knowledge.source.existing")}
          </button>
          {COMING_SOON.map((key) => (
            <button
              key={key}
              type="button"
              className="selchip"
              style={{ cursor: "not-allowed", opacity: 0.45 }}
              disabled
              title={t("knowledge.source.comingSoon")}
            >
              {t(`knowledge.source.connector.${key}`)}
              <span className="x">{t("knowledge.source.comingSoon")}</span>
            </button>
          ))}
        </div>
      </div>

      {mode === "upload" ? (
        <div className="field">
          <label>{t("knowledge.source.uploadLabel")}</label>
          {/* Not a `<label className="btn">`: this sits in a `.field`, and
              `.field label` (0,1,1) outranks `.btn` (0,1,0), which would force
              display:block plus the dim 9.5px field-caption type. */}
          <Btn onClick={() => fileRef.current?.click()}>
            {t("knowledge.source.uploadPick")}
          </Btn>
          <input
            ref={fileRef}
            type="file"
            multiple
            style={{ display: "none" }}
            data-testid="kb-file-input"
            onChange={handlePick}
          />
          {files.length > 0 && (
            <div className="code" style={{ maxHeight: 160, overflowY: "auto", marginTop: 9 }}>
              {files.map((f, i) => (
                <div
                  key={`${f.name}:${f.size}`}
                  style={{ display: "flex", justifyContent: "space-between", gap: 10 }}
                >
                  <span>{f.name}</span>
                  <button
                    type="button"
                    onClick={() => removeFile(i)}
                    style={{
                      background: "none",
                      border: 0,
                      color: "var(--crit)",
                      cursor: "pointer",
                      fontFamily: "inherit",
                    }}
                    title={t("knowledge.source.removeFile")}
                  >
                    ✕
                  </button>
                </div>
              ))}
            </div>
          )}
          <div className="note" style={{ marginTop: 10 }}>
            <span className="i">[i]</span>
            <span>{t("knowledge.source.uploadHint")}</span>
          </div>
        </div>
      ) : (
        <>
          <div className="field">
            <label htmlFor="kb-bucket">{t("knowledge.source.bucket")}</label>
            <input
              id="kb-bucket"
              className="input mono"
              value={bucket}
              onChange={(e) => onBucketChange(e.target.value)}
              placeholder="my-corpus-bucket"
              data-testid="kb-bucket-input"
            />
          </div>
          <div className="field">
            <label htmlFor="kb-prefix">{t("knowledge.source.prefix")}</label>
            <input
              id="kb-prefix"
              className="input mono"
              value={prefix}
              onChange={(e) => onPrefixChange(e.target.value)}
              placeholder="docs/"
              data-testid="kb-prefix-input"
            />
          </div>
          <div className="note">
            <span className="i">[i]</span>
            <span>{t("knowledge.source.existingHint")}</span>
          </div>
        </>
      )}
    </div>
  );
}
