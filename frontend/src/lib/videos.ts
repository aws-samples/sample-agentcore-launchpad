export type VideoLocale = "en" | "zh-CN";
export type VideoText = Record<VideoLocale, string>;
export type ConsoleVersion = "v3" | "v2" | "classic";
/** Every recorded console UI, newest first: the order filters and playlists list them in. */
export const CONSOLE_VERSIONS: readonly ConsoleVersion[] = ["v3", "v2", "classic"];
export type VideoVersionFilter = ConsoleVersion | "all";

export interface LibraryVideo {
  id: string;
  consoleVersion: ConsoleVersion;
  title: VideoText;
  description: VideoText;
  publishedAt: string;
  durationSeconds: number;
  language: string;
  posterUrl: string;
  sources: { url: string; type: string }[];
  captions: { url: string; language: string; label: string }[];
  chapters: { startSeconds: number; title: VideoText }[];
}

export interface VideoCatalog {
  schemaVersion: number;
  categories: { id: string; title: VideoText }[];
  collections: {
    id: string;
    categoryId: string;
    /** the module's V2 sidebar path (`/v2/agents`) */
    path: string;
    title: VideoText;
    description: VideoText;
    videoIds: string[];
  }[];
  videos: LibraryVideo[];
}

export interface VideoSection {
  id: string;
  categoryId: string;
  title: VideoText;
  path: string;
}

export interface VideoTaxonomy {
  schemaVersion: number;
  categories: VideoCatalog["categories"];
  sections: VideoSection[];
}

export interface VideoContent {
  category_id: string;
  section_id: string;
  console_version: ConsoleVersion;
  title: VideoText;
  description: VideoText;
  cdn_url: string;
  webm_url: string | null;
  poster_url: string | null;
  caption_url: string | null;
  duration_seconds: number;
  chapters: LibraryVideo["chapters"];
  sort_order: number;
}

export interface ManagedVideo {
  id: string;
  content: VideoContent;
  published_content: VideoContent | null;
  status: "draft" | "published";
  has_unpublished_changes: boolean;
  revision: number;
  created_by: string;
  updated_by: string;
  created_at: string;
  updated_at: string;
  published_at: string | null;
}

export interface ManagedVideoCatalog {
  taxonomy: VideoTaxonomy;
  videos: ManagedVideo[];
}

export interface VideoCollection {
  id: string;
  categoryId: string;
  path: string;
  title: VideoText;
  description: VideoText;
  videos: LibraryVideo[];
}

/** Backend publication already validates membership and ordering. */
export function videoCollections(catalog: VideoCatalog): VideoCollection[] {
  const byId = new Map(catalog.videos.map((video) => [video.id, video]));
  return catalog.collections.map(({ videoIds, ...collection }) => ({
    ...collection,
    videos: videoIds.flatMap((id) => {
      const video = byId.get(id);
      return video ? [video] : [];
    }),
  }));
}

/** A `?version=` value as a filter; anything unknown (or missing) is `fallback`. */
export function parseVersionFilter(raw: string | null, fallback: VideoVersionFilter): VideoVersionFilter {
  return raw === "all" || CONSOLE_VERSIONS.includes(raw as ConsoleVersion) ? (raw as VideoVersionFilter) : fallback;
}

/** Keep recordings from different console UIs in separate module playlists. */
export function videoCollectionsByVersion(
  collections: VideoCollection[],
  version: VideoVersionFilter,
): VideoCollection[] {
  const versions: readonly ConsoleVersion[] = version === "all" ? CONSOLE_VERSIONS : [version];
  return collections.flatMap((collection) => versions.flatMap((edition) => {
    const videos = collection.videos.filter((video) => video.consoleVersion === edition);
    return videos.length
      ? [{ ...collection, description: videos[0].description, videos }]
      : [];
  }));
}

/** The V2 recordings of the module whose sidebar entry is `path`, or null when it has none. */
export function moduleVideoCollection(catalog: VideoCatalog, path: string): VideoCollection | null {
  return videoCollectionsByVersion(videoCollections(catalog), "v2")
    .find((collection) => collection.path === path) ?? null;
}

export function videoTimestamp(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const remainder = String(total % 60).padStart(2, "0");
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${remainder}`
    : `${minutes}:${remainder}`;
}
