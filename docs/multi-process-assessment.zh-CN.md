# Agent 评估与 Skill Lab：是否值得改造成多进程模式

- 分析对象：`v0.0.8`（`77d36b83`，2026-10-25）的 `backend/`
- 分析方式：代码阅读，**未做压测**。文中的并发拐点是推断值，落地前需要实测确认（见第 6 节）。
- 范围：只考虑使用 Agent 评估（`app/evaluation/`）和 Skill Lab（`app/skill_lab/`）两个模块，不使用 Chat。

## 1. 结论

**不建议**把 backend 改成 `uvicorn --workers N` 这类多 web worker 模式。

1. 这两个模块的重活本来就不在请求路径上。评估的打分在 AWS 侧的 batch evaluation 里完成，backend 只负责调用 agent 和轮询结果。Skill Lab 的评估和训练跑在独立的 CLI 子进程里，rollout 交给 AgentCore 上的 `launchpad_skill_lab_worker` runtime 执行。
2. 它们的吞吐上限来自外部配额（每个账号最多 5 个 batch evaluation，CloudWatch Logs 的 API TPS）和本机资源（Skill Lab 子进程占用的 CPU 和内存）。增加 web worker 提高不了这些上限，反而会让配额争用更严重：每个进程各有一个容量为 5 的队列，N 个进程就是 5N。
3. 代价很高。现有代码在很多地方用"进程内存里有没有"来判断"任务是否还活着"，直接开多 worker 会出现重复执行、误杀正在运行的任务、取消失效等正确性问题（见第 4 节）。

**推荐做法**：保留单个 web 进程，先做第 5.1 节的定点优化。确实需要更多后台容量时，再拆出**一个独立的 job runner 进程**（第 5.2 节），而不是增加多个对等的 web worker。

## 2. 两个模块在单进程下的执行模型

### 2.1 Agent 评估

| 环节 | 实现 | 是否占用请求线程池 |
|---|---|---|
| 评估相关路由（`routers.py`、`online_routers.py`、`pipeline_routers.py`） | 全部是同步 `def`，没有 SSE | 是，但多数是短请求 |
| 执行一次 run（`service.execute_run`） | `EvalRunQueue` 的守护线程 | 否 |
| run 列表、队列状态（`GET /runs`、`GET /queue`） | 只查 SQLite | 是，开销很小 |
| run 结果、online 结果、数据处理预览 | 在请求线程里同步调用 CloudWatch | 是，**可能耗时几十秒** |

### 2.2 Skill Lab

| 环节 | 实现 | 是否占用请求线程池 |
|---|---|---|
| 评估、训练、taskgen 任务（`jobs._run_job`） | 队列线程负责 `Popen` 启动 vendored CLI，然后等待它结束（`runner.spawn`） | 否 |
| Rollout 执行 | 在 AgentCore 的 `launchpad_skill_lab_worker` runtime 上运行 | 否 |
| Judge 和优化器 | CLI 子进程直接调用 Bedrock | 否 |
| Task set 校验（`tasksets.py`） | 请求线程里 `subprocess.run`，超时 60 秒 | 是 |
| 资源上传（`task_assets.stage_uploads`） | async 路由，分块写本地文件 | 事件循环上有少量阻塞 IO |

backend 进程本身从不 import vendored 代码（`core/config.py` 中 `skill_lab_python` 处的注释）。Skill Lab 实际上**已经是多进程架构**，只不过子进程由 web 进程启动和管理。

## 3. 单进程下的实际瓶颈

### 3.1 评估：全局只有 5 个槽位，每个 run 从头占到尾

- `evaluation/queue.py` 的 `run_queue` 是**整个进程只有一个**的 FIFO 队列，并发上限 `eval_max_concurrent_runs` 默认 5，最大也只能设到 5，对应 AgentCore 每账号 5 个活跃 batch evaluation 的硬配额。
- 槽位从 `execute_run` 开始一直占到结束。一个 run 在此期间依次做三件事：
  1. 按 dataset 场景**串行**调用 agent，失败时最多重试 2 次（`TRANSIENT_SCENARIO_RETRIES`）；
  2. 用 `_wait_for_fresh_telemetry` 等每个 session 的遥测写完并稳定；
  3. 调用 `StartBatchEvaluation` 后每 30 秒轮询一次，最长等待 `eval_batch_wait_s`（默认 7200 秒）。代码注释记录过一次实测：16 个 session、10 个评估器的 batch 用了 45 分钟。
- 只有第 3 步真正占用 AWS 的配额，第 1、2 步并不占，却也占着槽位。
- 不同 workspace 即使指向不同的 AWS 账号，也共用这 5 个槽位。

**影响**：多人同时提交时，第 6 个人的等待时间等于前面某个 run 的总时长，可能是几十分钟。

