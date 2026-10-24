import { useId } from "react";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";
import { isS3ObjectUrl, openKbSource } from "../lib/kb-source";
import { remarkModelText } from "../lib/remark-model-text";

/** Chat-bubble markdown: GFM + hljs code highlighting, themed via `.md` in
 * app.css. No rehype-raw — model-emitted HTML stays escaped text. `remarkModelText`
 * repairs model output CommonMark mis-renders (citation markers, CJK bold). A link
 * to an S3 object (a knowledge-base source the agent cites) opens through a
 * presigned URL; `onSourceError` reports a source that cannot be opened. GFM footnotes
 * get a per-instance id prefix (a thread renders many answers, each with its own
 * `[^1]`), and their in-page `#…` links scroll within the page instead of opening a
 * new tab. */
export function Markdown({
  text,
  onSourceError,
}: {
  text: string;
  onSourceError?: (message: string) => void;
}) {
  const prefix = `md-${useId().replace(/[^a-zA-Z0-9]/g, "")}-`;
  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={[remarkGfm, remarkModelText]}
        rehypePlugins={[rehypeHighlight]}
        remarkRehypeOptions={{ clobberPrefix: prefix }}
        components={{
          a: ({ node, children, href, ...props }) => {
            void node;
            if (href && isS3ObjectUrl(href)) {
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
            if (href?.startsWith("#")) {
              return (
                <a
                  {...props}
                  href={href}
                  onClick={(e) => {
                    e.preventDefault();
                    const target = document.getElementById(decodeURIComponent(href.slice(1)));
                    target?.scrollIntoView({ block: "nearest", behavior: "smooth" });
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
