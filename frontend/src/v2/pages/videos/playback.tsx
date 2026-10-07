import { Play } from "lucide-react";
import { useTranslation } from "react-i18next";

import { type LibraryVideo, type VideoLocale, videoTimestamp } from "../../../lib/videos";

/** Title, duration/date and the collapsed introduction under the player. */
export function VideoSummary({ video, locale }: { video: LibraryVideo; locale: VideoLocale }) {
  const { t } = useTranslation();
  return (
    <>
      <h2 className="v2-videos-title">{video.title[locale]}</h2>
      <div className="v2-videos-meta">
        <span>{t("videos.duration", { duration: videoTimestamp(video.durationSeconds) })}</span>
        <span>{t("videos.published", { date: video.publishedAt.slice(0, 10) })}</span>
      </div>
      <details className="v2-videos-summary">
        <summary>{t("videos.summary")}</summary>
        <p>{video.description[locale]}</p>
      </details>
    </>
  );
}

/** A module's playlist; the current episode is marked. */
export function VideoEpisodeList({
  videos,
  currentId,
  locale,
  onSelect,
}: {
  videos: LibraryVideo[];
  currentId: string;
  locale: VideoLocale;
  onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  return (
    <ol className="v2-videos-scroll v2-videos-episodes" aria-label={t("videos.series")}>
      {videos.map((item, i) => {
        const on = item.id === currentId;
        return (
          <li key={item.id}>
            <button
              type="button"
              className={on ? "on" : undefined}
              aria-current={on ? "true" : undefined}
              onClick={() => onSelect(item.id)}
              data-testid={`video-entry-${item.id}`}
            >
              <span className="thumb">
                {item.posterUrl && <img src={item.posterUrl} alt="" loading="lazy" />}
                {on && <Play size={16} aria-hidden="true" />}
              </span>
              <span className="text">
                <strong>{item.title[locale]}</strong>
                <small>
                  {String(i + 1).padStart(2, "0")} · {videoTimestamp(item.durationSeconds)}
                  {on ? ` · ${t("videos.selected")}` : ""}
                </small>
              </span>
            </button>
          </li>
        );
      })}
    </ol>
  );
}

/** Chapter list; selecting one seeks and plays. */
export function VideoChapterList({
  video,
  locale,
  disabled,
  onSeek,
}: {
  video: LibraryVideo;
  locale: VideoLocale;
  disabled: boolean;
  onSeek: (startSeconds: number) => void;
}) {
  const { t } = useTranslation();
  return (
    <div className="v2-videos-scroll" role="region" aria-label={t("videos.episodeChapters")}>
      <p className="v2-videos-help">{t("videos.chapterHint")}</p>
      <ol className="v2-videos-chapters">
        {video.chapters.map((chapter) => (
          <li key={chapter.startSeconds}>
            <button type="button" disabled={disabled} onClick={() => onSeek(chapter.startSeconds)}>
              <span className="time">{videoTimestamp(chapter.startSeconds)}</span>
              <span>{chapter.title[locale]}</span>
            </button>
          </li>
        ))}
      </ol>
    </div>
  );
}
