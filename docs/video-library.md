# Shared video library

> **CN build note.** The CN port ships the directory, the pages and the admin
> workflow, but **no media stack**: there is no `launchpad-videos` CDK stack, no
> publish script and no bundled recordings. Recordings are self-hosted on the
> console host under `/media/` (nginx, outside the git checkout); every media field
> is a permanent HTTPS URL the administrator enters in Video management; the backend
> never uploads, proxies or probes it, and the browser plays it directly. See
> [Media hosting](#media-hosting-cn-build) below.

The console's **Learn → Videos** entry (`/videos`, `/v2/videos`; Learn is the last
sidebar group, below Configuration, and Videos is its only entry) opens a
module library. A V2 module with several recordings has an ordered playlist;
a module with one recording opens its chapter list. Selecting a card opens a
native player that streams from the recording's CDN URL. The feature is gated by the `videos` entry of
`Settings.features` (`GET /api/capabilities`): without it the entry is hidden and
the route renders not-found.

## Directory configuration and publication

The video directory is hub-global ledger data. `GET /api/videos` gives signed-in
members only published snapshots; the library page loads it on entry.
The administrator-only **Manage** tab of the same page (`/v2/videos?view=manage`;
`&edit=new` / `&edit=<id>` open the editor; the former `/v2/video-management` URL
redirects here) is backed by `/api/videos/manage`. Members see no tabs; a deep link to
the tab shows the admin-required card. An administrator can add, edit, order, publish, withdraw,
and delete videos. A draft save does not change the public library. Publication
copies the saved draft; later edits stay private until the administrator
publishes again. Every write checks the row's revision, so a stale tab receives
409 and must reload. The backend authorizes all routes, including direct API
calls; the admin UI is an additional navigation gate.

[`backend/app/data/video_sections.json`](../backend/app/data/video_sections.json) fixes the two-level
directory to the V2 navigation: first-level functional areas (Agent development,
Agent runtime, Agent evaluation, Learn, Configuration, and Overview) and their
second-level modules. Admins assign a video to one module; they cannot introduce
a category that drifts from the V2 console. When the navigation moves a module to
another area (Skill Lab now sits under Agent evaluation), edit only this file:
stored drafts and published snapshots keep their old area, and the API places them
under the section's current area on read and on the next publish. Videos filed under the
retired `my-connections` section are listed under `connections` (remap on read, `_SECTION_ALIASES`
in `services/videos.py`; the stored snapshot is never rewritten). The library groups recordings by
module and recorded console version (`v2` or `classic`), retaining separate
playlists and previous/next navigation within each version. The library
defaults to V2 recordings and offers V2, Classic, and All controls; the classic
console itself was retired (r13-a, 2026-10-17), but recordings of it stay
published and viewable under the Classic filter, and the data model keeps the
`console_version` dimension. The `version`, `category`,
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
`videos` and `video_catalog_seed` import
[`backend/app/data/videos.initial.json`](../backend/app/data/videos.initial.json)
once. In the CN build that manifest is **empty** (the Global recordings show the
Global console and are not seeded); only the `video_catalog_seed` marker is
written, so restarts never overwrite administrator changes, even after all
videos have been removed. The import path is kept for an operator who wants to
seed a fresh ledger: a manifest entry carries its V2 module as `sectionId`
together with MP4/WebM, poster, captions, chapters and a stable `id`
(`backend/tests/test_videos.py` exercises it against a temporary manifest).

Pre-version ledger snapshots remain unchanged. The API derives their version
only when the field is absent: a media path with an immutable
`YYYYMMDD-v2` or `YYYYMMDD-v2-*` revision is V2, while earlier revisions are
classic. New saves and explicit publication store the version in the draft and
published snapshot (a CN ledger only ever holds rows with the field; the rule
is kept for parity with upstream snapshots).

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

## Demo videos inside each module

