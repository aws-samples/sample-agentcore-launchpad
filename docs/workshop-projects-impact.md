# Workshop impact of projects (phase 1)

What the projects feature (design `docs/design/projects.zh-CN.md` §8.6, §9.6) changes
for the Agent-DLC workshop content repository (`agent-dlc-ws-remote`, `content/`), and
the text changes each affected page needs. This file only lists them; the workshop
repository is edited separately, after the M1 checks on a real environment.

## Summary

The workshop runs on one workspace (`default`) with five seeded accounts. After the
upgrade that workspace holds only the `default` project. All five accounts are
`inherit` members of it, so in a single-project console:

- no project switcher and no project chip appear in the top bar, and the command palette has no "switch project" entry;
- `/api/auth/status` returns the same `role` and 16-key `permissions` list (project keys are never listed);
- every member page, route and expected result in the flywheel chapters is unchanged. `infra` routes behave like `project.write`, and observability is not filtered;
- the seed script (`static/infrastructure/launchpad-multiuser.yaml`, `PATCH /api/users/{id}` with `workspaces: ["default"]`) needs no change: a workspace grant adds the `inherit` membership in the same transaction;
- the approve and create dialogs are unchanged. Project and group rows, and the folding of workspace grants under an "advanced" section, appear only once the platform has several projects or any project group. Design §8.6 expected an extra "join the default project only" option in the approval dialog; the implementation shows none in a single-project console, so no sentence about it is needed.

What does change is visible to the **administrator** only: new **Configure** entries,
a **Force logout** button on the user detail page, and extra fields in a few raw JSON
responses. A few explanatory sentences about API keys and separation of duties are
also now incomplete. Each item below gives the zh text first, then en; the `.en.md`
file of each page has the same structure and needs the matching change.

Priority: **Required** means a step's expected result or a screenshot no longer
matches. **Recommended** means a statement is now incomplete. **Optional** is an
improvement suggested by design §9.6.

## Page by page

### `10-prepare/20-setup` — 环境准备 / Setup

1. **Required — §3 rail table, row `配置` (line 169).** The administrator's **Configure** group now has five more entries right after **User Management**: **Administrators** (`/v3/admins`), **Projects** (`/v3/projects`), **Project groups** (`/v3/project-groups`), **Departments** (`/v3/departments`), **Project settings** (`/v3/project-settings`).
   - zh: `| **配置** | 用户管理、管理员管理、项目、项目组、部门、项目设置、多环境总览、工作区、出站凭证、公告管理、视频管理 | `/v3/users`、`/v3/admins`、`/v3/projects`、`/v3/project-groups`、`/v3/departments`、`/v3/project-settings`、`/v3/fleet`、`/v3/workspaces`、`/v3/identity`、`/v3/announcements`、`/v3/video-management` | 平台管理（仅管理员） | — |`
   - en: `| **Configuration** | User Management, Administrators, Projects, Project groups, Departments, Project settings, Fleet, Workspaces, Outbound credentials, Announcements, Video management | … same paths … |`
   - Any screenshot of the administrator's expanded rail (and the module count after the **Configure** group name) must be retaken.
2. **Optional — §5 / the bullet on the workspace switcher (line 179).** Add one sentence. zh: "本环境只有一个默认项目，所以顶栏上看不到项目切换器。" en: "This environment has a single default project, so the top bar shows no project switcher."

### `10-prepare/30-roles` — 认识角色与职责分离 / Roles and separation of duties

