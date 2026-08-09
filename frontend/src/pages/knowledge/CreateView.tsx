import type { CSSProperties } from "react";
import { useState } from "react";
import { useTranslation } from "react-i18next";

import { Btn, Panel, useToast, ViewHead } from "../../components";
import { type KBSourceBody, type KnowledgeBaseDetail } from "../KnowledgeBases";
import { kbErrorMessage } from "./kb-helpers";
import { SourcePicker, type SourceMode } from "./SourcePicker";

interface CreateViewProps {
  onBack: () => void;
  /** success tail: parent switches to the detail sub-page for the new KB */
  onCreated: (kbId: string) => void;
}

// Mirrors the backend rule — must start alphanumeric, then letters/digits/-/_
// (no spaces; AWS KB name constraint).
const NAME_RE = /^[a-zA-Z0-9][a-zA-Z0-9\-_]{0,99}$/;

export function CreateView({ onBack, onCreated }: CreateViewProps) {
  const { t } = useTranslation();
  const toast = useToast();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [mode, setMode] = useState<SourceMode>("upload");
  const [files, setFiles] = useState<File[]>([]);
  const [bucket, setBucket] = useState("");
  const [prefix, setPrefix] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const nameValid = NAME_RE.test(name.trim());
  const sourceValid =
    mode === "upload" ? files.length > 0 : bucket.trim().length > 0;
  const canSubmit = nameValid && sourceValid && !busy;

  const submit = async () => {
    setBusy(true);
    setError(null);
    try {
      const source: KBSourceBody =
        mode === "upload"
          ? { mode: "upload" }
          : { mode: "existing", bucket: bucket.trim(), prefix: prefix.trim() || undefined };
      const res = await fetch("/api/knowledge-bases", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name.trim(), description: description.trim(), source }),
      });
      const body = (await res.json().catch(() => ({}))) as KnowledgeBaseDetail & {
        message?: string;
        source_pending?: KBSourceBody;
      };
      if (!res.ok) {
        setError(kbErrorMessage(body, res.status));
        return;
      }
      const kbId = body.kb_id;
      // 202: the KB is returned while still CREATING — the backend finishes the
      // data source on its own thread, so there is nothing to replay from here
      // (the detail view polls and surfaces it if that ever fails).
      // upload mode: push the picked files into the KB's artifacts-bucket prefix
      // right away — uploads are allowed ahead of the data source, and the first
      // sync indexes them once it exists. A file failure is surfaced but the KB
      // is already created, so we still land on its detail page.
      if (mode === "upload" && files.length > 0) {
        const form = new FormData();
        for (const f of files) form.append("files", f);
        const up = await fetch(`/api/knowledge-bases/${kbId}/files`, {
          method: "POST",
          body: form,
        });
        if (!up.ok) {
          const upBody = (await up.json().catch(() => ({}))) as { message?: string };
          toast(t("knowledge.create.uploadFailed", { msg: kbErrorMessage(upBody, up.status) }));
        }
      }
      toast(t("knowledge.create.done", { name: name.trim() }));
      onCreated(kbId);
    } catch (err) {
      setError(String(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <section>
      <ViewHead
        kicker={t("knowledge.kicker")}
        title={t("knowledge.create.pageTitle")}
        meta={t("knowledge.create.pageMeta")}
      />
      <div style={{ marginBottom: 14 }}>
        <Btn onClick={onBack}>◂ {t("knowledge.title")}</Btn>
      </div>
      <div className="eval-grid">
        <Panel
          brk
          title={t("knowledge.create.formTitle")}
          sub={t("knowledge.create.pageMeta")}
          style={{ "--i": 0 } as CSSProperties}
        >
          <div data-testid="kb-create-form">
            <div className="field">
              <label htmlFor="kb-name">{t("knowledge.create.name")}</label>
              <input
                id="kb-name"
                className="input mono"
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="product-docs"
                data-testid="kb-name-input"
              />
              {name.trim().length > 0 && !nameValid && (
                <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
                  {t("knowledge.create.nameInvalid")}
                </div>
              )}
            </div>
            <div className="field">
              <label htmlFor="kb-desc">{t("knowledge.create.description")}</label>
              <textarea
                id="kb-desc"
                className="input"
                style={{ minHeight: 84, resize: "vertical" }}
                value={description}
                onChange={(e) => setDescription(e.target.value)}
                placeholder={t("knowledge.create.descriptionPlaceholder")}
                data-testid="kb-desc-input"
              />
              <div className="dim" style={{ fontSize: 11, marginTop: 6 }}>
                {t("knowledge.create.descriptionHint")}
              </div>
            </div>

            <SourcePicker
              mode={mode}
              onModeChange={setMode}
              files={files}
              onFilesChange={setFiles}
              bucket={bucket}
              onBucketChange={setBucket}
              prefix={prefix}
              onPrefixChange={setPrefix}
            />

            {error && (
              <div
                className="note"
                style={{ color: "var(--crit)", borderColor: "var(--crit)" }}
                data-testid="kb-create-error"
              >
                <span className="i">!</span>
                <span>{error}</span>
              </div>
            )}
            <div style={{ display: "flex", justifyContent: "flex-end", marginTop: 12 }}>
              <Btn primary disabled={!canSubmit} onClick={() => void submit()} data-testid="kb-submit">
                ▲ {busy ? t("knowledge.create.creating") : t("knowledge.create.submit")}
              </Btn>
            </div>
          </div>
        </Panel>

        <Panel
          title={t("knowledge.create.how.title")}
          sub={t("knowledge.create.how.sub")}
          style={{ "--i": 1 } as CSSProperties}
        >
          {(["s1", "s2", "s3", "s4"] as const).map((step, i) => (
            <div className="kv" key={step}>
              <span className="k mono">{`0${i + 1}`}</span>
              <span className="v" style={{ textAlign: "left", flex: 1, marginLeft: 12 }}>
                {t(`knowledge.create.how.${step}`)}
              </span>
            </div>
          ))}
          <div className="note" style={{ marginTop: 10 }}>
            <span className="i">[i]</span>
            <span>{t("knowledge.create.how.note")}</span>
          </div>
        </Panel>
      </div>
    </section>
  );
}
