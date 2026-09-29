# 0.2.0：可恢复的采集任务

这一版把 WebUI、HTTP API 和定时调度的采集工作交给后台执行器。请求提交后可以立刻返回，任务、进度事件和恢复信息保存在同一个数据库中。保留 0.1.1 的去重归属、筛选、事务和打包修复。

## 使用方式

在仪表盘点击“立即运行”，会进入 `/tasks/{task_id}`。页面显示当前阶段、阶段进度、尝试次数和任务记录，可以取消、重试或查看对应运行结果。关闭页面不停止任务。进度不是时间预估；抓取和筛选阶段可能需要等待网络请求。

```bash
curl -X POST http://127.0.0.1:18080/api/tasks/collect \
  -H 'Content-Type: application/json' \
  -d '{"profile_name":"mock_market","idempotency_key":"my-request-001"}'
```

返回 `202`、任务 JSON 和 `Location`。同一个幂等键及相同请求返回已有任务；同一个键配不同配置名、override 或 max_attempts 返回 `409`。幂等比较基于提交参数；配置文件之后的修改不会改变已有任务的快照。

| 接口 | 行为 |
|---|---|
| `POST /api/tasks/collect` | 提交任务；支持 `override`、`idempotency_key`、`max_attempts`（1–10，默认 3） |
| `GET /api/tasks?limit=50` | 查看最近任务，最多 500 条 |
| `GET /api/tasks/{id}` | 状态、阶段、进度、最新 run_id、错误和尝试次数 |
| `GET /api/tasks/{id}/events?after=0&limit=200` | 持久化事件；返回 items、next_cursor、has_more |
| `GET /api/tasks/{id}/stream` | SSE；支持 `Last-Event-ID` 或 `after`，终态发送 `end` 后关闭 |
| `POST /api/tasks/{id}/cancel` | 排队时直接取消；执行中请求取消 |
| `POST /api/tasks/{id}/retry` | 只允许失败或已取消任务，复用保存进度 |
| `POST /api/collect/run-now` | 兼容旧接口：默认等待成功并返回原 RunRecord 结构 |
| `POST /api/collect/run-now?wait=false` | 提交后立刻返回 202 |

等待接口默认最多等待 290 秒，`timeout` 可指定到 3600 秒。超时返回 `504`，响应中包含 task_id 和查询地址，后台任务继续执行。失败返回 `500`，取消返回 `409`，均包含 task_id。本机执行器未启动时，等待接口返回 `503`；异步入口仍可入队。

Python SDK 增加 `submit_collection`、`get_task`、`list_tasks`、`task_events`、`cancel_task`、`retry_task`、`wait_task`。`Client.run_now` 通过提交和轮询保持原来的运行结果返回结构；SDK 等待超时不会自动取消。已有 Qt 客户端继续使用这个兼容方法。独立 CLI `v7-run-now` 保持同步执行，不创建队列任务。

## 状态与恢复语义

- `queued` → `running` → `succeeded` / `failed`。
- 排队时取消直接进入 `cancelled`；执行中先进入 `cancel_requested`，在安全检查点转为 `cancelled`。
- 失败和取消任务可手动重试。累计 attempts 保留，每次手动重试至少再给 3 次尝试额度。
- 执行器租约过期后，运行记录变为 `interrupted`。仍有尝试额度则自动重新排队；额度耗尽进入 `failed`。已请求取消的任务进入 `cancelled`，不会重启。
- 普通上游错误进入 `failed`，需要手动重试；不会无限自动消耗付费 API。

采集配置在提交时冻结。抓取完成后保存 raw 检查点，筛选完成后保存 filtered 检查点。每条新闻的入库、事件关联、事件计数和任务条目账本在同一个事务中提交。恢复时跳过账本中已完成的条目，并根据整个任务账本重新汇总统计和导出内容，包含前一次尝试已保存的新闻。

每次尝试创建独立 RunRecord 和目录。TaskRecord 的 run_id 指向最新尝试，事件流保留历史 run_id。RunRecord 成功与任务成功在同一个数据库事务中提交。失败、取消和恢复会更新运行元数据；数据库是状态的权威来源，文件与数据库不构成跨介质原子事务。

租约默认 30 秒，后台每 10 秒续租，空闲轮询间隔 0.5 秒。网络请求期间也续租。写入文章或完成任务前，用条件 UPDATE 检查所有权并持有事务锁；旧执行器失去租约后不能提交业务结果。

定时触发的 ScheduleOccurrence、TaskRecord 和首个事件在同一事务中创建。多个调度器对同一 UTC 分钟只入队一次。调度器不再等待采集完成。服务停机期间错过的 cron 分钟不会补跑；已经入队的任务会保留。

## 取消与停机边界

取消是协作式的：抓取前后、LLM 批次和重试之间、逐条新闻入库前、导出完成前检查状态。不会强行终止已在执行的 HTTP 请求，也不会撤销已提交的新闻。取消后的任务可以按原快照续跑；需要重新抓取最新新闻时，请提交新任务。

WorldNewsAPI 设置连接超时 10 秒、读超时默认 60 秒（profile 的 `request_timeout_sec` 可修改）；LLM 筛选单次请求超时为 90 秒，现有重试逻辑保留。读超时不是整个任务的总时限。

正常停机请求在下一安全点中断并重新排队。执行器最多等待 5 秒后让服务退出；若网络调用尚未返回，后续进程会在租约过期后接管。调度器先停止，执行器后停止。

## 升级

1. 停止旧服务，备份 SQLite 数据库和运行目录。
2. 安装 0.2.0，使用原来的 `--db-url` 和 `--storage-root` 启动服务。
3. 启动时自动新增 `taskrecord`、`taskevent`、`taskcheckpoint`、`taskitem`，不修改旧表列，不重写已有新闻和脚本包。
4. 提交 mock 配置，确认任务页完成且原有运行记录仍可打开，再启用实际采集任务。

0.1.1 留下的 `ScheduleOccurrence(status=claimed)` 无法可靠推断是否已产生效果，升级不会自动重放；需要核查对应旧运行记录后按需手动补采。

默认每个服务进程有一个执行器。多实例必须使用同一数据库和可访问的相同存储路径；租约和所有权检查支持并发争抢，但这一版按本地 SQLite、小规模采集设计和验证。尚未验证 PostgreSQL、多机部署或网络文件系统。检查点和事件目前没有自动保留期，需在制定归档策略时一并备份这些表。

## 验证与范围

- 本地 Python 3.12：55 项测试，包括并发入队/争抢、租约过期、心跳、取消、事务回滚、筛选结果复用、部分入库后恢复、定时入队原子性、SSE 游标回放、SDK 和旧 API 兼容。
- 独立 wheel 环境：12 项离线冒烟检查，覆盖任务页面、事件流、异步提交、取消后重试和原有资源/脚本导出。
- CI 继续配置 Linux/Windows、Python 3.10/3.12；本次没有执行远程 CI。
- 未连接真实新闻或模型服务，未做真实浏览器交互验收或吞吐压测。

本次只实现采集任务运行基础。文章版本模型、事件语义聚合改进、全文检索、人工脚本编辑、TTS/视频任务和 Rundesk/Go 前端接入仍是后续工作。服务现有鉴权能力没有变化。
