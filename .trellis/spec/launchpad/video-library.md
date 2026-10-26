# Shared video library

- `/videos` and `/v2/videos` browse modules; `?video=<id>` watches a recording.
  Both read the hub-global published projection from `GET /api/videos`, with no
  workspace/AWS call. Admin CRUD and explicit per-video publish/withdraw live under
  `/api/videos/manage` and are `ADMIN` in `ROUTE_POLICY` and `WORKSPACE_EXEMPT`.
- Public video schema version 3 includes `consoleVersion: v2|classic`. Admin
  `VideoContent` stores `console_version` in each draft and published snapshot.
  Earlier ledger JSON that lacks the field is classified on read from a
  `YYYYMMDD-v2[-suffix]` media revision, otherwise Classic; reads do not rewrite
  the ledger. Admin saves and explicit publication persist the field.
- `backend/app/data/video_sections.json` fixes the two-level V2-aligned taxonomy. The first
  level is a V2 navigation group, the second its module; admin edits only each
  video's placement, metadata, order, and publication state. Every published
  video belongs to exactly one section. A section may outlive its sidebar entry: when a
  module folds into another page's tab, keep the section and its path (no ledger
  migration) and map the tab onto it with `V2NavItem.videoPath` — e.g. 评估任务 maps
  `tab=online` / `kind=online` / `strategy=continuous` / `from=online:*` to
  `/v2/eval/online` (`ONLINE_VIDEO_PATH`); `test_videos.py` reads `*_VIDEO_PATH`
  constants alongside the nav `to:` paths. In the library and watch view,
  collections are split by section and console version; the server's section
  order comes from the published snapshot, never from an unpublished draft.
- V2 and Classic libraries default to their corresponding version and both
  offer V2, Classic, and All. All shows separate cards for two versions of one
  module. A direct `?video=` deep link opens its recording regardless of the
  default filter, and back navigation selects that recording's version.
- `videos` and `video_catalog_seed` are hub-global ledger tables. The first
  `init_db` call imports the former bundled catalog from
  `backend/app/data/videos.initial.json`
  once, including media sources, captions, chapters and ordering. The marker
  prevents an intentionally emptied directory from being reseeded at restart.
  Later metadata edits never require a frontend rebuild.
- Use `version`/`category`/`section`/`q` query parameters for library filters and title search; preserve
  them when selecting recordings or returning to the library. Search returns
  individual matching episodes, not only their collection's first recording.
  Missing `video` opens the library; invalid `video` shows an explicit notice there.
- The watch directory contains only the current collection, with previous/next
  navigation following catalog order. Multi-episode collections use accessible
  Series/Chapters tabs (arrow keys, Home/End, roving tab index); singletons show
  chapters directly. Bound directory scrolling and collapse the long description.
  Tab changes must preserve the same player, while episode/route changes unmount it.
- New video drafts require the recording's console version, first- and second-level category, title,
  introduction, and permanent HTTPS CDN MP4/WebM URL. English copy falls back to
  Chinese; poster, WebM fallback, WebVTT, duration, chapters and order are optional.
  Saving never changes the public snapshot. Publication copies the saved draft;
  withdrawal removes only the public snapshot. All mutations require the current
  revision, including the SQLAlchemy update/delete predicate.
- CDN URLs are absolute, permanent, environment-independent, and should use
  immutable `media/<id>/<revision>/<filename>` paths. Do not use signed URLs or
  recreate the media stack for each Launchpad environment.
- `infra/video_app.py` deploys only `launchpad-videos`. Do not wire it into ordinary
  bootstrap or `infra/app.py`. Private S3 access uses CloudFront OAC with SourceArn;
  allow anonymous cross-origin reads in the CDN response policy (no credentials).
- Keep the bucket, CDN, OAC, and bucket/response policies retained together; a
  retained CDN with a deleted bucket policy stops serving already published URLs.
- Use native HTML video with a keyed player per selection and pause on unmount.
  Avoid eager video download and autoplay. MP4 plus WebM covers browser builds
  missing H264. Delay chapter seek until metadata is ready; handle play rejection.
  Chapter clicks scroll the player into view, respecting reduced-motion preferences.
- Child source errors may occur without an HTMLVideoElement error; display failure
  when all sources fail, with retry. Caption errors must not hide a working video.
- Burned-in Chinese subtitles are present in the first tutorial. Native WebVTT is
  optional and defaults off to avoid duplicate text.
- `scripts/publish_videos.mjs` remains a media uploader for the legacy manifest;
  the admin UI publishes metadata for already uploaded CDN media. The script uses
  AWS CLI, checks every local asset before uploads, refuses mismatching existing
  content, and uses conditional object creation.
  Match existing Content-Type/Cache-Control too, and send an S3 SHA-256 checksum
  so a local file changed after preflight cannot publish with stale hash metadata.
- Frontend build still validates the legacy import/publisher manifest. Review
  published/draft isolation, admin route denial, V2 taxonomy parity, CDN Range/CORS,
  real playback/seek, responsive layout, and source fallback when changing this
  surface. `make verify` remains the required completion gate.