### 3.2 评估：CloudWatch Logs 配额（同一账号、同一 region 内共享）

AWS 公告的配额：Logs Insights 并发查询上限 100，`StartQuery` 和 `GetQueryResults` 各为每账号每 region 10 TPS（[AWS Observability ICYMI Jan–May 2026](https://aws.amazon.com/blogs/mt/aws-observability-icymi-jan-may-2026/)）。`GetLogEvents` 和 `FilterLogEvents` 也受较低的 TPS 配额限制（[re:Post](https://repost.aws/knowledge-center/cloudwatch-logs-retrieve-data)）。

| 接口 | 行为 | 多用户时的问题 |
|---|---|---|
| `GET /api/eval/online/{id}/results` → `online.results` | 每次打开都发起约 4 个 Logs Insights 查询，**没有缓存**，同步轮询最长 55 秒（`observability.QUERY_DEADLINE_SECONDS`） | 3 个人同时打开就会超过 `StartQuery` 的 10 TPS。遇到限流只重试一次（等 1.5 秒），之后返回 502。等待期间占着一个请求线程 |
| `GET /api/eval/runs/{id}/results` → `service.run_results` | 每次打开都调用 `GetBatchEvaluation`，再用 `GetLogEvents` 从头翻页读完结果流，**没有缓存**。代码注释也承认终态 batch 的结果流不会再变 | 多人反复查看大 run 时会被限流，页面显示 `unreadable` |
| 数据处理（`pipeline_routers.py`）、遥测等待（`telemetry.py`）、`log_streams.py` | 调用 `run_insights_queries` 或 `filter_log_events` | 和页面请求共用同一份配额 |

### 3.3 Skill Lab：默认同时只跑 1 个任务

- `skill_lab_max_concurrent_jobs` 默认 1，范围 1–4（`core/config.py`）。所有用户、所有 workspace 的评估、训练、taskgen 任务都在一条队列里**串行**执行。
- `docs/setup.md` 说明了上限低的原因：每个任务是一个 CLI 子进程，外加它自己的 worker 会话，并发数是在本机 CPU 和内存、worker runtime 压力与总耗时之间做取舍。
- 所以 Skill Lab 的瓶颈在本机资源和队列配置上，和 web 进程数量无关。

### 3.4 两个模块共有的问题

- **鉴权中间件阻塞事件循环。** 开启登录后，`routers/auth.auth_middleware` 对每个 `/api` 请求都在事件循环上同步调用 `resolve_identity()` 查 SQLite。
- **SQLite 写竞争。** `core/db.py` 没有开启 WAL，也没有设置 `busy_timeout`。评估的 `_update`、Skill Lab 的 `_set_status` 和步骤轮询都会频繁写库，写锁竞争时读请求会变慢。
- **请求线程池。** anyio 默认 40 个线程。55 秒的 Logs Insights 查询和 60 秒的 task set 校验都会占线程，但只用评估和 Skill Lab 时，要几十人同时操作才会把线程池占满，风险中等偏低。

## 4. 如果直接开多 web worker，两个模块会出什么问题

### 4.1 评估

| 位置 | 问题 |
|---|---|
| `queue.run_queue` | 每个进程一个队列，同时活跃的 batch 数变成 5N，超过 AWS 配额，`StartBatchEvaluation` 会失败 |
| `service.resume_interrupted_runs`（启动钩子） | 新启动的 worker 会把其他 worker 正在跑、但 batch 还没开始的 run 标成 failed；已经开始的 batch 会被重复轮询 |
| `pipeline_routers._reap` / `_live_runs` | 请求落到另一个 worker 时，正在运行的 pipeline 会被判为"重启中断"，标成 failed |
| `recommendations._live_provider_jobs` | 同上，provider 推荐任务会被标成 FAILED |
| `EvalRunQueue.cancel` / `_pending` | 取消请求落到另一个 worker 时无法取消排队中的 run |
| `optimization.service.clear_stale_running_actions`、`canary_service.clear_stale_canary_actions` | 新 worker 启动时会清掉其他 worker 正在进行的实验和 canary 操作标志 |

### 4.2 Skill Lab

| 位置 | 问题 |
|---|---|
| `jobs.job_queue` | 每个进程一个队列，同时运行的子进程数变成 N 倍，直接压垮本机资源 |
| `jobs._procs`（`job_id → Popen`） | 取消请求落到另一个 worker 时杀不掉子进程。训练任务的 `_wait` 循环只检查预算，不检查 `cancel_requested` |
| `jobs.sweep_interrupted_jobs`（启动钩子） | 新 worker 启动时会把其他 worker 正在运行的任务全部标成 `interrupted` |
| `tasksets._TASKSET_LOCKS`（`threading.RLock`） | 代码注释写明 "A multi-process deployment must replace this with an inter-process/file lock before sharing DATA_DIR"，否则多进程同时修改同一个 task set 目录会出错 |

## 5. 推荐方案

### 5.1 第一步：保留单进程，做定点优化（推荐优先做）

| # | 改动 | 解决的问题 | 改动量 | 风险 |
|---|---|---|---|---|
| 1 | 终态 run 的结果第一次读取后写入 DB，或用长 TTL 缓存 | 3.2：`GetLogEvents` 限流 | 小 | 低 |
| 2 | Online 结果按 `(config_id, range)` 做 60 秒左右的 TTL 缓存，并对相同 key 合并并发请求。可直接复用 `services/observability.py` 里的 `_CACHE` 加 `_KEY_LOCKS` 写法 | 3.2：`StartQuery` 限流和 502 | 小 | 低 |
| 3 | `StartQuery` 遇到限流时改为指数退避，并加一个进程内全局限速器，控制在 10 TPS 以下 | 3.2 | 小 | 低 |
| 4 | SQLite 用 connect 事件设置 `PRAGMA journal_mode=WAL` 和 `busy_timeout` | 3.4 | 小 | 低 |
| 5 | `auth_middleware` 查身份改用 `run_in_threadpool`，或对身份做短 TTL 缓存 | 3.4 | 小 | 低 |
| 6 | 评估队列只让 batch 阶段（第 3 步）占用 5 个槽位；调用 agent 和等待遥测的阶段另设并发上限 | 3.1 | 中 | 中，需要同步更新 `resume_interrupted_runs` 和 `services/inflight.py` |
| 7 | 评估队列按 `(account, region)` 拆分，每个账号各自 5 个槽位 | 3.1 | 中 | 中 |
| 8 | 根据主机规格把 `skill_lab_max_concurrent_jobs` 调到 2–4，同时监控 CPU、内存和 worker runtime 的限流 | 3.3 | 只改配置 | 低 |
| 9 | 把 anyio 默认线程池上限从 40 调到 100 左右，并做成配置项（见 5.1.1） | 3.4：慢的同步请求占满线程池后，其他请求排队 | 小 | 低，前提是第 2–4 项已经完成 |

第 1–5 项和第 8、9 项改动小、风险低，可以先做。第 9 项要和第 2–4 项一起做或者放在它们之后。第 6、7 项改的是队列模型，需要单独排期并补充测试。

#### 5.1.1 调大线程池

**能解决的问题**：只有一类，即慢的同步请求把 40 个线程占满，导致其他请求排队。这两个模块里的慢请求是：online 结果页的 Logs Insights 查询（最长 55 秒）、run 结果页翻页读取 CloudWatch 日志、task set 校验（`subprocess.run`，最长 60 秒）。

**解决不了的问题，以及会变得更严重的问题**：

| 问题 | 调大线程池后 |
|---|---|
| 评估队列只有 5 个槽位，排队时间长（3.1） | 不变。run 跑在 `EvalRunQueue` 自己的线程里，不使用 anyio 线程池 |
| Skill Lab 并发数为 1（3.3） | 不变，原因同上 |
| CloudWatch `StartQuery` 每账号每 region 10 TPS（3.2） | **更严重**。同时发出的查询更多，限流和 502 会更频繁 |
| SQLite 写竞争（3.4） | **更严重**。同时写库的线程更多 |
| 鉴权中间件在事件循环上查库（3.4） | 不变。这段代码不经过线程池 |
| boto3 每个 client 默认 10 个连接 | 超过 10 个并发后，多出的连接用完就丢弃不复用，TLS 握手开销增加。可以同时给相关 client 设置 `Config(max_pool_connections=...)` |

所以第 9 项不能单独做。单独调大线程池，只是把瓶颈从"在 backend 里排队"挪成"被 AWS 和 SQLite 拒绝"，用户看到的现象会从页面变慢变成报错。

**实现方式**：v0.0.8 的 `main.py` 没有 lifespan。anyio 的默认 limiter 绑定在事件循环上，必须在事件循环启动之后设置，所以要加一个 lifespan（或者 startup 钩子）：

```python
from contextlib import asynccontextmanager

import anyio.to_thread


@asynccontextmanager
async def lifespan(app):
    limiter = anyio.to_thread.current_default_thread_limiter()
    limiter.total_tokens = get_settings().api_thread_limit  # 新配置项，默认 100
    yield


app = FastAPI(..., lifespan=lifespan)
```

建议新增配置项 `api_thread_limit`（环境变量 `LAUNCHPAD_API_THREAD_LIMIT`），并给出合理的上下界，不要写死。对评估和 Skill Lab 的场景，100 已经足够。上限不宜设得过大：线程越多，GIL 争用和内存占用也越高，而这两个模块并发真正受限的是外部配额。改完后用第 6 节的压测确认普通接口的 P95 延迟确实下降，同时 CloudWatch 限流次数没有上升。

### 5.2 第二步（按需）：web 进程加一个独立的 job runner 进程

只有在第一步之后仍然出现以下情况时才需要做：web 进程因为后台任务受到影响（例如重启一次就会中断所有任务），或者后台任务需要独立扩缩容、独立发布。

**目标形态**：

```text
web 进程（1 个）                     job runner 进程（1 个）
- 处理全部 HTTP 请求                 - 唯一执行启动钩子、恢复和清理逻辑
- 只负责写入任务（status=queued）    - 从 DB 认领任务（条件 UPDATE）
- 取消操作只写 cancel_requested      - 运行评估 run 和 Skill Lab 子进程
- 不跑启动钩子，不跑后台线程          - 轮询 cancel_requested，执行取消
```

**需要的改动**：

1. **按角色启动。** 增加 `LAUNCHPAD_ROLE=web|runner|all`，默认 `all`，与当前行为一致。`main.py` 中模块级的 `app = create_app(resume_jobs=True)` 改为根据角色决定是否执行启动钩子。runner 进程不需要 HTTP 服务，可以用一个单独的入口启动。
2. **用 DB 认领任务。** 提交任务时只写一行 `queued`，不直接把任务放进进程内的队列。runner 用 `UPDATE ... WHERE status='queued'` 认领任务，同时写入 `owner_id` 和 `heartbeat_at`。`deployer/pipeline.execute_deploy_job` 已经在用条件 UPDATE 认领，可以照着做。
3. **用 DB 统计槽位。** 评估槽位数改为在 DB 里统计"处于 batch 阶段的 run 数量"，按账号分组，取代 `EvalRunQueue` 在进程内维护的计数。
4. **用租约判断存活。** `_reap`、`_live_runs`、`_live_provider_jobs`、启动扫描都改为以 `heartbeat_at` 是否过期来判断任务是否还活着，不再依赖进程内的集合。
5. **跨进程取消。** Skill Lab 的 `_wait` 对所有任务类型都定期检查 `cancel_requested`，命中后杀掉进程组，替代通过 `_procs` 直接查找进程句柄的做法。
6. **跨进程锁。** `tasksets._TASKSET_LOCKS` 换成基于 `fcntl` 的文件锁。web 进程和 runner 进程都会操作 `DATA_DIR`，所以必须换。

**为什么只要一个 runner**：评估受账号配额限制，Skill Lab 受本机资源限制，增加 runner 数量都不会提高吞吐。一个 runner 还可以继续保留"全局只有一个执行者"这个前提，大部分现有的去重逻辑不需要重写。

### 5.3 不推荐的方案

| 方案 | 不推荐的原因 |
|---|---|
| `uvicorn --workers N` / gunicorn 多 worker | 提高不了外部配额和本机资源的上限，还会引入第 4 节的全部问题 |
| 负载均衡加 sticky session | 一个 uvicorn 的多个 worker 之间做不到 sticky；也解决不了启动钩子和后台线程的重复执行 |
| 引入 Redis 或 Celery 等外部队列 | 对一个 sample 项目来说部署成本过高。SQLite 的条件 UPDATE 已经足以实现认领和租约 |

## 6. 落地前的验证

1. **压测基线。** 用 locust 或 k6 模拟三类负载：N 个用户同时提交评估 run；M 个用户反复打开 run 结果页和 online 结果页；K 个 Skill Lab 任务排队。记录普通接口的 P95 延迟、CloudWatch 限流次数（`ThrottlingException`）、run 的排队时长。
2. **对比验证。** 做完第 5.1 节第 1–5 项后，用同样的负载再跑一遍，确认限流和 502 消失，P95 延迟下降。
3. **Skill Lab 定容。** 逐步把并发从 1 调到 4，观察本机 CPU、内存峰值和 worker runtime 是否被限流，找到合适的设置。
4. **重启行为。** 每次改动队列模型后，都运行 `services/inflight.py` 及其 drift 测试（`tests/test_inflight.py`），确认每种 in-flight 记录在重启后的处理方式仍然正确。

## 7. 什么时候需要重新评估

- 引入 Chat 或公共 `/v1` 的大流量调用，请求线程池成为瓶颈（这属于 web 层的问题，另行分析）；
- 需要多台机器高可用部署（届时需要 Postgres、共享存储，以及第 5.2 节的租约机制）；
- AWS 提高了 batch evaluation 的账号配额，或者 Skill Lab 改成完全在远端执行（不再依赖本机子进程）。
