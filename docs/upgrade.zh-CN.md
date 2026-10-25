# 升级 / Upgrade

本文说明如何把正在运行的 Launchpad 控制台从旧版本升级到新版本：代码、后端与前端依赖，以及 SQLite 台账（`data/launchpad.db`）。开始之前请先完整读一遍。

English: [upgrade.md](upgrade.md)

> 本示例未针对生产环境加固（见 [setup.zh-CN.md](setup.zh-CN.md#托管部署)）。下面的步骤能保护你已有的数据，但在用户依赖它之前，请先在你自己的主机上验证升级。

## 升级会改变什么

| 部分 | 位置 | 升级方式 |
|---|---|---|
| 代码 | git 检出目录 | `git merge --ff-only` 到新提交 |
| 后端依赖 | `backend/uv.lock` → `backend/.venv` | `uv sync --frozen` |
| 前端 | `frontend/package-lock.json` → `node_modules`，构建到 `frontend/dist` | `npm ci && npm run build` |
| 台账 | `data/launchpad.db`（SQLite） | **在下一次后端启动时自动迁移**，没有单独的迁移命令 |
| 配置 | `config/launchpad.yaml`、systemd 单元的环境变量、`/etc/launchpad-*.env` | 不受影响；`config/` 和 `data/` 都被 git 忽略 |

台账迁移在 `init_db`（`backend/app/core/db.py`）中执行，后端每次启动都会运行。它创建新增的表（`create_all`），添加新的列和索引（`_migrate`，幂等的 `ALTER TABLE … ADD COLUMN`），写入 `default` 工作区和 `default` 项目，把已有的每一行一次性归入 `default` 项目（由 `platform_settings` 中的 `migration.projects_v1` 标记保证只执行一次），然后检查表结构与模型一致、每一行都有工作区、每一个项目所属的行都有项目。任何检查失败，后端都会拒绝启动，并在日志中说明原因。再次重启不会重复执行：每一步都是幂等的。

AWS 仍是事实来源：台账只保存标识符和派生的进度。升级不会创建、修改或删除 AWS 资源。

## 选择升级方式

| 方式 | 适用场景 |
|---|---|
| [一键升级](#一键升级) | 推荐的第一次升级方式：用一条命令执行下面的手动升级步骤，并在新版本起不来时自动回滚。 |
| [手动升级](#手动升级) | 任何时候都可用。从没有 **配置 → 系统更新** 页面的版本（在线更新功能之前的所有版本）**第一次升级时必须手动**（或用一键升级）。 |
| [在线更新](#在线更新配置--系统更新) | 当前版本已带在线更新，并已安装 root 更新器。管理员在控制台中准备、排期，必要时回滚。 |

下面的步骤以 workshop 的参考主机为例：Ubuntu EC2 实例，检出目录 `/home/ubuntu/agentcore_launchpad`，属主 `ubuntu`，systemd 单元 `launchpad-backend` 在 `backend/` 下运行 `uv run uvicorn app.main:app`，nginx 直接提供 `frontend/dist`。请换成你自己的路径、属主和单元。如果你的主机还运行 `launchpad-frontend` 单元（`vite preview`，见 [setup.zh-CN.md](setup.zh-CN.md#托管部署)），本文每次重启 `launchpad-backend` 时都要一并重启它。

## 一键升级

`backend/scripts/upgrade.sh` 按顺序执行下面[手动升级](#手动升级)的每一步，并在任何一步失败时停下。它随**新**版本发布，所以从发布分支取出来再运行，不要用旧检出目录里的（旧版本没有它）：

```bash
cd /home/ubuntu/agentcore_launchpad
git fetch origin
git show origin/main:backend/scripts/upgrade.sh > /tmp/lp-upgrade.sh
sudo bash /tmp/lp-upgrade.sh --repo "$PWD" --dry-run      # 只做检查、备份和演练，不改任何东西
sudo bash /tmp/lp-upgrade.sh --repo "$PWD"                # 正式升级
sudo bash /tmp/lp-upgrade.sh --repo "$PWD" --install-updater   # 同上，并安装在线更新器
```

| 步骤 | 内容 | 失败时 |
|---|---|---|
| 1. 预检查 | 检出目录属主、没有本地修改、能快进到目标、磁盘空闲（`--min-free-gb`，默认 3）、找到 `uv` 和 `npm`、systemd 单元存在 | 停下，什么都没改 |
| 2. 进行中的任务 | 运行中版本的 `inflight.py`；退出码 `3` 时停下，除非加 `--yes` | 停下，什么都没改 |
| 3. 备份台账 | 用**目标版本**的 `ledger_backup.py` 在线备份并校验，改名为 `….bak-<时间>-preupgrade-<旧 sha>`（不参与轮换），旁边复制 `config/launchpad.yaml` 和 `data/sso-secret.key` | 停下，什么都没改 |
| 4. 演练迁移 | 在临时 worktree 中用目标版本的 `rehearse_projects_migration.py` 迁移备份副本 | 停下，什么都没改 |
| 5. 更新 | `git merge --ff-only`、`uv sync --frozen`、`npm ci && npm run build`（旧构建保留为 `frontend/dist.prev`） | 自动回滚 |
| 6. 重启并验证 | 重启单元（台账在这次启动时迁移），等待 `/api/auth/status` 返回目标的 `version.sha`（`--health-timeout`，默认 180 秒） | 自动回滚 |

`--dry-run` 在第 4 步后结束。自动回滚会把代码和 `frontend/dist` 恢复到升级前的提交、执行 `uv sync --frozen`、重启并确认控制台恢复响应。台账保持迁移后的状态（旧版本能在上面启动，见[回滚](#回滚)），第 3 步的备份仍在。

退出码：`0` 已升级（或已经是目标版本）；`1` 停在第 1–4 步，什么都没改；`2` 升级失败并已回滚；`3` 回滚也失败，请按[回滚](#回滚)手工处理；`64` 参数错误。每次运行的完整日志在 `data/upgrade/upgrade-<UTC 时间>.log`。其他参数见 `bash /tmp/lp-upgrade.sh --help`（`--target` 指定提交或分支，`--services` 指定要重启的单元，`--health-url` 指定健康检查地址）。

## 手动升级

**以检出目录的属主**（这里是 `ubuntu`）执行每一条命令，不要用 root：以 root 运行 `git`、`uv`、`npm` 会在 `.git`、`.venv`、`node_modules`、`dist` 里留下 root 所有的文件，服务和以后的升级都无法改写它们。只有 `systemctl` 需要 `sudo`。非交互式 SSH 会话的 `PATH` 里可能没有 `uv`，请用绝对路径（`/home/ubuntu/.local/bin/uv`）。

### 1. 预检查

```bash
cd /home/ubuntu/agentcore_launchpad
stat -c %U .                                   # 属主：下面的命令都以该用户执行
git status --short                             # 必须没有输出：本地修改会阻止快进
git rev-parse HEAD | tee ~/launchpad-prev-sha  # 回滚目标提交，务必保存
df -h .                                        # 空闲磁盘（见下文）
(cd backend && uv run python scripts/inflight.py); echo "inflight exit=$?"   # 如果你的版本带有该脚本
```

- **磁盘。** 升级需要空间存放台账备份（与 `data/launchpad.db` 一样大）、新的 Python 包、`node_modules` 和新的 `dist`。请保留几 GB 空闲；在线更新在空闲空间低于 `LAUNCHPAD_UPDATE_MIN_FREE_GB`（默认 3）时拒绝准备。
- **进行中的任务。** 重启会中断后台任务：尚未开始批处理的评测运行、Skill Lab 作业、架构助手正在进行的对话轮次、运行中的数据流水线。部署和工作区引导会从检查点继续。`scripts/inflight.py` 以只读方式读取台账，列出每一项以及重启对它的影响。退出码 `0`（没有任务）或 `2`（只有可续跑的任务）：继续。退出码 `3`：有任务会失败或被清除，请等待后重新运行，或者知情后重启，并告知相关人员需要重新提交哪些任务。各结果的含义见 [agent-runbook-prod.md](agent-runbook-prod.md#update-recipe-verified-sequence)。在线更新会要求同样的确认（见下文）。
- **工具链。** `uv` ≥ 0.8、Node.js ≥ 20，与 [setup.zh-CN.md](setup.zh-CN.md#前置条件) 一致。后端需要 Python ≥ 3.12，由 `uv` 提供。
- **网络。** 主机需要能访问 git 远端、PyPI 和 npm 源。前端锁文件还会从 `https://cdn.sheetjs.com` 下载 `xlsx`，如果出站流量受限，请放行该地址。

### 2. 备份台账

```bash
(cd backend && uv run python scripts/ledger_backup.py); echo "backup exit=$?"   # 不是 0 就停下
```

`ledger_backup.py` 用 SQLite 的在线备份 API 复制正在使用的台账，后端运行时也安全（直接 `cp` 正在使用的文件则不安全）。它写出 `data/launchpad.db.bak-<UTC YYYYmmdd-HHMMSS>`，对副本运行 `PRAGMA integrity_check`，并打印路径、大小以及 `agents` / `jobs` / `eval_runs` 的行数。退出码 `0` = 已写出并校验通过；`1` = 没有备份；`2` = 副本未通过完整性检查（保留为 `….corrupt`），弄清原因之前不要继续。

- **没有该脚本的旧版本**可以用系统自带的 `python3` 运行新版本中的脚本（带 `--db` 时不需要虚拟环境）：

  ```bash
  git fetch origin
  git show origin/main:backend/scripts/ledger_backup.py | python3 - --db data/launchpad.db
  ```

- **让这份备份不参与轮换。** 在线更新器会删除最新 10 份之外的 `launchpad.db.bak-<stamp>` 文件（`--keep 10`）；带后缀的文件名永远不会被删除。请给升级前的这份备份改名，例如 `mv data/launchpad.db.bak-<stamp> data/launchpad.db.bak-<stamp>-preupgrade`，并保留到新版本稳定运行一段时间之后。
- **同时复制不在台账里的文件：** `config/launchpad.yaml`，以及存在时的 `data/sso-secret.key`（加密台账中单点登录客户端密钥的密钥；带 SSO 的版本在未设置 `LAUNCHPAD_SSO_SECRET_KEY` 时会在首次使用时创建它）。恢复台账而没有对应的密钥，这些密钥就无法解密。

### 3. 在备份上演练迁移

先在副本上运行新版本的迁移。`rehearse_projects_migration.py` 随新版本发布，所以把新提交检出到运行中检出目录旁边的临时 worktree，运行中的服务不受影响：

```bash
git fetch origin
git merge-base --is-ancestor HEAD origin/main && echo "fast-forward: ok"
git log --oneline HEAD..origin/main | wc -l          # 落后的提交数
git worktree add --detach /tmp/launchpad-rehearsal origin/main
cd /tmp/launchpad-rehearsal/backend
uv sync --frozen
uv run python scripts/rehearse_projects_migration.py \
  /home/ubuntu/agentcore_launchpad/data/launchpad.db.bak-<stamp>-preupgrade
cd /home/ubuntu/agentcore_launchpad
git worktree remove --force /tmp/launchpad-rehearsal
```

传入的台账只会被读取。脚本把它复制到临时目录，在副本上执行与后端启动时完全相同的 `init_db`（禁止创建 AWS 客户端），并按表打印行数、迁移前后没有项目的行数、孤儿行数（父行已不存在的派生行，归入所在工作区的默认项目），然后打印 `init_db` 用时和结果：

```text
table                          kind         rows  null before  null after  orphans
api_keys                       strict          …            …           0        …
…
init_db took 0.53s
OK every row has a workspace, every strict row a project; no drift
```

退出码 `0` = 正式升级会成功。退出码 `1` = 有检查失败；迁移后的副本会保留并打印路径。此时不要升级，保存输出并提交 issue。`--out /tmp/rehearsal.db` 可以保留迁移后的副本供检查。在 workshop 主机上 `init_db` 用时 0.53 秒（us-west-2）和 0.62 秒（us-east-1），新版本首次启动不会明显变慢。

如果新版本没有 `rehearse_projects_migration.py`（它随项目功能发布），跳过这一步；第 2 步的备份仍是你的保障。

### 4. 更新代码

```bash
cd /home/ubuntu/agentcore_launchpad
git fetch origin
git merge --ff-only origin/main
git log --oneline -1
```

`main` 是发布分支，也是在线更新默认跟随的分支。还在跟随旧 `console-v3` 分支的主机，用同一条命令即可切换：`console-v3` 的所有提交都是 `main` 的祖先，合并是快进。跟随其他分支或指定提交的方法相同（`git merge --ff-only <sha>`）。`--ff-only` 会拒绝任何不是当前提交快进的情况，例如本地提交或历史被改写；请先解决这些问题再继续，不要强制覆盖。

### 5. 后端依赖

```bash
cd backend && uv sync --frozen && cd ..
```

`--frozen` 严格按 `backend/uv.lock` 安装，失败时不会重新解析依赖。例如从 a7f550d 升级到项目版本时，锁文件新增了 `authlib` 和 `python3-saml`（单点登录），它们会带来 `xmlsec` 和 `lxml`。锁文件中有 `xmlsec` 的预编译 wheel，覆盖 glibc 2.28 及以上的 Linux x86_64 与 aarch64（Ubuntu 20.04+、Amazon Linux 2023）、macOS 和 Windows。在更旧的 Linux 上（Amazon Linux 2 的 glibc 是 2.26），`uv` 会从源码编译 `xmlsec`，需要先安装系统库，Debian / Ubuntu 上是 `sudo apt-get install -y pkg-config libxml2-dev libxmlsec1-dev libxmlsec1-openssl`。

systemd 单元运行的是 `uv run`，它在启动时也会同步依赖；自己先运行 `uv sync --frozen`，可以在重启之前而不是之后发现失败。

### 6. 前端

```bash
rm -rf frontend/dist.prev && cp -a frontend/dist frontend/dist.prev   # 保留旧构建，便于快速回滚
cd frontend && npm ci && npm run build && cd ..
```

`npm ci` 严格按 `package-lock.json` 安装。nginx（或 `vite preview`）提供的是 `frontend/dist`，所以**必须构建**，否则浏览器拿到的仍是旧控制台，却在调用新 API。nginx 无需重启。

### 7. 重启：台账自动迁移

```bash
sudo systemctl restart launchpad-backend          # 如果运行了 launchpad-frontend 单元，一并重启
sudo journalctl -u launchpad-backend --since "2 min ago" -q
```

这次启动时 `init_db` 按 [升级会改变什么](#升级会改变什么) 中的描述迁移台账。项目版本首次启动时会创建 `default` 项目并绑定到每个工作区，把已有的每个智能体、作业、数据集及其派生行归入该项目，并把每个拥有工作区授权的非管理员账号以 `inherit` 身份加入该工作区的默认项目。每个工作区只有一个项目时，用户看到的一切都不变。详见 [architecture.zh-CN.md](architecture.zh-CN.md#运维)。

### 8. 验证

```bash
curl -s http://127.0.0.1:8000/api/auth/status        # 公开接口；包含 "version": {"sha": …}
git rev-parse HEAD                                   # 必须与 version.sha 相同
systemctl is-active launchpad-backend
sudo journalctl -u launchpad-backend --since "5 min ago" -q | grep -icE "error|traceback"   # 期望 0
```

`version.sha` 是运行中的进程加载的提交；如果仍是旧提交，说明重启没有生效。然后在浏览器中以管理员身份登录，打开几个页面（智能体、评测、**配置 → 项目**），确认你的智能体和历史记录都在。一切正常后删除旧构建：`rm -rf frontend/dist.prev`。

## 在线更新（配置 → 系统更新）

主机运行带在线更新的版本后，管理员可以在控制台中完成之后的升级。先在检出目录中以 root 安装一次更新器：

```bash
sudo backend/scripts/install_updater.sh                 # 默认值取自当前检出目录
# workshop 主机没有 launchpad-frontend 单元（nginx 直接提供 dist/）：
sudo backend/scripts/install_updater.sh --user ubuntu --services "launchpad-backend"
systemctl status launchpad-updater.path                 # 预期 active (waiting)
```

安装脚本把 `backend/scripts/apply_update.sh` 复制到 `/usr/local/lib/launchpad/`（更新不会替换执行它的脚本），写入 `/etc/launchpad-updater.env` 以及 `launchpad-updater.path` / `launchpad-updater.service` 两个单元。只有在你明确想用更新的 `apply_update.sh` 时才需要重新运行。

页面上的流程：

1. **检查**：比较运行中的提交和 `LAUNCHPAD_UPDATE_REPO`（默认 `https://github.com/aws-samples/sample-agentcore-launchpad`）中 `LAUNCHPAD_UPDATE_BRANCH`（默认 `main`）的最新提交。只接受快进：如果主机所在的提交不是该分支的祖先，就无法在线更新，请先手动升级一次到该分支。`console-v3` 合入 `main` 后，默认值从 `console-v3` 改成了 `main`。如果主机的环境里显式设置了 `LAUNCHPAD_UPDATE_BRANCH=console-v3`，它会继续跟随那个分支；删掉这一行（或改成 `main`）并重启后端即可切换。
2. **准备更新**：在 `data/updates/stage-<sha>` 中构建新提交（`uv sync --frozen`、`npm ci`、`npm run build`、导入自检，以及用一次性台账在临时端口上的启动自检）。运行中的控制台不受影响。
3. **排期重启**（一分钟后，或 7 天内的指定时间）会发布公告。**如果重启会中断进行中的任务，页面会列出这些任务，必须先确认才能排期**（否则 API 返回 `update.inflight_unacknowledged`）。到点后，更新器备份台账（`ledger_backup.py --keep 10`），快进检出目录，换入构建好的 `dist`，执行 `uv sync --frozen`，重启单元，并最多等待 120 秒，直到 `/api/auth/status` 返回新的 `version.sha`。如果失败，它会重置到上一个提交，恢复旧的 `dist`，再次重启，并报告 `rolled_back`。
4. **回滚**：准备上一次更新所替换的提交，再像更新一样排期。

未安装更新器时，页面仍可检查和准备，然后显示手工应用暂存目录的命令。完整说明见 [setup.zh-CN.md](setup.zh-CN.md#托管部署) 中“托管部署”的第 4 步。

## 回滚

新版本无法启动或行为异常时，回滚代码：

```bash
cd /home/ubuntu/agentcore_launchpad
git reset --hard "$(cat ~/launchpad-prev-sha)"
cd backend && uv sync --frozen && cd ..
rm -rf frontend/dist && mv frontend/dist.prev frontend/dist      # 或者：cd frontend && npm ci && npm run build
sudo systemctl restart launchpad-backend
git rev-parse HEAD; systemctl is-active launchpad-backend
```

在线更新之前的版本不会返回 `version.sha`；用 `git rev-parse HEAD` 确认提交，用 `systemctl` 和日志确认服务。

**尽量保留迁移后的台账。** 迁移只添加表、列和索引并填充新列，而旧版后端的启动检查只查看它自己的模型需要的列，所以旧版本通常仍能在迁移后的台账上启动（已对 a7f550d 验证：它的 `init_db` 能在项目版本迁移过的台账上正常运行）。新版本写入的数据仍留在台账中，旧代码只是不显示它不认识的内容（`default` 之外的项目、SSO 身份提供商、网络配置）。一个例外：项目版本会把智能体的内联 VPC 网络（子网 + 安全组，只有挂载了 S3 Files / EFS 的智能体才有）改写为对网络配置的引用。旧代码读不懂这种引用，这类智能体在旧版本中可能无法加载。

回滚后再次升级时，只要每个工作区只有一个项目，旧版本写入的、没有项目的行会在启动时被归入该工作区的项目（日志中有 WARNING）。在绑定了多个项目的工作区中，启动会拒绝并列出相关表；请先停止后端，再用 `backend/scripts/backfill_projects.py` 归档这些行（见 [architecture.zh-CN.md](architecture.zh-CN.md#运维)）。

**只在必要时恢复备份**：迁移后的台账已损坏，或旧版本无法使用它，并且你接受丢失备份之后写入的所有数据（新的智能体、运行、用户、审计记录）。服务停止时直接复制是安全的：

```bash
sudo systemctl stop launchpad-backend
mv data/launchpad.db data/launchpad.db.failed-upgrade
cp -a data/launchpad.db.bak-<stamp>-preupgrade data/launchpad.db
sudo systemctl start launchpad-backend
```

如果复制过 `data/sso-secret.key`，请与它所属的台账一起恢复。

## 多实例与多区域

每个部署（例如每个区域一个）都是独立的控制台，有自己的检出目录、台账和工作区，彼此不共享任何东西。请逐个升级：先对第一个完成备份、演练、升级和验证，让它运行一段时间，再升级下一个。每台主机演练一次的成本很低（在 workshop 台账上 `init_db` 远不到一秒），而且能发现只有那份台账才有的数据问题。

## 新增的可选设置

下面的设置都有默认值，升级时一个都不需要设置。每一项都是 `config/launchpad.yaml` 中的键，或单元环境中加 `LAUNCHPAD_` 前缀的大写名称。自 a7f550d 起新增（`backend/app/core/config.py`）：

| 设置 | 默认值 | 作用 |
|---|---|---|
| `update_repo` / `update_branch` | `https://github.com/aws-samples/sample-agentcore-launchpad` / `main` | 在线更新的来源。 |
| `update_enabled` | 未设置 | `false` 关闭在线更新；未设置时，只有安装了更新器的主机才能排期。 |
| `update_min_free_gb` / `update_stage_max_gb` | `3` / `6` | 准备所需的空闲磁盘；暂存目录的大小上限。 |
| `eval_cost_prices` | `{}` | 覆盖评测成本估算使用的单价；未列出的键保持默认值。 |
| `project_inference_profiles` | `false` | 为隔离项目使用按项目的 Bedrock 应用推理配置文件（按项目计费）。 |
| `assistant_digest_model_id` | `""` | 架构助手“已确认事项”摘要使用的模型；为空时使用工作区的默认模型。 |
| `assistant_context_window_tokens`、`assistant_replay_max_tokens`、`assistant_replay_max_turns` | `0`、`200000`、`12` | 架构助手的回放预算。 |
| `model_context_windows` | `{}` | 按模型 ID 子串配置输入上下文窗口，用于价格表中没有该值的模型。 |

仅环境变量：`LAUNCHPAD_SSO_SECRET_KEY`（加密 SSO 客户端密钥的 Fernet 密钥；未设置时创建密钥文件 `data/sso-secret.key`）和 `LAUNCHPAD_PUBLIC_BASE_URL`（浏览器访问地址，单点登录需要）。单点登录、成本标签、网络配置和存储在各自的 **配置** 页面中设置，不在设置项里；见 [setup.zh-CN.md](setup.zh-CN.md) 和 [sso-identity-center.zh-CN.md](sso-identity-center.zh-CN.md)。

## 故障排查

| 现象 | 处理 |
|---|---|
| `git merge --ff-only` 拒绝 | 检出目录有本地提交或修改（`git status`、`git log origin/main..HEAD`）。把它们移走，或在干净的检出目录中切到发布分支；不要用强制重置覆盖你需要的内容。 |
| `uv sync --frozen` 编译 `xmlsec` / `lxml` 失败 | 你的平台没有预编译 wheel：安装 `pkg-config` 以及 `libxml2` / `libxmlsec1` 的开发包（第 5 步）后重试。 |
| `npm ci` 失败 | 检查能否访问 npm 源和 `cdn.sheetjs.com`（`xlsx` 包），以及 Node.js 版本（≥ 20）。`npm ci` 会先删除 `node_modules`；中途失败时重新运行即可。 |
| 重启后页面没有变化 | 跳过了 `npm run build` 或构建失败，`frontend/dist` 仍是旧构建。重新构建，并在浏览器中强制刷新。 |
| 服务起不来 | `sudo journalctl -u launchpad-backend -n 200 --no-pager`，找第一个 `Traceback`。`ImportError` / `ModuleNotFoundError` 表示依赖没有同步：以属主运行 `uv sync --frozen`。`.venv`、`node_modules` 或 `data/` 下的 `Permission denied` 表示某一步用 root 执行过：`sudo chown -R ubuntu: <路径>`。 |
| `ledger schema is behind the models` | 代码需要的某一列没有被 `_migrate` 添加。不要手工修改台账：回滚代码（台账仍可使用），并报告日志中列出的 `ALTER TABLE` 行。 |
| `ledger rows with no workspace_id` / `ledger rows with no project_id` | 启动检查无法归属的行。如果是多项目工作区中的 `project_id`，停止后端后运行 `backend/scripts/backfill_projects.py --workspace <id> --project <id>`。如果是 `workspace_id`，回滚代码并报告。 |
| 其他迁移错误，或演练失败 | 停下。保存日志或演练输出，回滚代码；只有当迁移后的台账无法在旧版本上使用时才恢复备份。 |
| `curl /api/auth/status` 返回旧的 `version.sha` | 进程没有重启，或运行的是另一个检出目录：用 `systemctl cat launchpad-backend` 检查 `WorkingDirectory`。 |

## 参考：2026-10-16 的升级

us-west-2 的 workshop 主机从 `a7f550d` 升级到项目版本（`0c70391`，252 个提交），步骤与上面的手动升级完全相同：在线备份到 `data/launchpad.db.bak-<UTC 时间戳>-preprojects`，在副本上演练（`init_db` 0.53 秒；us-east-1 的台账为 0.62 秒；“OK every row has a workspace, every strict row a project; no drift”），`git fetch` 并检出，`uv sync --frozen`（新增 `python3-saml`、`xmlsec`、`lxml`），`npm ci && npm run build`，`systemctl restart launchpad-backend`，`/api/auth/status` 返回的 `version.sha` 为新提交。
