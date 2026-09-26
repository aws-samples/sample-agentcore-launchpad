export type VideoLocale = "en" | "zh-CN";
export type VideoText = Record<VideoLocale, string>;

export interface LibraryVideo {
  id: string;
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

export function videoTimestamp(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds <= 0) return "—";
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const remainder = String(total % 60).padStart(2, "0");
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${remainder}`
    : `${minutes}:${remainder}`;
}
