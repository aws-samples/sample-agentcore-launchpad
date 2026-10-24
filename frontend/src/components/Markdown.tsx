import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { isS3ObjectUrl, openKbSource } from "../lib/kb-source";
import { remarkModelText } from "../lib/remark-model-text";

/** Chat-bubble markdown: GFM + hljs code highlighting, themed via `.md` in
 * app.css. No rehype-raw — model-emitted HTML stays escaped text. `remarkModelText`
 * repairs model output CommonMark mis-renders (citation markers, CJK bold). A link
 * to an S3 object (a knowledge-base source the agent cites) opens through a
 * presigned URL; `onSourceError` reports a source that cannot be opened. */
export function Markdown({
  text,
  onSourceError,
}: {
  text: string;
  onSourceError?: (message: string) => void;
}) {
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkModelText]}
        rehypePlugins={[rehypeHighlight]}
        components={{
          a: ({ node, children, href, ...props }) => {
            void node;
            if (isS3ObjectUrl(href)) {
              return (
                <a
                  {...props}
                  href={href}
                  className="md-kb-source"
                  data-testid="md-kb-source"
                  onClick={(e) => {
                    e.preventDefault();
                    void openKbSource(href, onSourceError ?? (() => undefined));
                  }}
                >
                  {children}
                </a>
              );
            }
            return (
              <a {...props} href={href} target="_blank" rel="noreferrer">
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