1. **Recommended — §1 table, row `admin` (line 20).** zh: "…唯一能管理账号、管理员、项目与项目组、工作区、放行策略的角色…"; en: "…the only role that manages accounts, administrators, projects and project groups, workspaces and the release policy…".
2. **Recommended — §1 alert after the account table (line 52) and §7.** The server now compares the actor's **user id** first and falls back to the username for records written before the upgrade. A re-registered account with an old username is not treated as the old actor. zh: "服务端按 **账号**（用户 id，旧记录按用户名）强制执行职责分离"; en: "the server enforces separation of duties per **account** (user id; older records by username)".
3. **No change — §3 member steps (lines 68–108).** The member rail, the `/v3/users` refusal and the `/api/auth/status` lists in 3.3 are unchanged: project keys are not part of `permissions`, and members do not see **Project settings** (it needs `project.members`, which `inherit` does not grant). Re-check the module counts on line 80 during M1. The B5 gateway tool targets entry is no longer a rail entry: it is the *Tool targets* tab of **Gateway governance** (`/v3/governance?tab=targets`), so the member rail is one entry shorter than with B5 alone.
4. **No change — §4.2 approval (lines 141–162).** The **Approve account** dialog still shows only the **Workspaces** field with `default` pre-selected in a single-project console; the account becomes an `inherit` member of the default project.
5. **Required — §5 user detail (lines 166–180, screenshot `15-roles-bizowner-perms`).** The detail header has a new **Force logout** (强制下线) button between the password action and **Delete**. Retake the screenshot if it shows the header, and add to the "look, don't click" alert. zh: "页头的 **强制下线** 会立即结束该账号的所有会话，也不要点。"; en: "Do not click **Force logout** in the header either: it ends every session of the account at once." The **Agent permissions** and **Workspace access** cards are unchanged: the project access panel appears only once the platform has several projects or the account has a group or non-default project.
6. **No change — §6 release policy (`/api/release-policies/default`)**: same JSON.
7. **Optional — §7 table, row "审批注册账号、授予工作区".** Add that granting the `default` workspace makes the account a member of the default project with the inherited global role.

### `10-prepare/40-teams` — 组队与分工 / Teams

- **No change** to the approval steps (line 42) or the troubleshooting row (line 77): approving with `default` selected, and granting `default` later on **Workspace access**, both add the `inherit` membership.
- **Optional (design §9.6) — an advanced exercise.** In the default project, an administrator can replace the `inherit` roles with explicit templates (`developer`, `biz_owner`, `release_ops`, `sme`) from **Configure → Projects → Default → Members**, to show project roles without changing any later step. If added, it must say that a template replaces the global permission chips for that account: after the change, **Agent permissions** edits on the user page no longer affect what the account may do in the project.

### `20-flywheel/40-release` — 发布 / Release

