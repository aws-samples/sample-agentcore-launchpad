import { errorMessage, v2KnowledgeApi } from "./api";

/* A managed KB's retrieval result cites `location.s3Location.uri` — an https URL into
 * the data source's private bucket — so an agent's source link answers AccessDenied
 * in a browser. The console swaps it for a short-lived presigned GET on click. */

/** True for an S3 object address: `s3://bucket/key`, virtual-hosted
 *  `bucket.s3[.region].amazonaws.com/key` or path-style `s3[.region].amazonaws.com/bucket/key`. */
export function isS3ObjectUrl(href: string | undefined | null): href is string {
  if (!href) return false;
  if (href.startsWith("s3://")) return /^s3:\/\/[^/]+\/.+/.test(href);
  let url: URL;
  try {
    url = new URL(href);
  } catch {
    return false;
  }
  const host = url.hostname.toLowerCase();
  if (url.protocol !== "https:" || !host.endsWith(".amazonaws.com")) return false;
  const path = url.pathname.replace(/^\/+/, "");
  if (host.startsWith("s3.") || host.startsWith("s3-")) return /^[^/]+\/.+/.test(path);
  return /\.s3[.-]/.test(host) && path.length > 0;
}

/**
 * Open a cited source in a new tab. The tab opens synchronously (inside the click,
 * so no popup blocker objects) and is pointed at the presigned URL once the backend
 * answers; a failure closes it and reports through `onError`.
 */
export async function openKbSource(uri: string, onError: (message: string) => void): Promise<void> {
  const tab = window.open("", "_blank");
  if (tab) tab.opener = null;
  try {
    const { url } = await v2KnowledgeApi.sourceLink(uri);
    if (tab) tab.location.href = url;
    else window.open(url, "_blank", "noopener");
  } catch (err) {
    tab?.close();
    onError(errorMessage(err));
  }
}
