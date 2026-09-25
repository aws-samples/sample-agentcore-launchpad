import { Paperclip, SendHorizontal } from "lucide-react";
import { useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { PendingAttachments } from "../../../components/chat/Attachments";
import type { PendingAttachment } from "../../../components/chat/attachments";
import type { AttachmentCapability } from "../../../lib/api";
import { Button } from "../../ui";

/** Message box + attachment tray. Drop, paste (images) and the picker all feed `onAddFiles`. */
export function Composer({
  value,
  onChange,
  onSend,
  onAddFiles,
  onRemoveFile,
  files,
  attachmentError,
  capability,
  attachmentsEnabled,
  disabled,
  sendDisabledReason,
  placeholder,
}: {
  value: string;
  onChange: (value: string) => void;
  onSend: () => void;
  onAddFiles: (files: File[]) => void;
  onRemoveFile: (id: string) => void;
  files: PendingAttachment[];
  attachmentError: string | null;
  capability?: AttachmentCapability;
  attachmentsEnabled: boolean;
  /** busy / restoring / no agent */
  disabled: boolean;
  sendDisabledReason?: string;
  placeholder: string;
}) {
  const { t } = useTranslation();
  const fileInputRef = useRef<HTMLInputElement>(null);
  const [dragging, setDragging] = useState(false);

  return (
    <div
      className={`v2-chat-composer${dragging ? " dragging" : ""}`}
      data-testid="attachment-composer"
      onDragOver={(e) => {
        if (!e.dataTransfer.types.includes("Files")) return;
        e.preventDefault();
        e.dataTransfer.dropEffect = disabled ? "none" : "copy";
        if (!disabled) setDragging(true);
      }}
      onDragLeave={(e) => {
        if (!e.currentTarget.contains(e.relatedTarget as Node | null)) setDragging(false);
      }}
      onDrop={(e) => {
        if (!e.dataTransfer.types.includes("Files")) return;
        e.preventDefault();
        setDragging(false);
        onAddFiles(Array.from(e.dataTransfer.files));
      }}
      onPaste={(e) => {
        const images = Array.from(e.clipboardData.files).filter((file) => file.type.startsWith("image/"));
        if (!images.length) return;
        e.preventDefault();
        onAddFiles(images);
      }}
    >
      <PendingAttachments files={files} disabled={disabled} onRemove={onRemoveFile} />
      {attachmentError && (
        <div className="v2-chat-attach-error" role="alert" data-testid="attachment-error">
          {attachmentError}
        </div>
      )}
      <textarea
        className="v2-chat-input"
        rows={2}
        value={value}
        onChange={(e) => onChange(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
            e.preventDefault();
            onSend();
          }
        }}
        placeholder={placeholder}
        disabled={disabled}
        aria-label={t("chatPage.messageLabel")}
        data-testid="chat-input"
      />
      <div className="v2-chat-composer-bar">
        <input
          ref={fileInputRef}
          type="file"
          hidden
          multiple
          accept={capability?.accept.join(",")}
          disabled={disabled || !attachmentsEnabled}
          onChange={(e) => {
            onAddFiles(Array.from(e.target.files ?? []));
            e.target.value = "";
          }}
          aria-label={t("chatPage.attachments.add")}
          data-testid="attachment-input"
        />
        <Button
          size="sm"
          disabled={disabled || !attachmentsEnabled}
          title={t(attachmentsEnabled ? "chatPage.attachments.add" : "chatPage.attachments.unavailable")}
          onClick={() => fileInputRef.current?.click()}
          testId="attachment-picker"
        >
          <Paperclip size={14} aria-hidden="true" />
          {t("v2.chat.attach")}
        </Button>
        <span className="v2-chat-composer-hint">
          {attachmentsEnabled ? t("v2.chat.dropHint") : t("v2.chat.enterHint")}
        </span>
        <Button
          kind="primary"
          size="sm"
          disabled={disabled || sendDisabledReason !== undefined}
          title={sendDisabledReason}
          onClick={onSend}
          testId="chat-send"
        >
          <SendHorizontal size={14} aria-hidden="true" />
          {t("v2.chat.send")}
        </Button>
      </div>
    </div>
  );
}
