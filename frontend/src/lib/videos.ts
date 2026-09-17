import catalog from "../config/videos.json";

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

interface VideoCatalog {
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

export interface VideoCollection {
  id: string;
  categoryId: string;
  title: VideoText;
  description: VideoText;
  videos: LibraryVideo[];
}

// Imported only by the lazy Videos route; no environment or workspace lookup.
export const videoCatalog: VideoCatalog = catalog;

// Membership and ordering are validated before every frontend build.
export const videoCollections: VideoCollection[] = videoCatalog.collections.map(
  ({ videoIds, ...collection }) => ({
    ...collection,
    videos: videoIds.flatMap((id) => videoCatalog.videos.filter((video) => video.id === id)),
  }),
);

export function videoTimestamp(seconds: number): string {
  const total = Math.max(0, Math.floor(seconds));
  const minutes = Math.floor(total / 60);
  const remainder = String(total % 60).padStart(2, "0");
  return minutes >= 60
    ? `${Math.floor(minutes / 60)}:${String(minutes % 60).padStart(2, "0")}:${remainder}`
    : `${minutes}:${remainder}`;
}
