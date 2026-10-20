import { useCallback, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { codingApi, errorMessage } from "../../../lib/api";
import { useV2Toast } from "../../hooks";

/**
 * Hand a URL to the browser as a download: a temporary link, clicked. The URL is a
 * presigned S3 GET answering `Content-Disposition: attachment`, so the browser saves
 * the file and the page stays where it is (no `download` attribute: it is ignored
 * cross-origin anyway, the header decides).
 */
export function triggerDownload(url: string): void {
  const link = document.createElement("a");
  link.href = url;
  link.rel = "noopener";
  link.style.display = "none";
  document.body.appendChild(link);
  link.click();
  link.remove();
}

const keyOf = (projectId: string, path?: string) => `${projectId}\u0000${path ?? ""}`;

/**
 * Downloads of coding-project files: one file (`path`) or, without a path, the whole
 * project as a zip. The backend has the runtime stage the bytes in S3 and answers a
 * short-lived URL, which is used at once. `pending` drives the buttons' spinner and
 * disabled state; a second click on the same target while it prepares is ignored;
 * a failure is a toast with the console's error copy.
 */
export function useCodingDownload() {
  const { t } = useTranslation();
  const toast = useV2Toast();
  // the in-flight set itself (read synchronously by a second click); `busy` mirrors it for rendering
  const inFlight = useRef(new Set<string>());
  const [busy, setBusy] = useState<ReadonlySet<string>>(() => new Set());

  const download = useCallback(
    async (projectId: string, path?: string) => {
      const key = keyOf(projectId, path);
      if (inFlight.current.has(key)) return;
      inFlight.current.add(key);
      setBusy(new Set(inFlight.current));
      try {
        const res = await codingApi.download(projectId, path);
        triggerDownload(res.url);
        toast("success", t("v2.coding.download.started", { name: res.filename }));
      } catch (err) {
        toast("error", errorMessage(err));
      } finally {
        inFlight.current.delete(key);
        setBusy(new Set(inFlight.current));
      }
    },
    [t, toast],
  );

  const pending = useCallback((projectId: string, path?: string) => busy.has(keyOf(projectId, path)), [busy]);

  return { download, pending };
}
