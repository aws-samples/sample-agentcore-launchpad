import { memoryRecordSections } from "../lib/memory-record";
import { Markdown } from "./Markdown";

/**
 * A long-term memory record's text, rendered: a summarization record's
 * `<topic name="…">` blocks become titled sections and their markdown (lists,
 * tables) is rendered the way chat answers are; a plain fact stays a sentence.
 */
export function MemoryRecordText({ text }: { text: string }) {
  const sections = memoryRecordSections(text);
  if (sections.length === 0) return null;
  return (
    <div className="memory-record-text" data-testid="memory-record-text">
      {sections.map((s, i) => (
        <section key={i} className="memory-record-section">
          {s.title && <div className="memory-record-topic">{s.title}</div>}
          <Markdown text={s.body} />
        </section>
      ))}
    </div>
  );
}