1. **Recommended — §4.1, step 2 (line 214).** zh: "密钥属于当前工作区里的当前项目（本 workshop 只有默认项目），页面左侧当前选中的是哪个智能体都不影响。"; en: "The key belongs to the current project of the current workspace (this workshop has only the default project); …".
2. **Recommended — §4.1 alert (line 225).** zh: "API Key 属于当前工作区的当前项目：这把密钥能调用 `default` 工作区默认项目里所有运行中的 Agent。它同时记下创建者，并随创建者的访问失效：创建者的账号被删除、停用、过期，失去该工作区授权或该项目的成员资格，或者项目被归档时，这把密钥被自动停用……"; en: "An API key belongs to the current project of the current workspace: this key can call every active agent of the default project in workspace `default`. … when that account is deleted, disabled, expires, loses its grant to the workspace or its membership of the project, or the project is archived, the key is disabled …". Commands and outputs in §4.2–§4.5 are unchanged (`/v1` ignores `X-Project`; the key's project is the default project).
3. **No change — §5 Gateway Cedar policies.** They are `infra` writes; done by `admin`, and with one project `project.write` would suffice anyway.

### `30-wrap-up/10-team-lab` — 团队实战 / Team lab

- **Recommended — timetable row 12:00–13:00 (line 108).** `GET /api/audit?action=…` still answers `{events[…]}`, but each event now also carries `actor_user_id`, `project_id` (here `"default"` for flywheel actions, `null` for account and structure events) and `workspace_id`. Add after "字段 `events`": "（每条事件带 `project_id`，飞轮里的动作为 `default`）"; en: "(each event carries `project_id`, `default` for the flywheel actions)". The route also accepts `&project=default` and `&actor=<username or user id>`.

### `30-wrap-up/20-cleanup` — 清理 / Cleanup

- **Optional — "把职责分离落到你的团队" (line 115).** Add one sentence: for several teams on one platform, create a project per team (**Configure → Projects**) and give people project roles instead of global permission chips; teams that also need runtime isolation still get their own workspace.

### `90-appendix/10-organizer-deploy` — 组织方部署 / Organizer deploy

1. **No change** to the create-account and approval steps (lines 396–451) or the visibility check (`/api/workspaces/default/settings/visibility`, line 290): the single-project dialogs are unchanged.
2. **Recommended — "修改账号能进入的工作区" (lines 418–428).** Add: removing `default` also removes the account's `inherit` membership of the default project. Its API keys stop at once (`401`), and so does its session's access to `default` (still `workspace.forbidden`).
3. **Recommended — upgrade note for a running environment.** Add a short paragraph near the deployment checks. zh: "升级到带项目的 Launchpad 版本时无需操作：首次启动会自动创建 `default` 项目、把已有数据和账号归入其中；可先在台账副本上运行 `backend/scripts/rehearse_projects_migration.py` 演练。" en: "Upgrading a running console to a Launchpad version with projects needs no action: the first start creates the `default` project and files existing data and accounts into it; rehearse first on a copy of the ledger with `backend/scripts/rehearse_projects_migration.py`." (See *Operating projects* in `docs/architecture.md`.)
4. **Optional — §"添加管理员" (line 536).** The administrator list now has its own page, **Configure → Administrators** (`/v3/admins`), which shows who can sign in and who cannot be demoted.

### `90-appendix/20-production-integration` — 生产集成 / Production integration

- **Recommended — comparison table, rows "权限粒度" (line 45) and "凭证意外失效" (line 50).** zh: "Key 属于工作区里的一个项目……"; "签发 Key 的账号被删除、停用、过期，失去工作区授权或项目成员资格，或项目被归档时，Key 会**自动停用**……". en: "The key belongs to one project of the workspace…"; "…deleted, disabled, expired, loses its workspace grant or project membership, or the project is archived, the key is **disabled automatically**…".

### `90-appendix/30-glossary` — 术语表 / Glossary

1. **Recommended — §6 add a row after 工作区.** zh: `| **项目**（project） | 工作区内的数据与权限边界：智能体及其数据只属于一个项目，成员按项目角色工作 | 本 workshop 只有默认项目 `default`，所以看不到项目切换器；所有账号以“继承全局角色”加入它 | [认识角色 §4](/10-prepare/30-roles) |`. en: `| **Project** | The data and permission boundary inside a workspace: an agent and its data belong to one project, and members work through project roles | This workshop has only the `default` project, so there is no project switcher; every account joins it with the inherited global role | … |`.
2. **Recommended — §7 row 职责分离.** "服务端按**用户名**强制" → "服务端按**账号**（用户 id；旧记录按用户名）强制" / "per **account** (user id; older records by username)".

## Online update (OU-1) — the organizer stack

Not a projects change, but it ships in the same upgrade. **Configure → System update**
(`/v3/system-update`, administrators only) can schedule a restart only where the root
updater is installed, so the workshop's CloudFormation user-data
(`static/infrastructure/workshop-ec2.yaml`, after `systemctl enable --now
launchpad-backend`) needs one more step:

```bash
"$APP/backend/scripts/install_updater.sh" --repo "$APP" --user ubuntu \
  --services "launchpad-backend"   # this stack has no launchpad-frontend unit: nginx serves dist/
```

- The stack clones with `--depth 1`. The check still works, because the update cache
  takes the running commit's history from the local checkout.
- nginx serves `frontend/dist` directly. The updater swaps `dist` in by rename, so
  nginx needs no restart.
- **Recommended — `90-appendix/10-organizer-deploy`.** Add a sentence. zh:
  "控制台可在 **配置 → 系统更新** 在线更新：准备好后排期重启，失败会自动回滚。" en: "The console
  updates itself from **Configure → System update**: prepare, schedule the restart; a
  failed health check rolls back automatically."
- **Required — `10-prepare/20-setup` §3 rail table, row `配置`.** The administrator's
  **Configure** group gains **System update** (`/v3/system-update`) after **Models**.
  Retake any screenshot of the expanded rail.

## Verification at M1

Design §8.6 lists the checks to repeat on a real upgraded environment before these
edits are made: the five logins and their `/api/auth/status` lists; granting
`criteria.sign` to `biz-owner` on the user page taking effect in the default project;
members not seeing the **Configure** group; approving a self-registered account; the
`developer` → `biz-owner` → `release-ops` → `sme` hand-off on one agent; the `gated`
release policy; and the SoD refusals. Retake the screenshots listed above at the same
time.
