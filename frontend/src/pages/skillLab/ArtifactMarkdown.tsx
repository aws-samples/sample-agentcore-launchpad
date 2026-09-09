import type { ReactNode } from "react";
import { useTranslation } from "react-i18next";
import ReactMarkdown from "react-markdown";
import rehypeHighlight from "rehype-highlight";
import remarkGfm from "remark-gfm";

import type { BlockedReason } from "./artifactLinks";
import { classifyArtifactHref } from "./artifactLinks";

const linkButtonStyle = {
  background: "none",
  border: 0,
  padding: 0,
  font: "inherit",
  color: "var(--s1)",
  textDecoration: "underline",
  textUnderlineOffset: 2,
  cursor: "pointer",
} as const;

/**
 * Markdown preview for a job artifact (report.md, best_skill.md, …).
 *
 * Same renderer stack as the Chat bubble (GFM + hljs, themed by `.md`), but a
 * separate component: the link policy is different. Relative links are opened
 * through the scoped artifacts API from the current file's directory
 * (`onOpenArtifact`), http/https/mailto links open in a new tab with a safe
 * rel, anything else is rendered as inert text. Images are never fetched — a
 * remote image would leak the reader's IP to whoever wrote the artifact, and a
 * relative one has no header-carrying `<img src>` path anyway — so they render
 * as a placeholder naming the source. No rehype-raw: HTML in the artifact is
 * inert, and Source mode shows it verbatim.
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
          style={linkButtonStyle}
          data-artifact-link={target.path}
          title={t("skillLab.eval.artifacts.openLink", { path: target.path || "out/" })}
          onClick={() => onOpenArtifact(target.path)}
        >
          {children}
        </button>
      );
    }
    return (
      <span
        className="dim"
        data-blocked-link={target.reason}
        title={blockedTitle(target.reason)}
        style={{ textDecoration: "line-through dotted" }}
      >
        {children}
      </span>
    );
  };

  return (
    <div className="md" data-testid="artifact-markdown">
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
              <span className="mono dim" data-artifact-image={source} style={{ fontSize: 10.5 }}>
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
