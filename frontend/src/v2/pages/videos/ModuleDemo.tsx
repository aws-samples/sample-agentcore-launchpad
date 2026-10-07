import { PlayCircle } from "lucide-react";
import { createContext, type ReactNode, useContext, useEffect, useRef, useState } from "react";
import { useTranslation } from "react-i18next";
import { useLocation, useNavigate } from "react-router-dom";

import { useVideoCatalog } from "../../../lib/useVideoCatalog";
import {
  type LibraryVideo,
  moduleVideoCollection,
  type VideoCatalog,
  type VideoCollection,
  type VideoLocale,
} from "../../../lib/videos";
import { PageHeaderAsideContext } from "../../hooks";
import { activeNavItem } from "../../nav";
import { Button, Drawer, Segmented } from "../../ui";
import { VideoChapterList, VideoEpisodeList, VideoSummary } from "./playback";
import { useVideoPlayback } from "./useVideoPlayback";
import "./videos.css";

const LIBRARY_PATH = "/v2/videos";
const MANAGEMENT_PATH = "/v2/video-management";

const CatalogContext = createContext<VideoCatalog | null>(null);

/**
 * Per-module demo videos: the shell loads the published catalog once and every
 * PageHeader gets a 演示视频 button when its sidebar module has V2 recordings.
 * A failed load just shows no button.
 */
export function ModuleDemoProvider({ children }: { children: ReactNode }) {
  const { data, reload } = useVideoCatalog();
  const { pathname } = useLocation();
  const previous = useRef(pathname);
  useEffect(() => {
    // leaving video management: pick up what the admin just published or withdrew
    if (previous.current === MANAGEMENT_PATH && pathname !== MANAGEMENT_PATH) reload();
    previous.current = pathname;
  }, [pathname, reload]);
  return (
    <CatalogContext.Provider value={data}>
      <PageHeaderAsideContext.Provider value={ModuleDemoButton}>{children}</PageHeaderAsideContext.Provider>
    </CatalogContext.Provider>
  );
}

function ModuleDemoButton() {
  const { t } = useTranslation();
  const catalog = useContext(CatalogContext);
  const { pathname, search } = useLocation();
  const [open, setOpen] = useState(false);
  const item = activeNavItem(pathname, search);
  // the library itself is where every video already is
  const collection = catalog && item && item.to !== LIBRARY_PATH
    ? moduleVideoCollection(catalog, item.to)
    : null;
  if (!collection) return null;
  const count = collection.videos.length;
  return (
    <>
      <Button size="sm" title={t("videos.moduleDemoHint")} onClick={() => setOpen(true)} testId="v2-module-demo">
        <PlayCircle size={14} aria-hidden="true" />
        {count > 1 ? `${t("videos.moduleDemo")} · ${count}` : t("videos.moduleDemo")}
      </Button>
      {open && <ModuleDemoDrawer collection={collection} onClose={() => setOpen(false)} />}
    </>
  );
}

function ModuleDemoDrawer({ collection, onClose }: { collection: VideoCollection; onClose: () => void }) {
  const { t, i18n } = useTranslation();
  const navigate = useNavigate();
  const locale: VideoLocale = i18n.resolvedLanguage?.startsWith("zh") ? "zh-CN" : "en";
  const [currentId, setCurrentId] = useState(collection.videos[0].id);
  const video = collection.videos.find((item) => item.id === currentId) ?? collection.videos[0];
  const openInLibrary = () =>
    navigate(`${LIBRARY_PATH}?${new URLSearchParams({ view: "watch", video: video.id, version: "v2" })}`);
  return (
    <Drawer
      wide
      open
      onClose={onClose}
      title={t("videos.moduleDemoTitle", { module: collection.title[locale] })}
      footer={<Button onClick={openInLibrary} testId="v2-module-demo-library">{t("videos.openInLibrary")}</Button>}
      testId="v2-module-demo-drawer"
    >
      <DemoPlayback
        key={video.id}
        video={video}
        videos={collection.videos}
        locale={locale}
        onSelect={setCurrentId}
      />
    </Drawer>
  );
}

function DemoPlayback({
  video,
  videos,
  locale,
  onSelect,
}: {
  video: LibraryVideo;
  videos: LibraryVideo[];
  locale: VideoLocale;
  onSelect: (id: string) => void;
}) {
  const { t } = useTranslation();
  const { media, alerts, mediaFailed, playChapter } = useVideoPlayback(video, locale);
  const hasSeries = videos.length > 1;
  const [directory, setDirectory] = useState<"series" | "chapters">(hasSeries ? "series" : "chapters");
  return (
    <section className="v2-videos-demo" aria-label={t("videos.player")} data-testid="video-watch">
      {media}
      <div className="v2-videos-demo-details">
        {alerts}
        <VideoSummary video={video} locale={locale} />
      </div>
      <div className="v2-videos-demo-dir">
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
        <VideoEpisodeList videos={videos} currentId={video.id} locale={locale} onSelect={onSelect} />
      )}
      {directory === "chapters" && (
        <VideoChapterList video={video} locale={locale} disabled={mediaFailed} onSeek={playChapter} />
      )}
    </section>
  );
}
