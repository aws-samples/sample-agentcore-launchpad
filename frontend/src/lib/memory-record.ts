/**
 * Long-term memory records come back from AgentCore as text the strategy wrote: a
 * fact or preference is a sentence, but the summarization strategy writes
 * `<topic name="…">…</topic>` blocks whose bodies are markdown (lists, tables). The
 * console shows those as titled sections with rendered markdown instead of the raw
 * XML and table source.
 */
export interface MemoryRecordSection {
  /** the `<topic name>`; null for text outside any topic (or a record without topics) */
  title: string | null;
  /** markdown */
  body: string;
}

const TOPIC_RE = /<topic\s+name\s*=\s*(?:"([^"]*)"|'([^']*)')\s*>([\s\S]*?)<\/topic\s*>/gi;

const ENTITIES: Record<string, string> = { amp: "&", lt: "<", gt: ">", quot: '"', apos: "'", "#39": "'" };

function decodeEntities(value: string): string {
  return value.replace(/&(amp|lt|gt|quot|apos|#39);/g, (_, name: string) => ENTITIES[name] ?? _);
}

function pushText(out: MemoryRecordSection[], text: string) {
  const body = text.trim();
  if (body) out.push({ title: null, body });
}

/** A record's text as sections: one per `<topic>`, plus any text around them. */
export function memoryRecordSections(text: string): MemoryRecordSection[] {
  const out: MemoryRecordSection[] = [];
  let last = 0;
  for (const match of text.matchAll(TOPIC_RE)) {
    pushText(out, text.slice(last, match.index));
    const title = decodeEntities((match[1] ?? match[2] ?? "").trim()) || null;
    const body = match[3].trim();
    if (title || body) out.push({ title, body });
    last = (match.index ?? 0) + match[0].length;
  }
  pushText(out, text.slice(last));
  return out;
}

/** One line for a clamped list cell: `title：body` per topic, markdown kept as text,
 *  whitespace collapsed. */
export function memoryRecordPlainText(text: string, separator = "："): string {
  return memoryRecordSections(text)
    .map((s) => (s.title ? `${s.title}${separator}${s.body}` : s.body))
    .join(" · ")
    .replace(/\s+/g, " ")
    .trim();
}
