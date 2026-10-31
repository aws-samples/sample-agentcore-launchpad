import "./videos.css";

import { ArrowLeft, ChevronLeft, ChevronRight, Play, PlayCircle, Search } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useSearchParams } from "react-router-dom";

import { useVideoCatalog } from "../../lib/useVideoCatalog";
import {
  type LibraryVideo,
  parseVersionFilter,
  type VideoCatalog,
  type VideoCollection,
  videoCollections,
  videoCollectionsByVersion,
  type VideoLocale,
  videoTimestamp,
  type VideoVersionFilter,
} from "../../lib/videos";
import { Reasoned } from "../reason";
import { Btn, Chip, Empty, Filters, Notice, PageHead, Panel, Skeleton } from "../ui";
import { Select } from "../select";

/* ── library ─────────────────────────────────────────────────────────────── */

function Library({
  catalog,
  collections: allCollections,
  locale,
  version,
  query,
  category,
  section,
  invalidLink,
  onFilter,
  onClear,
  onWatch,
}: {
  catalog: VideoCatalog;
  collections: VideoCollection[];
  locale: VideoLocale;
  version: VideoVersionFilter;
  query: string;
  category: string | null;
  section: string | null;
  invalidLink: boolean;
  onFilter: (name: "q" | "category" | "section" | "version", value: string) => void;
  onClear: () => void;
  onWatch: (id: string) => void;
}) {
  const { t } = useTranslation();
  const videos = catalog.videos;
  const versionCount = (value: VideoVersionFilter) =>
    value === "all" ? videos.length : videos.filter((video) => video.consoleVersion === value).length;
  const search = query.trim().toLocaleLowerCase(locale);
  const collections = allCollections.filter((item) => (!category || item.categoryId === category) && (!section || item.id === section));
  // a search flattens collections into the matching episodes
  const results = collections.flatMap((item) =>
    search
      ? item.videos
          .filter((video) => `${item.title[locale]} ${video.title[locale]}`.toLocaleLowerCase(locale).includes(search))
          .map((video) => ({ id: video.id, title: video.title[locale], description: video.description[locale], videos: [video] }))
      : [{ id: item.id, title: item.title[locale], description: item.description[locale], videos: item.videos }],
  );
  const countFor = (id: string) => allCollections.filter((c) => c.categoryId === id).reduce((n, c) => n + c.videos.length, 0);
  const sectionOptions = allCollections.filter(
    (item, index) => item.categoryId === category && allCollections.findIndex((c) => c.id === item.id) === index,
  );

  if (videos.length === 0) {
    return (
      <Panel>
        <Empty title={t("videos.empty")}>{t("videos.emptyHint")}</Empty>
      </Panel>
    );
  }

  return (
    <section aria-label={t("videos.libraryTitle")} style={{ display: "grid", gap: 14 }}>
      {invalidLink && <Notice s="wait">{t("videos.invalidLink")}</Notice>}
      <div className="v3-vid-bar">
        <Filters
          value={version}
          onChange={(v) => onFilter("version", v)}
          options={[
            { value: "v3", label: t("videos.version.v3"), count: versionCount("v3") },
            { value: "v2", label: t("videos.version.v2"), count: versionCount("v2") },
            { value: "classic", label: t("videos.version.classic"), count: versionCount("classic") },
            { value: "all", label: t("videos.versionAll"), count: versionCount("all") },
          ]}
        />
        <span className="sep" aria-hidden="true" />
        <Filters
          value={category ?? ""}
          onChange={(v) => onFilter("category", v)}
          options={[
            { value: "", label: t("videos.all"), count: versionCount(version) },
            ...catalog.categories.map((item) => ({ value: item.id, label: item.title[locale], count: countFor(item.id) })),
          ]}
        />
      </div>
      <div className="v3-vid-bar">
        <label className="v3-vid-section">
          <span>{t("videoManage.section")}</span>
          <Reasoned disabledReason={t("story.reason.pickArea")}>
            <Select className="v3-select" value={section ?? ""} disabled={!category} onChange={(e) => onFilter("section", e.target.value)}>
              <option value="">{t("videos.all")}</option>
              {sectionOptions.map((item) => <option key={item.id} value={item.id}>{item.title[locale]}</option>)}
            </Select>
          </Reasoned>
        </label>
        <div style={{ marginLeft: "auto", position: "relative", flex: "1 1 200px", maxWidth: 300 }}>
          <Search size={14} style={{ position: "absolute", left: 12, top: 11, color: "var(--v3-text-3)" }} aria-hidden="true" />
          <input className="v3-input" style={{ paddingLeft: 34 }} value={query} onChange={(e) => onFilter("q", e.target.value)}
            placeholder={t("videos.search")} aria-label={t("videos.search")} />
        </div>
        <span className="mono" role="status" style={{ color: "var(--v3-text-3)", fontSize: 12 }}>
          {search ? `${t("videos.searchResults")} · ${t("videos.count", { count: results.length })}` : t("videos.collectionCount", { count: results.length })}
        </span>
      </div>

      {results.length === 0 ? (
        <Panel>
          <Empty title={t("videos.noResults")}>
            {t("videos.noResultsHint")}
            <div style={{ marginTop: 12 }}><Btn onClick={onClear}>{t("videos.clearFilters")}</Btn></div>
          </Empty>
        </Panel>
      ) : (
        <ul className="v3-vid-grid">
          {results.map((item) => {
            const first = item.videos[0];
            const duration = item.videos.reduce((total, video) => total + video.durationSeconds, 0);
            return (
              <li key={`${item.id}-${first.consoleVersion}`}>
                <button type="button" className="v3-vid-card" onClick={() => onWatch(first.id)}>
                  <span className="thumb">
                    {first.posterUrl && <img src={first.posterUrl} alt="" loading="lazy" />}
                    <PlayCircle className="play" size={40} aria-hidden="true" />
                    <span className="len">{videoTimestamp(duration)}</span>
                  </span>
                  <span className="text">
                    <b>{item.title}</b>
                    <small>{item.description}</small>
                    <span className="tags">
                      <Chip s={first.consoleVersion === "v3" ? "ok" : first.consoleVersion === "v2" ? "info" : undefined}>{t(`videos.version.${first.consoleVersion}`)}</Chip>
                      {item.videos.length > 1 && <Chip>{t("videos.episodeCount", { count: item.videos.length })}</Chip>}
                    </span>
                  </span>
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </section>
  );
}

/* ── player ──────────────────────────────────────────────────────────────── */

function Player({
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
    // the player is keyed on the video: pause on unmount so detached media stops speaking
    return () => player?.pause();
  }, []);

  function applyPendingSeek(player: HTMLVideoElement) {
    if (pendingSeek.current === null || player.readyState < HTMLMediaElement.HAVE_METADATA) return;
    const target = pendingSeek.current;
    player.currentTime = Number.isFinite(player.duration) ? Math.min(target, Math.max(0, player.duration - 0.01)) : target;
    pendingSeek.current = null;
  }

  function playChapter(startSeconds: number) {
    const player = playerRef.current;
    if (!player || mediaFailed) return;
    player.scrollIntoView({ block: "nearest", behavior: window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth" });
    pendingSeek.current = startSeconds;
    setPlayBlocked(false);
    applyPendingSeek(player);
    // keep play() inside the user gesture; the pending seek lands on loadedmetadata
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
    <section aria-label={t("videos.player")} className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <div>
        <button type="button" className="v3-btn ghost sm" onClick={onBack}><ArrowLeft size={14} /> {t("videos.title")}</button>
      </div>
      <PageHead
        eyebrow={collection.title[locale]}
        title={video.title[locale]}
        sub={
          <span className="v3-vid-meta">
            <Chip s={video.consoleVersion === "v3" ? "ok" : video.consoleVersion === "v2" ? "info" : undefined}>{t(`videos.version.${video.consoleVersion}`)}</Chip>
            {hasSeries && <Chip>{t("videos.episodePosition", { index: index + 1, count: collection.videos.length })}</Chip>}
            <span>{t("videos.duration", { duration: videoTimestamp(video.durationSeconds) })}</span>
            <span>{t("videos.published", { date: video.publishedAt.slice(0, 10) })}</span>
          </span>
        }
      />
      <div className="v3-vid-layout">
        <Panel flush>
          <video
            ref={playerRef}
            className="v3-vid-player"
            controls
            playsInline
            preload="metadata"
            crossOrigin="anonymous"
            poster={video.posterUrl}
            aria-label={video.title[locale]}
            data-testid="video-player"
            onLoadedMetadata={({ currentTarget }) => {
              // subtitles are burned in: optional CC starts off
              for (const track of currentTarget.textTracks) track.mode = "disabled";
              applyPendingSeek(currentTarget);
            }}
            onPlay={() => setPlayBlocked(false)}
            onError={(event) => {
              if (event.target === event.currentTarget) setMediaFailed(true);
            }}
          >
            {video.sources.map((source) => (
              <source key={source.url} src={source.url} type={source.type}
                onError={() => {
                  failedSources.current.add(source.url);
                  if (failedSources.current.size === video.sources.length) setMediaFailed(true);
                }} />
            ))}
            {video.captions.map((caption) => (
              <track key={caption.url} kind="subtitles" src={caption.url} srcLang={caption.language} label={caption.label}
                onError={() => setCaptionFailed(true)} />
            ))}
            {t("videos.unsupported")}
          </video>
          <div className="v3-vid-details">
            {mediaFailed && (
              <Notice s="act">
                <b>{t("videos.loadFailed")}</b> {t("videos.loadFailedHint")}{" "}
                <Btn size="sm" onClick={retry}>{t("videos.retry")}</Btn>
              </Notice>
            )}
            {playBlocked && !mediaFailed && <Notice s="wait">{t("videos.playBlocked")}</Notice>}
            {captionFailed && <Notice>{t("videos.captionFailed")}</Notice>}
            <details className="v3-vid-summary">
              <summary>{t("videos.summary")}</summary>
              <p>{video.description[locale]}</p>
            </details>
            {hasSeries && (
              <nav className="v3-vid-pages" aria-label={t("videos.series")}>
                {previous ? (
                  <button type="button" onClick={() => onSelect(previous.id)}>
                    <span><ChevronLeft size={14} aria-hidden="true" />{t("videos.previous")}</span>
                    <b>{previous.title[locale]}</b>
                  </button>
                ) : <span />}
                {next && (
                  <button type="button" className="next" onClick={() => onSelect(next.id)}>
                    <span>{t("videos.next")}<ChevronRight size={14} aria-hidden="true" /></span>
                    <b>{next.title[locale]}</b>
                  </button>
                )}
              </nav>
            )}
          </div>
        </Panel>

        <Panel
          title={hasSeries ? (
            <Filters value={directory} onChange={setDirectory}
              options={[{ value: "series", label: t("videos.series") }, { value: "chapters", label: t("videos.episodeChapters") }]} />
          ) : t("videos.episodeChapters")}
          end={<span className="mono">{t("videos.episodeCount", { count: collection.videos.length })}</span>}
        >
          {hasSeries && directory === "series" && (
            <ol className="v3-vid-episodes" aria-label={t("videos.series")}>
              {collection.videos.map((item, i) => {
                const on = item.id === video.id;
                return (
                  <li key={item.id}>
                    <button type="button" className={on ? "on" : undefined} aria-current={on ? "true" : undefined} onClick={() => onSelect(item.id)}>
                      <span className="thumb">
                        {item.posterUrl && <img src={item.posterUrl} alt="" loading="lazy" />}
                        {on && <Play size={16} aria-hidden="true" />}
                      </span>
                      <span className="text">
                        <b>{item.title[locale]}</b>
                        <small>{String(i + 1).padStart(2, "0")} · {videoTimestamp(item.durationSeconds)}{on ? ` · ${t("videos.selected")}` : ""}</small>
                      </span>
                    </button>
                  </li>
                );
              })}
            </ol>
          )}
          {directory === "chapters" && (
            <div role="region" aria-label={t("videos.episodeChapters")}>
              <p className="v3-vid-help">{t("videos.chapterHint")}</p>
              <ol className="v3-vid-chapters">
                {video.chapters.map((chapter) => (
                  <li key={chapter.startSeconds}>
                    <Reasoned disabledReason={t("story.reason.videoFailed")}>
                      <button type="button" disabled={mediaFailed} onClick={() => playChapter(chapter.startSeconds)}>
                        <span className="time">{videoTimestamp(chapter.startSeconds)}</span>
                        <span>{chapter.title[locale]}</span>
                      </button>
                    </Reasoned>
                  </li>
                ))}
              </ol>
            </div>
          )}
        </Panel>
      </div>
    </section>
  );
}

/**
 * The published, hub-global video directory, for every signed-in user.
 * `?view=watch&video=<id>` opens the player; `?q=` / `?category=` / `?section=` /
 * `?version=` filter the library and survive watching, as in V2.
 */
export function V3Videos() {
  const { t, i18n } = useTranslation();
  const [params, setParams] = useSearchParams();
  const loaded = useVideoCatalog();
  const catalog = loaded.data;
  const locale: VideoLocale = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const requestedId = params.get("video");
  const collections = catalog ? videoCollections(catalog) : [];
  const selected = catalog?.videos.find((video) => video.id === requestedId);
  const rawVersion = params.get("version");
  const version: VideoVersionFilter = parseVersionFilter(rawVersion, "v2");
  const visible = videoCollectionsByVersion(collections, version);
  const collection =
    selected && videoCollectionsByVersion(collections, selected.consoleVersion).find((item) => item.videos.some((v) => v.id === selected.id));
  const rawCategory = params.get("category");
  const category = catalog?.categories.some((item) => item.id === rawCategory) ? rawCategory : null;
  const rawSection = params.get("section");
  const section = collections.some((item) => item.id === rawSection && (!category || item.categoryId === category)) ? rawSection : null;

  const withParams = (edit: (next: URLSearchParams) => void, replace = false) =>
    setParams(
      (current) => {
        const next = new URLSearchParams(current);
        edit(next);
        return next;
      },
      { replace },
    );
  const watch = (id: string) =>
    withParams((next) => {
      next.set("view", "watch");
      next.set("video", id);
      if (next.get("version") !== "all") {
        const video = catalog?.videos.find((item) => item.id === id);
        if (video) next.set("version", video.consoleVersion);
      }
    });
  const back = () =>
    withParams((next) => {
      next.delete("view");
      next.delete("video");
      if (next.get("version") !== "all" && selected) next.set("version", selected.consoleVersion);
    });

  if (!catalog) {
    return (
      <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
        <PageHead eyebrow={t("v3.videos.eyebrow")} title={t("videos.title")} sub={t("videos.description")} />
        {loaded.loading ? (
          <Panel><Skeleton rows={4} /></Panel>
        ) : (
          <Notice s="act">
            {loaded.error ?? t("videos.loadFailed")} <Btn size="sm" onClick={loaded.reload}>{t("v3.videos.retry")}</Btn>
          </Notice>
        )}
      </div>
    );
  }
  if (selected && collection) {
    return <Player key={selected.id} video={selected} collection={collection} locale={locale} onSelect={watch} onBack={back} />;
  }
  return (
    <div className="v3-reveal" style={{ display: "grid", gap: 16 }}>
      <PageHead
        eyebrow={t("v3.videos.eyebrow")}
        title={t("videos.title")}
        sub={t("videos.description")}
        end={
          <span className="mono" style={{ color: "var(--v3-text-3)" }}>
            {t("videos.count", {
              count: version === "all" ? catalog.videos.length : catalog.videos.filter((v) => v.consoleVersion === version).length,
            })}
          </span>
        }
      />
      {loaded.error && (
        <Notice s="act">{loaded.error} <Btn size="sm" onClick={loaded.reload}>{t("v3.videos.retry")}</Btn></Notice>
      )}
      <Library
        catalog={catalog}
        collections={visible}
        locale={locale}
        version={version}
        query={params.get("q") ?? ""}
        category={category}
        section={section}
        invalidLink={params.get("video") !== null}
        onFilter={(name, value) =>
          withParams((next) => {
            next.delete("view");
            next.delete("video");
            if (name === "category" || name === "version") next.delete("section");
            if (value) next.set(name, value);
            else next.delete(name);
          }, true)
        }
        onClear={() => withParams((next) => ["q", "category", "section", "version", "view", "video"].forEach((n) => next.delete(n)), true)}
        onWatch={watch}
      />
    </div>
  );
}
