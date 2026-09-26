import { useEffect, useMemo, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { Link, useSearchParams } from "react-router-dom";

import { Btn } from "../components/Btn";
import { Panel } from "../components/Panel";
import { ViewHead } from "../components/ViewHead";
import { useVideoCatalog } from "../lib/useVideoCatalog";
import {
  videoCollections as buildCollections, videoTimestamp,
  type LibraryVideo, type VideoCatalog, type VideoCollection, type VideoLocale,
} from "../lib/videos";
import "./videos.css";

const EMPTY_CATALOG: VideoCatalog = { schemaVersion: 2, categories: [], collections: [], videos: [] };

function videoLink(params: URLSearchParams, id?: string) {
  const next = new URLSearchParams(params);
  if (id) next.set("video", id);
  else next.delete("video");
  return { pathname: "/videos", search: next.toString() ? `?${next}` : "" };
}

function VideoPlayer({ video, collection, locale, params }: {
  video: LibraryVideo;
  collection: VideoCollection;
  locale: VideoLocale;
  params: URLSearchParams;
}) {
  const { t } = useTranslation();
  const playerRef = useRef<HTMLVideoElement>(null);
  const headingRef = useRef<HTMLHeadingElement>(null);
  const pendingSeek = useRef<number | null>(null);
  const failedSources = useRef(new Set<string>());
  const [mediaFailed, setMediaFailed] = useState(video.sources.length === 0);
  const [playBlocked, setPlayBlocked] = useState(false);
  const [captionFailed, setCaptionFailed] = useState(false);
  const hasSeries = collection.videos.length > 1;
  const [directory, setDirectory] = useState<"series" | "chapters">(
    hasSeries ? "series" : "chapters",
  );
  const episodeIndex = collection.videos.findIndex((item) => item.id === video.id);
  const previous = collection.videos[episodeIndex - 1];
  const next = collection.videos[episodeIndex + 1];

  useEffect(() => {
    headingRef.current?.focus({ preventScroll: true });
    headingRef.current?.scrollIntoView({ block: "nearest" });
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
    <section aria-label={t("videos.player")} data-testid="video-watch">
      <Link className="videos-back" to={videoLink(params)} data-testid="video-back">
        ← {t("videos.backToLibrary")}
      </Link>
      <div className="videos-watch-head">
        <h2 ref={headingRef} tabIndex={-1}>{collection.title[locale]}</h2>
        {hasSeries ? (
          <span>{t("videos.episodePosition", {
            index: episodeIndex + 1, count: collection.videos.length,
          })}</span>
        ) : null}
      </div>
      <div className="videos-layout">
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
            <h3 className="videos-title">{video.title[locale]}</h3>
            <div className="videos-meta">
              <span>{t("videos.duration", { duration: videoTimestamp(video.durationSeconds) })}</span>
              <span>{t("videos.published", { date: video.publishedAt.slice(0, 10) })}</span>
            </div>
            <details className="videos-summary">
              <summary>{t("videos.summary")}</summary>
              <p className="videos-description">{video.description[locale]}</p>
            </details>
            {hasSeries ? (
              <nav className="videos-pagination" aria-label={t("videos.series")}>
                {previous ? (
                  <Link to={videoLink(params, previous.id)} data-testid="video-previous">
                    <span>← {t("videos.previous")}</span>
                    <strong>{previous.title[locale]}</strong>
                  </Link>
                ) : <span />}
                {next ? (
                  <Link to={videoLink(params, next.id)} data-testid="video-next">
                    <span>{t("videos.next")} →</span>
                    <strong>{next.title[locale]}</strong>
                  </Link>
                ) : null}
              </nav>
            ) : null}
          </div>
        </Panel>
        <Panel
          title={collection.title[locale]}
          sub={t("videos.episodeCount", { count: collection.videos.length })}
          pad={false}
          className="videos-playlist"
          data-testid="video-directory"
        >
          {hasSeries ? (
            <div className="videos-directory-tabs" role="tablist" aria-label={t("videos.directory")}>
              {(["series", "chapters"] as const).map((item) => (
                <button
                  key={item}
                  id={`videos-${item}-tab`}
                  type="button"
                  role="tab"
                  aria-selected={directory === item}
                  aria-controls={`videos-${item}-panel`}
                  tabIndex={directory === item ? 0 : -1}
                  onClick={() => setDirectory(item)}
                  onKeyDown={(event) => {
                    if (!["ArrowLeft", "ArrowRight", "Home", "End"].includes(event.key)) return;
                    event.preventDefault();
                    const target = event.key === "Home" ? "series"
                      : event.key === "End" ? "chapters"
                        : item === "series" ? "chapters" : "series";
                    setDirectory(target);
                    document.getElementById(`videos-${target}-tab`)?.focus();
                  }}
                >
                  {t(item === "series" ? "videos.series" : "videos.episodeChapters")}
                </button>
              ))}
            </div>
          ) : <h3 className="videos-directory-title">{t("videos.episodeChapters")}</h3>}
          {hasSeries ? (
            <div
              id="videos-series-panel"
              role="tabpanel"
              aria-labelledby="videos-series-tab"
              hidden={directory !== "series"}
              className="videos-directory-scroll"
              tabIndex={0}
            >
              <ol className="videos-episode-list">
                {collection.videos.map((item, index) => (
                  <li key={item.id}>
                    <Link
                      className={`videos-episode${item.id === video.id ? " selected" : ""}`}
                      to={videoLink(params, item.id)}
                      aria-current={item.id === video.id ? "true" : undefined}
                      data-testid={`video-entry-${item.id}`}
                    >
                      {item.posterUrl && <img src={item.posterUrl} alt="" loading="lazy" />}
                      <span>
                        <strong>{item.title[locale]}</strong>
                        <small>
                          {String(index + 1).padStart(2, "0")} · {videoTimestamp(item.durationSeconds)}
                          {item.id === video.id ? ` · ${t("videos.selected")}` : ""}
                        </small>
                      </span>
                    </Link>
                  </li>
                ))}
              </ol>
            </div>
          ) : null}
          <div
            id="videos-chapters-panel"
            role={hasSeries ? "tabpanel" : "region"}
            aria-labelledby={hasSeries ? "videos-chapters-tab" : undefined}
            aria-label={hasSeries ? undefined : t("videos.episodeChapters")}
            hidden={directory !== "chapters"}
            className="videos-directory-scroll"
            tabIndex={0}
          >
            <p className="videos-help">{t("videos.chapterHint")}</p>
            <ol className="videos-chapter-list">
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
          </div>
          <Link className="videos-directory-back" to={videoLink(params)}>
            {t("videos.changeModule")}
          </Link>
        </Panel>
      </div>
    </section>
  );
}

export function Videos() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const loaded = useVideoCatalog();
  const videoCatalog = loaded.data ?? EMPTY_CATALOG;
  const videoCollections = useMemo(() => buildCollections(videoCatalog), [videoCatalog]);
  const locale: VideoLocale = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const videos = videoCatalog.videos;
  const requestedId = params.get("video");
  const selected = videos.find((video) => video.id === requestedId);
  const collection = videoCollections.find((item) => item.videos.some((v) => v.id === selected?.id));
  const query = params.get("q") ?? "";
  const search = query.trim().toLocaleLowerCase(locale);
  const category = videoCatalog.categories.some((item) => item.id === params.get("category"))
    ? params.get("category") : null;
  const section = videoCollections.some((item) => item.id === params.get("section") &&
    (!category || item.categoryId === category)) ? params.get("section") : null;
  const collections = videoCollections.filter((item) =>
    (!category || item.categoryId === category) && (!section || item.id === section));
  const results = collections.flatMap((item) => (
    search
      ? item.videos
        .filter((video) => `${item.title[locale]} ${video.title[locale]}`.toLocaleLowerCase(locale).includes(search))
        .map((video) => ({
          id: video.id, title: video.title[locale], description: video.description[locale], videos: [video],
        }))
      : [{
        id: item.id, title: item.title[locale],
        description: item.description[locale], videos: item.videos,
      }]
  ));

  function filter(name: "q" | "category" | "section", value: string) {
    setParams((current) => {
      const next = new URLSearchParams(current);
      next.delete("video");
      if (name === "category") next.delete("section");
      if (value) next.set(name, value);
      else next.delete(name);
      return next;
    }, { replace: true });
  }

  function clearFilters() {
    setParams((current) => {
      const next = new URLSearchParams(current);
      for (const name of ["q", "category", "section", "video"]) next.delete(name);
      return next;
    }, { replace: true });
  }

  if (!loaded.data) {
    return (
      <>
        <ViewHead kicker={t("videos.kicker")} title={t("videos.title")} description={t("videos.description")} />
        <Panel title={t("videos.library")}>
          <p>{loaded.loading ? t("common.loading") : loaded.error ?? t("videos.loadFailed")}</p>
          {!loaded.loading && <Btn onClick={loaded.reload}>{t("videos.retry")}</Btn>}
        </Panel>
      </>
    );
  }

  return (
    <>
      <ViewHead
        kicker={t("videos.kicker")}
        title={t("videos.title")}
        description={t("videos.description")}
        meta={t("videos.count", { count: videos.length })}
      />
      {loaded.error && (
        <p className="videos-notice" role="alert">
          {loaded.error} <Btn onClick={loaded.reload}>{t("videos.retry")}</Btn>
        </p>
      )}
      {videos.length === 0 ? (
        <Panel title={t("videos.library")}>
          <div className="empty" data-testid="videos-empty">
            <p>{t("videos.empty")}</p>
            <p>{t("videos.emptyHint")}</p>
          </div>
        </Panel>
      ) : selected && collection ? (
        <VideoPlayer
          key={selected.id}
          video={selected}
          collection={collection}
          locale={locale}
          params={params}
        />
      ) : (
        <section aria-label={t("videos.libraryTitle")} data-testid="video-library">
          {requestedId !== null ? (
            <p className="videos-notice" role="status" data-testid="video-invalid-link">
              {t("videos.invalidLink")}
            </p>
          ) : null}
          <div className="videos-library-head">
            <div>
              <h2>{t("videos.libraryTitle")}</h2>
              <p>{t("videos.browse")}</p>
            </div>
            <input
              className="videos-search"
              type="search"
              value={query}
              placeholder={t("videos.search")}
              aria-label={t("videos.search")}
              onChange={(event) => filter("q", event.target.value)}
              data-testid="video-search"
            />
          </div>
          <div className="videos-filters" role="group" aria-label={t("videos.categories")}>
            <button type="button" aria-pressed={!category} onClick={() => filter("category", "")}>
              {t("videos.all")} <span>{videos.length}</span>
            </button>
            {videoCatalog.categories.map((item) => (
              <button
                key={item.id}
                type="button"
                aria-pressed={category === item.id}
                onClick={() => filter("category", item.id)}
                data-testid={`video-category-${item.id}`}
              >
                {item.title[locale]}
                <span>{videoCollections.filter((c) => c.categoryId === item.id)
                  .reduce((count, c) => count + c.videos.length, 0)}</span>
              </button>
            ))}
          </div>
          <label className="videos-section-filter">
            <span>{t("videoManage.section")}</span>
            <select
              value={section ?? ""}
              disabled={!category}
              onChange={(event) => filter("section", event.target.value)}
              data-testid="video-section-filter"
            >
              <option value="">{t("videos.all")}</option>
              {videoCollections.filter((item) => item.categoryId === category).map((item) => (
                <option key={item.id} value={item.id}>{item.title[locale]}</option>
              ))}
            </select>
          </label>
          <p className="videos-result-count" role="status">
            {search
              ? `${t("videos.searchResults")} · ${t("videos.count", { count: results.length })}`
              : t("videos.collectionCount", { count: results.length })}
          </p>
          {results.length > 0 ? (
            <ul className="videos-grid">
              {results.map((item) => {
                const first = item.videos[0];
                const duration = item.videos.reduce((total, video) => total + video.durationSeconds, 0);
                return (
                  <li key={item.id}>
                    <Link
                      className="videos-card"
                      to={videoLink(params, first.id)}
                      data-testid={`video-card-${item.id}`}
                    >
                      <div className="videos-thumbnail">
                        {first.posterUrl && <img src={first.posterUrl} alt="" loading="lazy" />}
                        <span>
                          {item.videos.length > 1
                            ? `${t("videos.episodeCount", { count: item.videos.length })} · ` : ""}
                          {videoTimestamp(duration)}
                        </span>
                      </div>
                      <div className="videos-card-text">
                        <h3>{item.title}</h3>
                        <p>{item.description}</p>
                      </div>
                    </Link>
                  </li>
                );
              })}
            </ul>
          ) : (
            <div className="empty" data-testid="videos-no-results">
              <p>{t("videos.noResults")}</p>
              <p>{t("videos.noResultsHint")}</p>
              <Btn onClick={clearFilters}>{t("videos.clearFilters")}</Btn>
            </div>
          )}
        </section>
      )}
    </>
  );
}
