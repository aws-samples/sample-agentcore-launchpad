import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { remarkModelText } from "../lib/remark-model-text";

/** Chat-bubble markdown: GFM + hljs code highlighting, themed via `.md` in
 * app.css. No rehype-raw — model-emitted HTML stays escaped text. `remarkModelText`
 * repairs model output CommonMark mis-renders (citation markers, CJK bold). */
export function Markdown({ text }: { text: string }) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkModelText]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          a: ({ node, children, ...props }) => {
            void node;
            return (
              <a {...props} target="_blank" rel="noreferrer">
                {children}
              </a>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
