import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";

import type { LibraryVideo, VideoLocale } from "../../../lib/videos";
import { Alert, Button } from "../../ui";

/**
 * One video's native `<video>` with source fallback, optional captions (off by
 * default: subtitles are burned in) and chapter seek — shared by the library's
 * watch page and the per-module demo drawer. Key the caller on the video id so
 * switching episodes remounts it; unmounting pauses the media.
 */
export function useVideoPlayback(video: LibraryVideo, locale: VideoLocale) {
  const { t } = useTranslation();
  const playerRef = useRef<HTMLVideoElement>(null);
  const pendingSeek = useRef<number | null>(null);
  const failedSources = useRef(new Set<string>());
  const [mediaFailed, setMediaFailed] = useState(video.sources.length === 0);
  const [playBlocked, setPlayBlocked] = useState(false);
  const [captionFailed, setCaptionFailed] = useState(false);

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

  const media = (
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
  );

  const alerts = (
    <>
      {mediaFailed && (
        <Alert tone="error" action={<Button size="sm" onClick={retry}>{t("videos.retry")}</Button>}>
          <span data-testid="video-load-error">
            <b>{t("videos.loadFailed")}</b> {t("videos.loadFailedHint")}
          </span>
        </Alert>
      )}
      {playBlocked && !mediaFailed && <Alert tone="warn">{t("videos.playBlocked")}</Alert>}
      {captionFailed && <Alert>{t("videos.captionFailed")}</Alert>}
    </>
  );

  return { media, alerts, mediaFailed, playChapter };
}
