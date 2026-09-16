# Shared video library

The console's **Learn → Videos** entry (`/videos`) plays tutorials directly from
CloudFront. The initial entry is the September 16, 2026 architect assistant
recording: starting a conversation, reviewing an elder-care application, deploying,
evaluating, and completing a configuration-bundle A/B experiment.

## Shared configuration

[`frontend/src/config/videos.json`](../frontend/src/config/videos.json) is the
version-controlled catalog. Every frontend build uses the same absolute HTTPS
media URLs, independent of workspace, AWS account, Region, or
`config/launchpad.yaml`. Updating a catalog reaches an environment when that code
revision is deployed; watching the videos never calls a Launchpad backend API.
No credentials or expiring signed URLs are required.

Each entry has a stable `id`, `en`/`zh-CN` title and description, publication date,
duration in seconds, narration language, JPEG poster, MP4/WebM sources, optional
WebVTT captions, and ordered chapter offsets with bilingual titles. Link to a
specific entry with `/videos?video=architect-assistant`.

The page loads only the selected video's metadata and does not autoplay. Native
controls provide seeking, volume, playback speed (browser dependent), and full
screen. The recording already has visible Chinese subtitles; the optional text
track starts off to avoid duplicate subtitles. WebM supports browsers whose builds
do not include H264/AAC decoding. All content is a recording, not live workspace
state.

`scripts/validate_video_catalog.mjs` runs before every frontend build and rejects
duplicate IDs, incomplete translations, unsupported media types, temporary URLs,
and unordered/out-of-range chapter offsets.

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
environments should reuse the checked-in catalog, not deploy another media stack.

## Publish or update a video

1. Prepare final MP4, optional WebM, JPEG poster, and optional `.vtt` captions in
   one local directory. The publisher can convert a same-basename `.srt` to
   WebVTT. Media binaries remain outside Git.
2. Add an entry to the catalog (or revise one), using the shared `BaseUrl` and a
   new revision directory. File names in URLs must match local basenames. Include
   both languages for title, description, and chapter labels; language identifies
   the actual narration.
3. Validate and preview the exact upload set from the repository root:

   ```bash
   node scripts/publish_videos.mjs --video architect-assistant \
     --source-dir data/videos/architect-assistant-intro-update-20260916 --dry-run
   ```

4. Run the same command without `--dry-run` to upload. It looks up the shared
   stack, checks all local files and existing objects first, and refuses URLs
   belonging to another CDN. Objects use their proper Content-Type and a SHA-256
   metadata value. Identical existing objects are skipped only when Content-Type
   and Cache-Control also match; incompatible content or metadata requires a new
   revision path. S3 validates the upload's SHA-256 checksum against the preflight
   digest, rejecting local files that changed during publication. Conditional
   writes also prevent a concurrent publisher from overwriting a key.
5. Check the CDN URL with a byte Range request, play the video and seek chapters
   in `/videos`, then run `make verify` and deploy the catalog/frontend revision
   through the ordinary application release process.

Optional publisher flags are `--region` (default `us-west-2`) and `--stack`
(default `launchpad-videos`). Uploading media does not restart or deploy the
application, and deploying the application does not upload media.