Every V2 module page shows its own recordings without leaving the page. Each
published collection carries its module's V2 sidebar `path` from
`video_sections.json`. The V2 shell loads the published catalog once, resolves
the current location to its sidebar entry (sub-pages, `?view=` views, and the
agent flows listed under an entry's `also` count as that module), and gives
the page header a **Demo videos** button when the module has V2 recordings.
The button shows the recording count when there is more than one. Classic-only
recordings, the library page itself, and a failed catalog load show no button.
The button opens a drawer with the shared player, the module's playlist, and
chapters. **Open in the video library** jumps to the same video's watch page.
Closing the drawer or navigating away unmounts and pauses the player. A video
reaches its module's button once it is published under that module. The shell
reloads the catalog when the administrator leaves video management; other open
tabs pick changes up on their next page load.

## Media hosting (CN build)

The CN build deploys **no media infrastructure**: `infra/stacks/video_stack.py`,
`infra/video_app.py`, `scripts/publish_videos.mjs` and
`scripts/validate_video_catalog.mjs` from the Global repository were deliberately
not ported. The Global recordings' CDN is not in this account, and the account's
public-entry rules (CloudFront + private origin only) rule out a per-environment
media stack. **Recordings are self-hosted on the console host** (user decision
2026-10-17, N13-obs11): nginx serves the directory `/home/ubuntu/launchpad-media/`
as `/media/` on the console port, next to the SPA and the API — static files only,
no directory listing, byte-range requests, explicit MIME types for `.mp4`, `.webm`,
`.vtt` and `.jpg`, and `Cache-Control: public, max-age=31536000, immutable`
(`infra/ec2/nginx-site.sh`). The directory lives outside the git checkout; a fresh
host creates it at first boot and `scripts/host-update.sh` never writes to it.
Staging a file on the host is a deploy-ops step
([deploy.md §8.8](cn/interfaces/deploy.md#88-self-hosted-demo-video-media-n13-obs11)).

**Path convention** (one immutable revision directory per recording):

```
/media/<section>/<YYYYMMDD-v2-cn-hr>/<slug>.zh-CN.mp4     primary source
/media/<section>/<YYYYMMDD-v2-cn-hr>/<slug>.zh-CN.webm    optional second source
/media/<section>/<YYYYMMDD-v2-cn-hr>/<slug>.jpg           poster
/media/<section>/<YYYYMMDD-v2-cn-hr>/<slug>.zh-CN.vtt     captions (WebVTT)
```

`<section>` is the V2 section id from `video_sections.json` (for example `agents`,
`assistant`), the revision segment is the recording date, the console version and
a quality tag (`-cn-hr`), and `<slug>` is a short ASCII name. A path whose revision
segment matches `YYYYMMDD-v2…` is classified as a V2 recording when the entry
carries no explicit console version. Publish a replacement under a **new revision
directory** instead of overwriting — the year-long immutable cache means an
overwritten file would keep playing the old bytes in every browser that saw it.
Media binaries never enter Git.

**Which origin to write.** The media URL entered in Video management is the full
`https://<host>:<port>/media/…` address — the same host and port the viewer's
browser shows for the console (the `ConsoleUrl` output, or the NLB address when the
console is reached through one). The player requests media with
`crossOrigin="anonymous"`, so a URL on a *different* host than the page (the
instance IP while viewers use the NLB IP, or the reverse) is a cross-origin request
that nginx — which sends no CORS headers for `/media/` — would make the browser
refuse. Any other permanent, credential-free HTTPS URL (an operator-run CDN) is
still accepted by the backend; such a host must then allow anonymous cross-origin
`GET`/`HEAD` with `Range` itself. The backend stores and serves URLs only; it never
uploads, proxies or probes media. Publish only material intended for tutorial
viewers.

## Publish or update a video

1. Stage the finished `.mp4` (and optional `.webm`, JPEG poster, WebVTT captions)
   on the console host under a new revision directory of
   `/home/ubuntu/launchpad-media/<section>/<YYYYMMDD-v2-cn-hr>/` — the steps are in
   [deploy.md §8.8](cn/interfaces/deploy.md#88-self-hosted-demo-video-media-n13-obs11)
   (upload to the evidence bucket from the producer machine, copy onto the host
   with the instance role). Check `https://<console>/media/…` for the video and
   caption files with a `Range` request and in a browser. Staging a file does not
   publish a Launchpad directory entry.
2. As an administrator, open **Learn → Videos** in V2 and switch to the **Manage**
   tab (`/v2/videos?view=manage`). Add a
   video with its first- and second-level V2 categories, recorded console version,
   title, introduction, and the media URL (`https://<console>/media/…mp4`). Set
   optional media, duration, chapters, and order as needed. Save the draft, preview
   the link in the editor, then select **Publish**.
3. Open `/v2/videos` or `/videos` as a member. Check its module card, episode
   selection, playback, chapter seeking and, for a V2 recording, the **Demo
   videos** button on the module's own page. Edits to a published video remain
   private until republished; **Withdraw** hides it without deleting its draft.
   Deleting a directory row does not delete CDN media.

Publishing directory metadata needs no Git commit, frontend redeployment or
backend restart.
