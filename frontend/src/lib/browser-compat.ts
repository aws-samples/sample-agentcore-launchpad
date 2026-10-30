import i18next from "i18next";

/** randomUUID is secure-context-only, but getRandomValues also works on HTTP. */
export function browserUuid(): string {
  if (typeof crypto.randomUUID === "function") return crypto.randomUUID();
  const bytes = crypto.getRandomValues(new Uint8Array(16));
  bytes[6] = (bytes[6] & 0x0f) | 0x40;
  bytes[8] = (bytes[8] & 0x3f) | 0x80;
  const hex = Array.from(bytes, (byte) => byte.toString(16).padStart(2, "0")).join("");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20)}`;
}

function manualCopy(text: string): void {
  const previous = document.activeElement;
  const dialog = document.createElement("dialog");
  dialog.className = "v2 v2-copy-dialog";
  dialog.dataset.testid = "manual-copy";
  dialog.setAttribute("aria-label", i18next.t("browserCompat.manualCopyTitle"));
  const title = document.createElement("h2");
  title.textContent = i18next.t("browserCompat.manualCopyTitle");
  const hint = document.createElement("p");
  hint.textContent = i18next.t("browserCompat.manualCopyHint");
  const field = document.createElement("textarea");
  field.className = "v2-textarea mono";
  field.value = text;
  field.readOnly = true;
  field.rows = 8;
  field.setAttribute("aria-label", i18next.t("browserCompat.copyText"));
  field.addEventListener("focus", () => field.select());
  const close = document.createElement("button");
  close.className = "v2-btn primary";
  close.type = "button";
  close.textContent = i18next.t("v2.common.close");
  close.addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => {
    field.value = "";
    dialog.remove();
    if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
  }, { once: true });
  dialog.append(title, hint, field, close);
  document.body.append(dialog);
  dialog.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      event.stopPropagation();
      event.preventDefault();
      dialog.close();
    }
  });
  dialog.showModal();
  field.focus();
  field.select();
}

/** True only when a browser copy succeeded. Otherwise show selectable text for manual
 * copying, without claiming success or persisting the copied value anywhere. */
export async function copyBrowserText(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    // Permissions can deny the modern API even on HTTPS; try the user-gesture fallback.
  }
  const previous = document.activeElement;
  const selection = window.getSelection();
  const ranges = selection ? Array.from({ length: selection.rangeCount }, (_, i) => selection.getRangeAt(i).cloneRange()) : [];
  const field = document.createElement("textarea");
  field.value = text;
  field.readOnly = true;
  field.style.cssText = "position:fixed;left:-10000px;top:0;";
  document.body.append(field);
  let copied = false;
  try {
    field.focus();
    field.select();
    copied = typeof document.execCommand === "function" && document.execCommand("copy");
  } catch {
    // A disabled legacy copy command still has a usable manual path.
  } finally {
    field.value = "";
    field.remove();
    if (previous instanceof HTMLElement && previous.isConnected) previous.focus();
    if (selection) {
      selection.removeAllRanges();
      for (const range of ranges) selection.addRange(range);
    }
  }
  if (!copied) manualCopy(text);
  return copied;
}
