import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import type { BlockedReason } from "../../../lib/skillLabArtifacts";
import { classifyArtifactHref } from "../../../lib/skillLabArtifacts";

/**
 * Markdown preview of a job artifact (report.md, best_skill.md, …). Relative
 * links open through the scoped artifacts API from the current file's
 * directory; http/https/mailto open in a new tab; anything else is inert text.
 * Images are never fetched (a remote one would leak the reader's IP to whoever
 * wrote the artifact) — they render as a placeholder naming the source. No
 * rehype-raw: HTML in the artifact stays inert.
 */
export function ArtifactMarkdown({
  text,
  baseDir,
  onOpenArtifact,
}: {
  text: string;
  baseDir: string;
  onOpenArtifact: (path: string) => void;
}) {
  const { t } = useTranslation();

  const blockedTitle = (reason: BlockedReason) => {
    switch (reason) {
      case "outside":
        return t("skillLab.eval.artifacts.linkOutside");
      case "absolute":
        return t("skillLab.eval.artifacts.linkAbsolute");
      case "malformed":
        return t("skillLab.eval.artifacts.linkMalformed");
      default:
        return t("skillLab.eval.artifacts.linkBlocked");
    }
  };

  const renderLink = (href: string | undefined, children: ReactNode) => {
    const target = classifyArtifactHref(href, baseDir);
    if (target.kind === "external") {
      return (
        <a href={target.href} target="_blank" rel="noreferrer noopener">
          {children}
        </a>
      );
    }
    if (target.kind === "artifact") {
      return (
        <button
          type="button"
          className="v2-link"
          data-artifact-link={target.path}
          title={t("skillLab.eval.artifacts.openLink", { path: target.path || "out/" })}
          onClick={() => onOpenArtifact(target.path)}
        >
          {children}
        </button>
      );
    }
    return (
      <span className="v2-muted blocked" data-blocked-link={target.reason} title={blockedTitle(target.reason)}>
        {children}
      </span>
    );
  };

  return (
    <div className="v2-skilllab-md" data-testid="v2-skilllab-markdown">
      <ReactMarkdown
        remarkPlugins={[remarkGfm]}
        rehypePlugins={[rehypeHighlight]}
        // Identity: the components below classify the *original* URL themselves
        // (the default transform would blank a `javascript:` href before we
        // could label it as blocked).
        urlTransform={(url) => url}
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
            const target = classifyArtifactHref(source, baseDir);
            const label = t("skillLab.eval.artifacts.image", { src: source, alt: alt ?? "" });
            return (
              <span className="mono v2-muted" data-artifact-image={source}>
                {target.kind === "artifact" ? renderLink(source, label) : label}
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
