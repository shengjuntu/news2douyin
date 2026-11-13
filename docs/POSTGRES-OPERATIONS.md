# PostgreSQL部署、迁移与恢复（0.24）

正式服务使用PostgreSQL保存事务状态、来源回执、新闻版本、抽取状态、研究和作品台账；DuckDB与原始ZIP继续在本地保存全部GKG记录；FalkorDB保存可重建的关系投影。SQLite保留给轻量使用和旧库兼容。这里提供部署与迁移流程，本轮没有实际运行完整Docker Compose或迁移用户现场27GB数据。

## 连接与目录

| 设置 | 用途 |
| --- | --- |
| `NEWS2DOUYIN_DATABASE_URL` | CLI与服务默认事务连接。例：`postgresql+psycopg://user:password@db:5432/news2douyin`，凭证中的特殊字符须URL编码 |
| 显式 `--db-url` | 优先于环境变量；没有显式参数与环境连接时保留SQLite默认值 |
| `NEWS2DOUYIN_POSTGRES_PASSWORD` | 正式Compose内置PG密码；必须填写随机十六进制字符串，避免DSN特殊字符歧义 |
| `NEWS2DOUYIN_DATA_DIR` | 正式Compose挂载的宿主机持久目录，升级时填原 `storage_root` 的绝对路径 |
| `NEWS2DOUYIN_STORAGE_ROOT` | Compose容器内挂载目标及启动参数，默认 `/data`；已有记录引用绝对路径时保持原路径 |
| `NEWS2DOUYIN_FALKOR_URL` / `NEWS2DOUYIN_GRAPH_PREFIX` | 图实例与命名空间；迁移时保留原值，并保证URL从应用容器可达 |

正式Compose强制将事务库连接到其内部 `postgres` 服务，不采用 `.env` 中遗留的SQLite URL。PG镜像固定主版本 `postgres:17-bookworm`，需通过受控拉取更新同主版本补丁；正式部署可进一步锁定已验证digest。PG17数据目录挂载到 `/var/lib/postgresql/data`。升级PG主版本必须按PG官方流程执行，不能仅更改镜像主版本复用数据卷。

PG连接池默认8，允许额外8连接；SQLite文件库默认5+10。两者可用 `NEWS2DOUYIN_DB_POOL_SIZE`、`NEWS2DOUYIN_DB_MAX_OVERFLOW` 调整，留空采用各后端默认；`NEWS2DOUYIN_DB_POOL_TIMEOUT=10` 是获取连接的等待秒数。锁等待默认 `NEWS2DOUYIN_DB_LOCK_TIMEOUT_MS=2000`，语句超时默认 `NEWS2DOUYIN_DB_STATEMENT_TIMEOUT_MS=15000`。SQLite兼容路径启用WAL，忙等待默认 `NEWS2DOUYIN_SQLITE_BUSY_TIMEOUT_MS=30000`。这些等待发生在不同阶段，增大池或超时不能替代解决事务过大、查询无界或磁盘容量不足。命名查询观测和排查步骤见[查询性能运维](QUERY-PERFORMANCE.md)。

投影默认每次最多处理25条，软时间预算250ms，有工作后间隔1秒、空闲间隔3秒，对应 `NEWS2DOUYIN_GKG_PROJECTION_CHUNK`、`NEWS2DOUYIN_GKG_PROJECTION_BUDGET_MS`、`NEWS2DOUYIN_GKG_PROJECTION_INTERVAL` 和 `NEWS2DOUYIN_GKG_PROJECTION_IDLE_INTERVAL`。单条处理可能超过软预算，至少确认一条后按行号继续；这些参数只控制事务与调度粒度，不截断原始接收。后台异常保留内存错误并退避重试，不为保存错误再次抢占故障事务库。

