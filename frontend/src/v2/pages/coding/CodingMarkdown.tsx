import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import { classifyCodingHref, type CodingLinkBlocked } from "./codingLinks";

/** Open a project file in the editor, optionally at a line (and column). */
export type OpenFileHandler = (path: string, line: number | null, column: number | null) => void;

const REMARK = [remarkGfm];
const REHYPE = [rehypeHighlight];
// identity: the components below classify the *original* URL themselves (the default
// transform would blank a `javascript:` href before it could be labelled as blocked)
const keepUrl = (url: string) => url;

/**
 * Markdown from the coding agent (messages, reasoning, plans) and the editor's `.md`
 * preview, styled like the shared `components/Markdown` (`.md`). Links into the project
 * — codex writes them absolutely, `/mnt/workspace/project/main.py#L12` — open the file
 * in the IDE; relative links resolve against `baseDir` (the previewed file's directory,
 * the project root for agent text). http/https/mailto open in a new tab; anything else
 * is inert text. Images are never fetched (a remote one would leak the reader's IP to
 * whoever wrote the text): a placeholder names the source. No rehype-raw — HTML stays
 * escaped text.
 */
export function CodingMarkdown({
  text,
  baseDir = "",
  onOpenFile,
}: {
  text: string;
  baseDir?: string;
  onOpenFile?: OpenFileHandler;
}) {
  const { t } = useTranslation();

  const blockedTitle = (reason: CodingLinkBlocked) =>
    reason === "outside" ? t("v2.coding.markdown.linkOutside") : t("v2.coding.markdown.linkBlocked");

  const renderLink = (href: string | undefined, children: ReactNode) => {
    const target = classifyCodingHref(href, baseDir);
    if (target.kind === "external") {
      return (
        <a href={target.href} target="_blank" rel="noreferrer noopener">
          {children}
        </a>
      );
    }
    if (target.kind === "file" && onOpenFile) {
      return (
        <button
          type="button"
          className="v2-coding-mdlink"
          data-coding-file-link={target.path}
          title={
            target.line !== null
              ? t("v2.coding.markdown.openFileLine", { path: target.path, line: target.line })
              : t("v2.coding.markdown.openFile", { path: target.path })
          }
          onClick={() => onOpenFile(target.path, target.line, target.column)}
        >
          {children}
        </button>
      );
    }
    return (
      <span
        className="v2-coding-mdlink-off"
        data-blocked-link={target.kind === "blocked" ? target.reason : "file"}
        title={target.kind === "blocked" ? blockedTitle(target.reason) : undefined}
      >
        {children}
      </span>
    );
  };

  return (
    <div className="md">
      <ReactMarkdown
        remarkPlugins={REMARK}
        rehypePlugins={REHYPE}
        urlTransform={keepUrl}
        components={{
          a: ({ node, href, children, ...rest }) => {
            void node;
            void rest;
            return renderLink(href, children);
          },
          img: ({ node, src, alt, ...rest }) => {
            void node;
            void rest;
            const source = typeof src === "string" ? src : "";
            return (
              <span className="mono v2-muted" data-coding-image={source}>
                {t("v2.coding.markdown.image", { src: source, alt: alt ?? "" })}
              </span>
            );
          },
        }}
      >
        {text}
      </ReactMarkdown>
    </div>
  );
}
