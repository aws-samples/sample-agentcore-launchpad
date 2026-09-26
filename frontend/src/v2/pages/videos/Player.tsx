import { ChevronLeft, ChevronRight, Play } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import { type LibraryVideo, type VideoCollection, type VideoLocale, videoTimestamp } from "../../../lib/videos";
import { Alert, Button, Card, FlowHeader, LinkButton, Segmented, Tag } from "../../ui";

/**
 * 观看视频 (`?view=watch&video=`) — native `<video>` with source fallback, optional
 * captions (off by default: subtitles are burned in), chapter seek, and the series
 * playlist. Keyed on the video id by the router, so switching episodes remounts it.
 */
export function VideoPlayer({
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
  const playerRef = useRef<HTMLVideoElement>(null);
  const pendingSeek = useRef<number | null>(null);
  const failedSources = useRef(new Set<string>());
  const [mediaFailed, setMediaFailed] = useState(video.sources.length === 0);
  const [playBlocked, setPlayBlocked] = useState(false);
  const [captionFailed, setCaptionFailed] = useState(false);
  const hasSeries = collection.videos.length > 1;
  const [directory, setDirectory] = useState<"series" | "chapters">(hasSeries ? "series" : "chapters");
  const index = collection.videos.findIndex((item) => item.id === video.id);
  const previous = collection.videos[index - 1];
  const next = collection.videos[index + 1];

  useEffect(() => {
    const player = playerRef.current;
    player?.scrollIntoView({ block: "nearest" });
    // Selection and route changes unmount this keyed player. Pause explicitly so
    // detached media cannot keep speaking while the next video loads.
    return () => player?.pause();
  }, []);

  function applyPendingSeek(player: HTMLVideoElement) {
    if (pendingSeek.current === null || player.readyState < HTMLMediaElement.HAVE_METADATA) return;
    const target = pendingSeek.current;
    player.currentTime = Number.isFinite(player.duration)
      ? Math.min(target, Math.max(0, player.duration - 0.01))
      : target;
    pendingSeek.current = null;
  }

  function playChapter(startSeconds: number) {
    const player = playerRef.current;
    if (!player || mediaFailed) return;
    player.scrollIntoView({
      block: "nearest",
      behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth",
    });
    pendingSeek.current = startSeconds;
    setPlayBlocked(false);
    applyPendingSeek(player);
    // Keep play() in the user gesture, even before metadata exists. The pending
    // seek is applied by loadedmetadata while this playback request is loading.
    void player.play().catch((error: unknown) => {
      if (!player.isConnected || (error instanceof DOMException && error.name === "AbortError")) return;
      if (!player.error) setPlayBlocked(true);
    });
  }

  function retry() {
    pendingSeek.current = null;
    failedSources.current.clear();
    setMediaFailed(video.sources.length === 0);
    setPlayBlocked(false);
    setCaptionFailed(false);
    playerRef.current?.load();
  }

  return (
    <section aria-label={t("videos.player")} data-testid="video-watch">
      <FlowHeader
        onBack={onBack}
        title={collection.title[locale]}
        end={
          <div className="v2-row">
            <Tag tone={video.consoleVersion === "v2" ? "blue" : "gray"}>
              {t(`videos.version.${video.consoleVersion}`)}
            </Tag>
            {hasSeries && <Tag tone="blue">{t("videos.episodePosition", { index: index + 1, count: collection.videos.length })}</Tag>}
          </div>
        }
      />
      <div className="v2-videos-layout">
        <Card flush testId="video-player-panel">
          <video
            ref={playerRef}
            className="v2-videos-player"
            controls
            playsInline
            preload="metadata"
            crossOrigin="anonymous"
            poster={video.posterUrl}
            aria-label={video.title[locale]}
            data-testid="video-player"
            onLoadedMetadata={({ currentTarget }) => {
              // Burned-in subtitles already exist. Keep optional CC off initially.
              for (const track of currentTarget.textTracks) track.mode = "disabled";
              applyPendingSeek(currentTarget);
            }}
            onPlay={() => setPlayBlocked(false)}
            onError={(event) => {
              // Source failures can fall back; a caption failure is nonfatal.
              if (event.target === event.currentTarget) setMediaFailed(true);
            }}
          >
            {video.sources.map((source) => (
              <source
                key={source.url}
                src={source.url}
                type={source.type}
                onError={() => {
                  failedSources.current.add(source.url);
                  if (failedSources.current.size === video.sources.length) setMediaFailed(true);
                }}
              />
            ))}
            {video.captions.map((caption) => (
              <track
                key={caption.url}
                kind="subtitles"
                src={caption.url}
                srcLang={caption.language}
                label={caption.label}
                onError={() => setCaptionFailed(true)}
              />
            ))}
            {t("videos.unsupported")}
          </video>
          <div className="v2-videos-details">
            {mediaFailed && (
              <Alert tone="error" action={<Button size="sm" onClick={retry}>{t("videos.retry")}</Button>}>
                <span data-testid="video-load-error">
                  <b>{t("videos.loadFailed")}</b> {t("videos.loadFailedHint")}
                </span>
              </Alert>
            )}
            {playBlocked && !mediaFailed && <Alert tone="warn">{t("videos.playBlocked")}</Alert>}
            {captionFailed && <Alert>{t("videos.captionFailed")}</Alert>}
            <h2 className="v2-videos-title">{video.title[locale]}</h2>
            <div className="v2-videos-meta">
              <span>{t("videos.duration", { duration: videoTimestamp(video.durationSeconds) })}</span>
              <span>{t("videos.published", { date: video.publishedAt.slice(0, 10) })}</span>
            </div>
            <details className="v2-videos-summary">
              <summary>{t("videos.summary")}</summary>
              <p>{video.description[locale]}</p>
            </details>
            {hasSeries && (
              <nav className="v2-videos-pagination" aria-label={t("videos.series")}>
                {previous ? (
                  <button type="button" onClick={() => onSelect(previous.id)} data-testid="video-previous">
                    <span>
                      <ChevronLeft size={14} aria-hidden="true" />
                      {t("videos.previous")}
                    </span>
                    <strong>{previous.title[locale]}</strong>
                  </button>
                ) : (
                  <span />
                )}
                {next && (
                  <button type="button" className="next" onClick={() => onSelect(next.id)} data-testid="video-next">
                    <span>
                      {t("videos.next")}
                      <ChevronRight size={14} aria-hidden="true" />
                    </span>
                    <strong>{next.title[locale]}</strong>
                  </button>
                )}
              </nav>
            )}
          </div>
        </Card>

        <Card
          flush
          title={collection.title[locale]}
          sub={t("videos.episodeCount", { count: collection.videos.length })}
          testId="video-directory"
        >
          <div className="v2-videos-dir-head">
            {hasSeries ? (
              <Segmented
                value={directory}
                onChange={setDirectory}
                options={[
                  { value: "series", label: t("videos.series") },
                  { value: "chapters", label: t("videos.episodeChapters") },
                ]}
              />
            ) : (
              <span className="v2-videos-dir-title">{t("videos.episodeChapters")}</span>
            )}
          </div>
          {hasSeries && directory === "series" && (
            <ol className="v2-videos-scroll v2-videos-episodes" aria-label={t("videos.series")}>
              {collection.videos.map((item, i) => {
                const on = item.id === video.id;
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
          )}
          {directory === "chapters" && (
            <div className="v2-videos-scroll" role="region" aria-label={t("videos.episodeChapters")}>
              <p className="v2-videos-help">{t("videos.chapterHint")}</p>
              <ol className="v2-videos-chapters">
                {video.chapters.map((chapter) => (
                  <li key={chapter.startSeconds}>
                    <button type="button" disabled={mediaFailed} onClick={() => playChapter(chapter.startSeconds)}>
                      <span className="time">{videoTimestamp(chapter.startSeconds)}</span>
                      <span>{chapter.title[locale]}</span>
                    </button>
                  </li>
                ))}
              </ol>
            </div>
          )}
          <div className="v2-videos-dir-foot">
            <LinkButton onClick={onBack}>{t("videos.changeModule")}</LinkButton>
          </div>
        </Card>
      </div>
    </section>
  );
}