外部图操作使用可续租的SQL租约，默认 `NEWS2DOUYIN_GRAPH_LEASE_SEC=120` 秒（有效范围30–3600秒），避免在等待FalkorDB时持有长SQL事务。租约、原档进程锁和图命名空间所有权各自承担不同责任；已有图的所有权标识随事务库完整迁移保留，不能通过清空它来绕过不匹配。

## 新装正式服务

以下命令从源码包根目录执行，需要Docker Compose。先复制 `.env.example`，填写密码与实际模型、研究服务地址。不要把模型地址保留为容器内部的 `127.0.0.1`；如使用宿主模型可配置可达的 `host.docker.internal` 地址，并确认模型服务监听范围。Compose不提供模型服务。

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
```

将生成的密码写入 `NEWS2DOUYIN_POSTGRES_PASSWORD`，填写宿主持久目录，然后执行：

```bash
docker compose --env-file .env -f deploy/production.compose.yaml up -d --build
docker compose --env-file .env -f deploy/production.compose.yaml ps
docker compose --env-file .env -f deploy/production.compose.yaml logs --tail 100 app
```

PG和FalkorDB不设置宿主端口，仅app可通过内部网络连接。HTTP默认绑定 `127.0.0.1:18080`，需要对外服务时配置已有反向代理与访问控制。图Browser默认没有公开端口；需要人工浏览可使用已有受控Browser入口，保持DB服务端口私有。

应用首次启动初始化空库。检查 `/setup` 显示 `PostgreSQL / psycopg`，再检查 `/intake` 的接收、投影、抽取与错误。app容器健康检查使用 `/api/health` 验证进程响应；PG与图各有独立健康检查。进程健康不证明上游可访问、模型有效或积压已处理，后者以状态页面为准。

`docker compose config` 会展开环境变量，其中可能包含密码，不要把其完整输出保存到公共日志或故障报告。PG已有数据卷的密码不会随 `.env` 变化自动轮换；轮换须同步修改PG角色密码与应用配置。

## SQLite迁移到PostgreSQL

迁移复制事务库，不重写原始ZIP、DuckDB、作品文件或FalkorDB。旧库和新库不能同时写同一个存储目录；目标首次必须是专用空库。不要先启动app初始化目标，再运行首次迁移。

1. 停止旧应用服务，确认采集、投影、抽取、研究和调度线程均已结束。仅点击“暂停接收”不够。记录原数据库真实路径、原 `storage_root`、图地址和图前缀。
2. 备份SQLite及其一致性快照、整个原 `storage_root`、配置和图持久数据。源服务必须保持停止。工具以只读方式读取SQLite，拒绝带非空WAL的源文件；若有未合并WAL，先通过SQLite一致性备份API生成快照，不能直接删除WAL文件。
3. 准备专用空PG库。正式Compose仅先启动PG和图服务并构建app镜像。若沿用原FalkorDB实例，在 `.env` 设置其从容器可达的地址并保留前缀；新装Compose中的空图不能替代原图。已有图卷也必须核对真实卷名与挂载，不依赖目录改变后的默认Compose项目名。
4. 运行迁移，核对返回的逐表行数、全行内容哈希及最终 `status=ready`。可用 `--report-file /backup/sqlite-to-pg.json` 保留核验报告。失败后保留源快照不变，使用 `--resume` 继续；同一目标的断点只适用于同一源身份，不允许换源续传。未完成的目标不能启动正常应用。
5. 迁移完成才启动app，核对新闻数量与固定版本、专题/研究引用、作品路径、接收游标及投影offset。确认图所有权与关系查询正常，最后恢复自动采集/投影。原SQLite和备份保留到核验结束。

原生安装示例：设置 `NEWS2DOUYIN_DATABASE_URL` 指向专用空PG库后运行，目标连接不会写在命令行中。

```bash
news2douyin-migrate-sqlite --source /srv/news2douyin/runs/news2douyin_v7.db --source-stopped --chunk-size 1000
news2douyin-migrate-sqlite --source /srv/news2douyin/runs/news2douyin_v7.db --source-stopped --chunk-size 1000 --resume
```

第二条只在需要恢复失败迁移时执行。也可显式传 `--target-url`；终端历史可能记录连接凭证，因此通常使用环境变量。`--source-stopped` 是操作者对源服务已停止的明确声明，不能代替检查；工具也会检查源的一致性条件。

Compose示例：假设 `.env` 的 `NEWS2DOUYIN_DATA_DIR` 与 `NEWS2DOUYIN_STORAGE_ROOT` 都为 `/srv/news2douyin/runs`，旧库实际文件名为 `news2douyin_v7.db`。路径须按现场值修改，不猜测旧文件名。

```bash
docker compose --env-file .env -f deploy/production.compose.yaml up -d postgres falkordb
docker compose --env-file .env -f deploy/production.compose.yaml build app
docker compose --env-file .env -f deploy/production.compose.yaml run --rm --no-deps app news2douyin-migrate-sqlite --source /srv/news2douyin/runs/news2douyin_v7.db --source-stopped --chunk-size 1000
docker compose --env-file .env -f deploy/production.compose.yaml up -d app
```

若新旧运行环境路径不同，需要保持原路径挂载或单独核验历史文件指针，不能只复制数据库后假定所有作品可读。PG事务库迁移完成也不表示DuckDB已复制到另一台机器；迁移主机时必须完整迁移存储目录。

回滚：停止新app，保留新PG备份与诊断，再恢复原应用版本、原SQLite快照、相同时间点的存储目录和图备份。若新服务已接收新数据，直接切回旧SQLite会丢失此后控制状态，必须先评估并保留新原档；没有自动跨版本反向合并承诺。

## 备份、容量与恢复

DuckDB支持单进程内读写，本项目保持一个应用进程，不运行多个Uvicorn worker，也不将app横向扩容到同一接收目录。PG解决事务层并发，不改变DuckDB文件的进程边界。不要在运行的app之外用另一个进程打开同一DuckDB写库。

一次可恢复备份应同时包含：PG逻辑备份、原始ZIP与DuckDB、作品和其他存储文件、图持久数据、配置与版本。先停app，再按同一备份窗口保存。以下备份目录须事先创建，并使用受限权限：

```bash
docker compose --env-file .env -f deploy/production.compose.yaml stop app
docker compose --env-file .env -f deploy/production.compose.yaml exec -T postgres pg_dump -U news2douyin -d news2douyin -Fc > /backup/news2douyin.pg.dump
tar -C /srv/news2douyin/runs -cpf /backup/news2douyin-files.tar .
docker compose --env-file .env -f deploy/production.compose.yaml stop falkordb
docker compose --env-file .env -f deploy/production.compose.yaml cp falkordb:/var/lib/falkordb/data /backup/news2douyin-graph
docker compose --env-file .env -f deploy/production.compose.yaml start falkordb app
```

若图由外部实例提供，应使用该实例的备份流程替换图备份步骤。恢复先在隔离环境建立专用空PG库并用 `pg_restore --exit-on-error` 导入，再恢复同一时点文件和图，最后启动app核验。应定期演练恢复，不能只检查备份文件是否存在。不要使用 `docker compose down -v` 清理生产服务，`-v` 会删除受管持久卷。

监控应分别看原档空间、PG增长、接收延迟、等待投影、抽取排队、错误与429退避。`/intake` 的原始记录数、投影回执数、文章数并不相等，同一URL的重复观察会合入同一文章；数据库不可读时计数显示未知。原档目前没有自动清理规则，留存周期和分层归档须在实际容量评估后另行实施。

参考：[PostgreSQL版本支持政策](https://www.postgresql.org/support/versioning/)、[PostgreSQL官方镜像及PGDATA约定](https://hub.docker.com/_/postgres/)、[DuckDB 1.4并发模型](https://duckdb.org/docs/1.4/connect/concurrency)。
