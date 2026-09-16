import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { Btn } from "../components/Btn";
import { Panel } from "../components/Panel";
import { ViewHead } from "../components/ViewHead";
import { videoCatalog, videoTimestamp, type LibraryVideo, type VideoLocale } from "../lib/videos";
import "./videos.css";

function VideoPlayer({ video, locale }: { video: LibraryVideo; locale: VideoLocale }) {
  const { t } = useTranslation();
  const playerRef = useRef<HTMLVideoElement>(null);
  const pendingSeek = useRef<number | null>(null);
  const failedSources = useRef(new Set<string>());
  const [mediaFailed, setMediaFailed] = useState(video.sources.length === 0);
  const [playBlocked, setPlayBlocked] = useState(false);
  const [captionFailed, setCaptionFailed] = useState(false);

  useEffect(() => {
    const player = playerRef.current;
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
      if (!player.isConnected || (error instanceof DOMException && error.name === "AbortError")) {
        return;
      }
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
    <Panel title={t("videos.player")} pad={false} brk data-testid="video-player-panel">
      <video
        ref={playerRef}
        className="videos-player"
        controls
        playsInline
        preload="metadata"
        crossOrigin="anonymous"
        poster={video.posterUrl}
        aria-label={video.title[locale]}
        data-testid="video-player"
        onLoadedMetadata={({ currentTarget }) => {
          // Burned-in Chinese subtitles already exist. Native caption tracks are
          // available through CC, but start off to avoid duplicate subtitles.
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
      <div className="videos-details">
        {mediaFailed ? (
          <div className="videos-error" role="alert" data-testid="video-load-error">
            <div>
              <strong>{t("videos.loadFailed")}</strong>
              <p>{t("videos.loadFailedHint")}</p>
            </div>
            <Btn type="button" onClick={retry}>{t("videos.retry")}</Btn>
          </div>
        ) : null}
        {playBlocked && !mediaFailed ? (
          <p className="videos-notice" role="status">{t("videos.playBlocked")}</p>
        ) : null}
        {captionFailed ? (
          <p className="videos-notice" role="status">{t("videos.captionFailed")}</p>
        ) : null}
        <h2 className="videos-title">{video.title[locale]}</h2>
        <div className="videos-meta">
          <span>{t("videos.duration", { duration: videoTimestamp(video.durationSeconds) })}</span>
          <span>{t("videos.published", { date: video.publishedAt.slice(0, 10) })}</span>
        </div>
        <p className="videos-description">{video.description[locale]}</p>
        {video.chapters.length > 0 ? (
          <section className="videos-chapters" aria-label={t("videos.chapters")}>
            <h3>{t("videos.chapters")}</h3>
            <p className="videos-help">{t("videos.chapterHint")}</p>
            <ol>
              {video.chapters.map((chapter) => (
                <li key={chapter.startSeconds}>
                  <button
                    type="button"
                    className="videos-chapter"
                    disabled={mediaFailed}
                    onClick={() => playChapter(chapter.startSeconds)}
                  >
                    <span className="videos-time">{videoTimestamp(chapter.startSeconds)}</span>
                    <span>{chapter.title[locale]}</span>
                  </button>
                </li>
              ))}
            </ol>
          </section>
        ) : null}
      </div>
    </Panel>
  );
}

export function Videos() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const locale: VideoLocale = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const videos = videoCatalog.videos;
  const requestedId = params.get("video");
  const selected = videos.find((video) => video.id === requestedId) ?? videos[0];
  const selectedId = selected?.id;

  useEffect(() => {
    if (!selectedId || requestedId === selectedId) return;
    // Canonicalize missing/stale links without adding a redundant history entry.
    setParams((current) => {
      const next = new URLSearchParams(current);
      next.set("video", selectedId);
      return next;
    }, { replace: true });
  }, [requestedId, selectedId, setParams]);

  return (
    <>
      <ViewHead
        kicker={t("videos.kicker")}
        title={t("videos.title")}
        description={t("videos.description")}
        meta={t("videos.count", { count: videos.length })}
      />
      {!selected ? (
        <Panel title={t("videos.library")}>
          <div className="empty" data-testid="videos-empty">
            <p>{t("videos.empty")}</p>
            <p>{t("videos.emptyHint")}</p>
          </div>
        </Panel>
      ) : (
        <div className="videos-layout">
          <VideoPlayer key={selected.id} video={selected} locale={locale} />
          <Panel title={t("videos.library")} pad={false} className="videos-library">
            <ul aria-label={t("videos.library")}>
              {videos.map((video) => {
                const next = new URLSearchParams(params);
                next.set("video", video.id);
                return (
                  <li key={video.id}>
                    <Link
                      className={`videos-entry${video.id === selected.id ? " selected" : ""}`}
                      to={{ pathname: "/videos", search: `?${next.toString()}` }}
                      aria-current={video.id === selected.id ? "true" : undefined}
                      data-testid={`video-entry-${video.id}`}
                    >
                      <div className="videos-thumbnail">
                        <img src={video.posterUrl} alt="" loading="lazy" />
                        <span>{videoTimestamp(video.durationSeconds)}</span>
                      </div>
                      <strong>{video.title[locale]}</strong>
                      <span className="videos-entry-description">{video.description[locale]}</span>
                      {video.id === selected.id ? (
                        <span className="videos-selected">{t("videos.selected")}</span>
                      ) : null}
                    </Link>
                  </li>
                );
              })}
            </ul>
          </Panel>
        </div>
      )}
    </>
  );
}
