# Shared video library

The console's **Learn → Videos** entry (`/videos`, `/v2/videos`) opens a
module library. A V2 module with several recordings has an ordered playlist;
a module with one recording opens its chapter list. Selecting a card opens a
native player backed by CloudFront. Agent Management includes a BYOC walkthrough
covering code ZIP and Dockerfile uploads, existing deployment records, and
fresh Chat calls (`/videos?video=agent-management-byoc`).

## Directory configuration and publication

The video directory is hub-global ledger data. `GET /api/videos` gives signed-in
members only published snapshots; the classic and V2 pages load it on entry.
`/v2/video-management` is an administrator-only configuration page backed by
`/api/videos/manage`. An administrator can add, edit, order, publish, withdraw,
and delete videos. A draft save does not change the public library. Publication
copies the saved draft; later edits stay private until the administrator
publishes again. Every write checks the row's revision, so a stale tab receives
409 and must reload. The backend authorizes all routes, including direct API
calls; the admin UI is an additional navigation gate.

[`backend/app/data/video_sections.json`](../backend/app/data/video_sections.json) fixes the two-level
directory to the V2 navigation: first-level functional areas (Agent development,
Agent runtime, Agent evaluation, Learn, Configuration, and Overview) and their
second-level modules. Admins assign a video to one module; they cannot introduce
a category that drifts from the V2 console. The library groups recordings by
module and recorded console version (`v2` or `classic`), retaining separate
playlists and previous/next navigation within each version. The V2 library
defaults to V2 recordings, while the classic library defaults to classic
recordings; both offer V2, Classic, and All controls. The `version`, `category`,
and `section` filters plus title search `q` survive watch/back navigation. All
shows separate cards for both versions of a module, and every card and watch
page identifies its version. A missing `video` opens the library; an invalid
one shows a notice without selecting a substitute. Stable video IDs preserve
old deep links, including a direct link outside the page's default version.

The required new-video fields are recorded console version, first-level area,
second-level module, Chinese title, Chinese introduction, and a permanent HTTPS
CDN URL ending in `.mp4` or `.webm`. Administrators choose the version when
saving a draft. English title/introduction default to Chinese when left blank. A JPEG
poster, WebM fallback, WebVTT captions, duration, chapter metadata, and module
order are optional. Admins can preview the CDN video before publication. The
backend does not upload, proxy, or probe the media; the browser reads it directly
from the CDN. The CDN URL must be uploaded and verified before publishing. The
same absolute URL works in every workspace and environment.

On the first database initialization after this feature is installed,
`videos` and `video_catalog_seed` import the existing 16 published entries
from [`backend/app/data/videos.initial.json`](../backend/app/data/videos.initial.json),
including MP4/WebM, poster, captions, chapters, and their stable IDs. This is a
one-time migration fixture and legacy media-publisher manifest, no longer the
runtime directory. Restarts never overwrite administrator changes, even after
all videos have been removed. `scripts/validate_video_catalog.mjs` still
validates that initial manifest before a frontend build.

Pre-version ledger snapshots remain unchanged. The API derives their version
only when the field is absent: a media path with an immutable
`YYYYMMDD-v2` or `YYYYMMDD-v2-*` revision is V2, while earlier revisions are
classic. New saves and explicit publication store the version in the draft and
published snapshot. The five original evaluation recordings remain on the CDN;
after their V2 replacements were published under the original directory IDs,
administrators restored the old recordings as separate classic entries through
video management. Existing V2 IDs and media stay intact.

The library mounts no player. The watch view loads only the selected video's
metadata and does not autoplay. Native
controls provide seeking, volume, playback speed (browser dependent), and full
screen. The recording already has visible Chinese subtitles; the optional text
track starts off to avoid duplicate subtitles. WebM supports browsers whose builds
do not include H264/AAC decoding. All content is a recording, not live workspace
state.

The public watch view lists only the selected module. Multiple recordings in
one module expose keyboard-accessible Series/Chapters tabs; a single recording
exposes chapters directly. The directory scrolls independently, and the full
introduction is collapsed until opened. Tab changes keep the player mounted;
selecting another video pauses and unmounts it.

## Infrastructure

The optional, standalone `launchpad-videos` stack is defined by
`infra/video_app.py` and `infra/stacks/video_stack.py`. Normal bootstrap, application
startup, and workspace creation do not deploy it.

| Resource | Shared deployment |
|---|---|
| Account / Region | `434444145045` / `us-west-2` |
| Stack | `launchpad-videos` |
| S3 bucket | `launchpad-videos-mediaa721a567-gzguwkeuaooh` |
| CloudFront distribution | `E2I51GHCGNST3L` |
| CDN base URL | `https://d3fbtvyrf8heia.cloudfront.net` |

S3 blocks all public access, enforces HTTPS and bucket-owner object ownership,
uses SSE-S3 encryption, and enables versioning. Only this distribution's OAC can
read objects. CloudFront redirects HTTP to HTTPS and permits GET/HEAD/OPTIONS.
Its response policy allows anonymous cross-origin media and caption reads from
any environment, exposes Range headers, and does not allow credentials. Videos
are public through the CDN: publish only material intended for tutorial viewers.

Versioned paths (`media/<id>/<revision>/<filename>`) cache for one year with
`immutable`; publish replacements to new paths instead of invalidating or
overwriting existing media. The stack has termination protection, and all media
resources and access policies use `RETAIN` to preserve published URLs. S3 storage
and CloudFront transfer/request charges apply. Resource retirement is a separate
operator action.

To reproduce in the media owner's AWS account, use the normal AWS credential
chain and an already bootstrapped CDK environment:

```bash
cd infra
CDK_DEFAULT_REGION=us-west-2 cdk diff \
  --app 'uv run python video_app.py' --output cdk.out-videos
CDK_DEFAULT_REGION=us-west-2 cdk deploy launchpad-videos \
  --app 'uv run python video_app.py' --output cdk.out-videos \
  --outputs-file ../data/video-cdn-outputs.json
```

The outputs provide `BucketName`, `DistributionId`, and `BaseUrl`. A deployment
in a different account produces different addresses; ordinary Launchpad
environments should use the existing shared media URLs and publish their own
metadata through video management, not deploy another media stack.

## Publish or update a video

1. Upload the finished `.mp4` or `.webm` to an HTTPS CDN under a new immutable
   revision path. Upload optional WebM, poster, and WebVTT in the same revision.
   Check the video and caption URLs with Range/CORS requests and browser playback.
   Media binaries remain outside Git. The media upload does not publish a
   Launchpad directory entry.
2. As an administrator, open **Configuration → Video management** in V2. Add a
   video with its first- and second-level V2 categories, title, introduction,
   and CDN URL. Set optional media, duration, chapters, and order as needed.
   Save the draft. Preview the CDN link in the editor, then select **Publish**.
3. Open `/v2/videos` or `/videos` as a member. Check its module card, episode
   selection, playback, and chapter seeking. Edits to a published video remain
   private until republished; **Withdraw** hides it without deleting its draft.
   Deleting a directory row does not delete CDN media.

`scripts/publish_videos.mjs` remains available for the **legacy import
manifest only** (`backend/app/data/videos.initial.json`). It validates local MP4,
WebM, JPEG, and VTT against that file, checks content type and SHA-256, and
conditionally uploads immutable objects to the existing S3/CDN. A video created
in the admin UI is not added to that historical manifest; upload its media
separately before pasting its CDN link. Publishing directory metadata needs no
Git commit or frontend redeployment.
