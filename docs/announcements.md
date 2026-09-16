# Announcements

The overview announcement panel replaces the lab-guide banner. The former
topbar lab shortcut is now a link in the hands-on-lab notice. Every signed-in
member can read published notices; **Administration → Announcements**
(`/announcements`) is visible only to administrators.

## Editing and publication

- **New / Save draft** writes the editable content only.
- **Publish** copies that draft into the public snapshot and records a new
  publication time. Editing an already published notice leaves its previous
  public snapshot visible until the administrator publishes again.
- **Withdraw** removes the public snapshot while keeping the editable draft.
- **Delete** permanently removes the notice, including its published snapshot.

The editor shows the current publication status and whether unpublished changes
exist. Every edit, publish, withdrawal, and delete carries the revision the
administrator read. A concurrent change returns `409 announcements.conflict`;
reload and review the latest version before trying again.
Unsaved editor buffers belong to the current page; save a draft before leaving
the management page. Notices also appear on the no-workspace-grant screen so an
authenticated member can read them before receiving workspace access.

Titles, bodies, and optional link labels are plain, user-authored text. They
remain in their authored language; console controls support English and Chinese.
Links must be app-relative paths beginning with a single `/`, or absolute HTTPS
URLs without credentials. HTML and Markdown are not rendered. A link URL and
label must be provided together.

## Storage and API

The `announcements` SQLite table is global to one Launchpad installation,
independent of the selected AWS workspace. It stores editable and published
content separately, revision, author usernames, and creation/update/publication
timestamps. It creates no AWS resources. Startup creates the table through the
normal ledger initialization; startup and reads never seed or publish content.

The central `route_policy.py` table enforces roles and exempts these routes from
workspace resolution. An unauthenticated caller receives `401` when auth is
enabled; a member requesting management routes receives `403`. The ordinary
auth-disabled development console retains its existing implicit-admin behavior.

| Method / path | Access | Behavior |
|---|---|---|
| `GET /api/announcements` | Member | Published snapshots, newest publication first |
| `GET /api/announcements/manage` | Admin | Drafts and published state, newest update first |
| `GET /api/announcements/{id}` | Admin | Editable detail |
| `POST /api/announcements` | Admin | Create draft; return `201` |
| `PUT /api/announcements/{id}` | Admin | Save draft with `expected_revision` |
| `POST /api/announcements/{id}/publish` | Admin | Publish the current draft |
| `POST /api/announcements/{id}/unpublish` | Admin | Withdraw the notice |
| `DELETE /api/announcements/{id}` | Admin | Delete with query `expected_revision` |

Lists return `{announcements, total}` and accept `limit` (1–100, default 20) and
`offset` (default 0). Create/edit content uses `title`, `body`, nullable `link_url`
and `link_label`; publish/withdraw bodies contain `expected_revision`. Published
responses expose no draft fields or author metadata.

## Publish the initial notices

[`config/announcements.initial.json`](../config/announcements.initial.json)
contains the three requested notices: hands-on lab guide, video library launch,
and planned migration to `https://launchpad.jugglehub.top`. The domain notice does
not specify a cutover date, create DNS records, or redirect traffic.

Use the editor for normal ongoing management. To publish the reviewed initial
file in another installation after deploying this code:

```bash
cd backend
uv run python scripts/publish_announcements.py --base-url http://127.0.0.1:8000
# Review the preview, then publish:
uv run python scripts/publish_announcements.py --base-url http://127.0.0.1:8000 --apply
```

The script uses the shared authenticated HTTP helper. When login is required,
provide `LAUNCHPAD_E2E_USERNAME` / `LAUNCHPAD_E2E_PASSWORD` in the environment.
It validates all input and checks existing titles before writes, skips identical
published notices, and refuses to replace edited notices. This is an explicit
initial-publication tool, not a background synchronization process. Administrators
manage later changes and removals through the console. Independent installations
have independent notice data; the linked video media URLs remain shared.
