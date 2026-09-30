import { useEffect, useState } from "react";
import { useTranslation } from "react-i18next";
import type { AttachmentCapability, ChatAttachmentMetadata } from "../../lib/api";
import type { PendingAttachment } from "./attachments";
import { formatAttachmentSize } from "./attachments";

function ImagePreview({ file }: { file: File }) {
  const [url, setUrl] = useState<string>();
  useEffect(() => {
    const next = URL.createObjectURL(file);
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [file]);
  return url ? <img className="chat-attachment-preview" src={url} alt="" /> : null;
}

export function PendingAttachments({
  files,
  disabled,
  onRemove,
}: {
  files: PendingAttachment[];
  disabled: boolean;
  onRemove: (id: string) => void;
}) {
  const { t } = useTranslation();
  if (!files.length) return null;
  return (
    <ul className="chat-attachments" aria-label={t("chatPage.attachments.pending")} data-testid="pending-attachments">
      {files.map(({ id, file, mediaType }) => (
        <li className="chat-attachment" key={id}>
          {mediaType.startsWith("image/") ? <ImagePreview file={file} /> : <span aria-hidden="true">▤</span>}
          <span className="chat-attachment-info">
            <span className="chat-attachment-name" title={file.name}>{file.name}</span>
            <span className="dim">{formatAttachmentSize(file.size)}</span>
          </span>
          <button
            type="button"
            className="chat-attachment-remove"
            aria-label={t("chatPage.attachments.remove", { name: file.name })}
            title={t("chatPage.attachments.remove", { name: file.name })}
            disabled={disabled}
            onClick={() => onRemove(id)}
          >×</button>
        </li>
      ))}
    </ul>
  );
}

export function MessageAttachments({ files }: { files?: ChatAttachmentMetadata[] }) {
  const { t } = useTranslation();
  if (!files?.length) return null;
  return (
    <ul className="chat-attachments" aria-label={t("chatPage.attachments.sent")} data-testid="message-attachments">
      {files.map((file, index) => (
        <li className="chat-attachment" key={index}>
          <span aria-hidden="true">▤</span>
          <span className="chat-attachment-info">
            <span className="chat-attachment-name" title={file.name}>{file.name}</span>
            <span className="dim">
              {formatAttachmentSize(file.size)} · {t(`chatPage.attachments.delivery.${file.delivery}`)}
            </span>
          </span>
        </li>
      ))}
    </ul>
  );
}

export function AttachmentHint({ capability }: { capability?: AttachmentCapability }) {
  const { t } = useTranslation();
  const supported = capability && (capability.images || capability.text || capability.pdf !== "unsupported");
  const reasonKey = `chatPage.attachments.reasons.${capability?.reason_code}`;
  const reason = capability?.reason_code && [
    "harness", "republish", "custom", "model", "not_active",
  ].includes(capability.reason_code) ? t(reasonKey) : null;
  const formats = capability ? [
    capability.images && t("chatPage.attachments.images"),
    capability.text && t("chatPage.attachments.text"),
    capability.pdf === "native" && t("chatPage.attachments.pdfNative"),
    capability.pdf === "text" && t("chatPage.attachments.pdfText"),
  ].filter(Boolean).join(" · ") : "";
  return (
    <div className="chat-attachment-hint" id="chat-attachment-hint" data-testid="attachment-capability">
      {supported ? (
        <>
          <span>{formats}</span>
          {capability.pdf === "text" && !reason && <span>{t("chatPage.attachments.extractionHint")}</span>}
          <span>{t("chatPage.attachments.limits", {
            count: capability.max_files,
            fileSize: formatAttachmentSize(capability.max_file_bytes),
            totalSize: formatAttachmentSize(capability.max_total_bytes),
          })}</span>
        </>
      ) : !reason ? t("chatPage.attachments.unavailable") : null}
      {reason && <span data-testid="attachment-capability-reason">{reason}</span>}
    </div>
  );
}
