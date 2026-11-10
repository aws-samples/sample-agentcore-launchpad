import { ExternalLink, FileText } from "lucide-react";
import { useTranslation } from "react-i18next";

import { zonedDate } from "../../../lib/timestamps";
import { documentFormat, type LibraryVideo, type VideoCollection, type VideoLocale } from "../../../lib/videos";
import { Card, FlowHeader, LinkButton, Tag } from "../../ui";
import { VideoEpisodeList } from "./playback";

/**
 * Document items (workshop / student manuals) — a PDF and/or a self-contained HTML page
 * staged under `/media/` on the console host. The console never embeds them (the CSP has
 * `frame-src 'none'`): every link opens the file in a new tab, same origin as the page.
 */

/** 「打开手册」 plus the other-format twin when the manual has one. */
export function DocumentLinks({ video, compact }: { video: LibraryVideo; compact?: boolean }) {
  const { t } = useTranslation();
  const primary = documentFormat(video.documentUrl);
  return (
    <span className="v2-videos-doc-links">
      <a
        className={`v2-btn primary${compact ? " sm" : ""}`}
        href={video.documentUrl}
        target="_blank"
        rel="noopener noreferrer"
        data-testid={`document-open-${video.id}`}
      >
        <ExternalLink size={14} aria-hidden="true" />
        {t("videos.openDocument")} · {t(`videos.format.${primary}`)}
      </a>
      {video.altUrl && (
        <a
          className={`v2-btn${compact ? " sm" : ""}`}
          href={video.altUrl}
          target="_blank"
          rel="noopener noreferrer"
          title={t("videos.openDocument")}
          data-testid={`document-alt-${video.id}`}
        >
          {t(`videos.format.${documentFormat(video.altUrl)}`)}
        </a>
      )}
    </span>
  );
}

/** The library card's thumbnail for a manual: poster when there is one, else the icon. */
export function DocumentThumb({ video }: { video: LibraryVideo }) {
  return (
    <span className="thumb doc">
      {video.posterUrl && <img src={video.posterUrl} alt="" loading="lazy" />}
      <FileText className="doc-icon" size={40} aria-hidden="true" />
    </span>
  );
}

/**
 * The detail route (`?view=watch&video=<id>`) for a document: title, introduction and the
 * open links — no `<video>`. A section with several items keeps its list on the side.
 */
export function DocumentDetail({
  video,
  collection,
  locale,
  onSelect,
  onBack,
}: {
  video: LibraryVideo;
  collection: VideoCollection;
  locale: VideoLocale;
  onSelect: (id: string) => void;
  onBack: () => void;
}) {
  const { t } = useTranslation();
  const hasSeries = collection.videos.length > 1;
  return (
    <section aria-label={t("videos.documentDetail")} data-testid="document-detail">
      <FlowHeader
        onBack={onBack}
        title={collection.title[locale]}
        end={
          <div className="v2-row">
            <Tag tone="outline">{t("videos.kind.document")}</Tag>
            <Tag tone={video.consoleVersion === "v2" ? "blue" : "gray"}>
              {t(`videos.version.${video.consoleVersion}`)}
            </Tag>
          </div>
        }
      />
      <div className={hasSeries ? "v2-videos-layout" : undefined}>
        <Card testId="document-panel">
          <h2 className="v2-videos-title">{video.title[locale]}</h2>
          <div className="v2-videos-meta">
            <span>{t("videos.published", { date: zonedDate(video.publishedAt) || video.publishedAt })}</span>
            <span>
              {t("videos.documentFormats")} · {t(`videos.format.${documentFormat(video.documentUrl)}`)}
              {video.altUrl ? ` / ${t(`videos.format.${documentFormat(video.altUrl)}`)}` : ""}
            </span>
          </div>
          <p className="v2-videos-doc-intro">{video.description[locale]}</p>
          <DocumentLinks video={video} />
          <p className="v2-videos-help v2-videos-doc-hint">{t("videos.documentOpensNewTab")}</p>
        </Card>
        {hasSeries && (
          <Card
            flush
            title={collection.title[locale]}
            sub={t("videos.episodeCount", { count: collection.videos.length })}
            testId="video-directory"
          >
            <VideoEpisodeList videos={collection.videos} currentId={video.id} locale={locale} onSelect={onSelect} />
            <div className="v2-videos-dir-foot">
              <LinkButton onClick={onBack} testId="document-back-to-library">{t("videos.changeModule")}</LinkButton>
            </div>
          </Card>
        )}
      </div>
    </section>
  );
}
