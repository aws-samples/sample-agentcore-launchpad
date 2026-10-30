import { useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { errorMessage, v2KnowledgeApi } from "../../../lib/api";
import { KB_DESCRIPTION_MAX, KB_NAME_RE } from "../../../lib/knowledgeBases";
import { useV2Toast } from "../../hooks";
import { Alert, Button, Card, Field, FlowHeader } from "../../ui";
import { emptySource, sourceBody, type SourceDraft } from "./source";
import { SourceFields } from "./SourceFields";

const HOW_STEPS = ["s1", "s2", "s3", "s4"] as const;

/**
 * `?view=new` — create a managed KB. The POST answers 202 while the KB is still
 * CREATING (the backend finishes the data source on its own thread); picked
 * files are uploaded right away (uploads are allowed ahead of the data source,
 * and the server starts the ingestion sync once every file landed — N17-obs204),
 * then the detail page takes over polling. An upload that fails after the create
 * keeps the page here (N17-obs204 forensics: the request never left the browser
 * while the page toasted success and navigated): the created KB is named, and the
 * primary action becomes "Retry upload" to the same KB — nothing is created twice.
 */
export function KbCreate() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  const [, setParams] = useSearchParams();
  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [source, setSource] = useState<SourceDraft>(emptySource);
  const [touched, setTouched] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // the KB a previous submit created whose upload then failed — the retry target
  const [createdId, setCreatedId] = useState<string | null>(null);

  const nameValid = KB_NAME_RE.test(name.trim());
  const sourceValid = source.mode === "upload" ? source.files.length > 0 : source.bucket.trim().length > 0;
  const nameError = (touched || name.trim()) && !nameValid ? t("knowledge.create.disabledName") : null;
  const sourceError =
    touched && !sourceValid
      ? t(source.mode === "upload" ? "knowledge.create.disabledFiles" : "knowledge.create.disabledBucket")
      : null;

  const openDetail = (id: string) => setParams({ view: "detail", id }, { replace: true });

  const submit = async () => {
    setTouched(true);
    if (!nameValid || !sourceValid) return;
    setBusy(true);
    setError(null);
    try {
      let id = createdId;
      if (!id) {
        const kb = await v2KnowledgeApi.create({
          name: name.trim(),
          description: description.trim(),
          source: sourceBody(source),
        });
        id = kb.kb_id;
        setCreatedId(id);
      }
      if (source.mode === "upload" && source.files.length > 0) {
        try {
          // the server starts the ingestion once every file landed (N17-obs204)
          await v2KnowledgeApi.uploadFiles(id, source.files);
        } catch (err) {
          // the KB exists already — stay here and offer the retry to the SAME KB; the
          // detail page would otherwise show an empty source with nothing to index
          setError(t("knowledge.create.uploadFailed", { msg: errorMessage(err) }));
          return;
        }
      }
      toast("success", t("knowledge.create.done", { name: name.trim() }));
      openDetail(id);
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <FlowHeader
        title={t("knowledge.create.pageTitle")}
        onBack={() => setParams({})}
        end={
          <>
            <Button onClick={() => setParams({})}>{t("v2.common.cancel")}</Button>
            {createdId && (
              <Button onClick={() => openDetail(createdId)} testId="v2-kb-open-created">
                {t("knowledge.create.openCreated")}
              </Button>
            )}
            <Button kind="primary" disabled={busy} onClick={() => void submit()} testId="v2-kb-submit">
              {busy
                ? t(createdId ? "knowledge.create.uploading" : "knowledge.create.creating")
                : t(createdId ? "knowledge.create.retryUpload" : "knowledge.create.submit")}
            </Button>
          </>
        }
      />
      {error && (
        <Alert tone="error">
          <span data-testid="v2-kb-create-error">{error}</span>
        </Alert>
      )}
      {createdId && !busy && (
        <Alert tone="warn">
          <span data-testid="v2-kb-created-pending-upload">
            {t("knowledge.create.createdPendingUpload", { name: name.trim() })}{" "}
            <span className="mono" data-testid="v2-kb-created-id">{createdId}</span>
          </span>
        </Alert>
      )}
      <Card title={t("v2.knowledge.basic")}>
        <div className="v2-form cols-2">
          <Field label={t("knowledge.create.name")} required error={nameError} hint={t("v2.knowledge.nameHint")}>
            <input
              className="v2-input mono"
              value={name}
              maxLength={100}
              onChange={(e) => setName(e.target.value)}
              placeholder="product-docs"
              data-testid="v2-kb-name"
            />
          </Field>
          <div />
          <Field
            label={t("knowledge.create.description")}
            full
            hint={`${t("knowledge.create.descriptionHint")} (${description.length}/${KB_DESCRIPTION_MAX})`}
          >
            <textarea
              className="v2-textarea"
              value={description}
              maxLength={KB_DESCRIPTION_MAX}
              onChange={(e) => setDescription(e.target.value)}
              placeholder={t("knowledge.create.descriptionPlaceholder")}
              data-testid="v2-kb-desc"
            />
          </Field>
        </div>
      </Card>
      <Card title={t("knowledge.source.label")} sub={t("knowledge.create.pageMeta")}>
        <SourceFields value={source} onChange={setSource} error={sourceError} />
      </Card>
      <Card title={t("knowledge.create.how.title")} sub={t("knowledge.create.how.sub")}>
        <ol className="v2-knowledge-how">
          {HOW_STEPS.map((step, i) => (
            <li key={step}>
              <span className="num">{i + 1}</span>
              <span>{t(`knowledge.create.how.${step}`)}</span>
            </li>
          ))}
        </ol>
        <Alert>{t("knowledge.create.how.note")}</Alert>
      </Card>
    </>
  );
}
