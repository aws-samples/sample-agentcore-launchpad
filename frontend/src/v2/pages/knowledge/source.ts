import type { KBSourceBody, KBSourceMode } from "../../../lib/knowledgeBases";

export interface SourceDraft {
  mode: KBSourceMode;
  files: File[];
  bucket: string;
  prefix: string;
}

export const emptySource = (): SourceDraft => ({ mode: "upload", files: [], bucket: "", prefix: "" });

/** The API body for a draft (files are uploaded separately). */
export function sourceBody(draft: SourceDraft): KBSourceBody {
  return draft.mode === "upload"
    ? ({ mode: "upload" } as const)
    : ({ mode: "existing", bucket: draft.bucket.trim(), prefix: draft.prefix.trim() || undefined } as const);
}
