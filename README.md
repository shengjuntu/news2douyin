# news2douyin 0.25.1 · 持续理解与内容工作台

0.25.1将图索引纳入启动后的后台检查，只补缺失索引并等待实际就绪；事件投影移出全图清理，维护采用独立进度和有限批次。投影队列按事件与专题公平轮转，状态页分别显示索引、投影与图分析。既有图和文章版本继续沿用。见[发布说明](docs/release-0.25.1.md)、[索引检查](docs/GRAPH-INDEXES.md)和[验证记录](docs/validation/0.25.1/README.md)。

用一段文字建立长期专题：系统生成可编辑的关注范围、关系模式和知识问题，再在后台回溯已有事件、持续判断新材料。候选按实体、动作关系和词项召回，并可由全局图关联补充；模型精筛区分核心进展、背景和待核，保存原文版本与判断依据。模型不可用时保留待核，不静默丢弃。旧关键词专题仍可使用，高级设置默认折叠。使用和升级见 [专题跟踪说明](docs/TOPIC-TRACKING.md) 与 [0.25 发布说明](docs/release-0.25.0.md)。

通过综合、外交与军事冲突线索频道，以及列表、交互事件网络与国家圆形图理解新变化，用专题持续积累知识，在同一内容任务中完成选题、研究、创作和作品交付。

| 主入口 | 用途 |
| --- | --- |
| 事件雷达 `/radar` | 事件、来源、时间和地理脉络；基于知识图的关联与可解释排序 |
| 我的专题 `/focus` | 长期关注、知识纲要、未读变化、缺口和主动补查 |
| 内容工作台 `/workbench` | 选题 → 研究 → 创作 → 作品；材料、引用和版本持续保留 |
| 新闻库 `/articles` | 原始报道、全文检索、来源及不可变原文版本 |

全量采集接收 `/intake`、定向采集策略、执行记录、研究助手设置、知识处理状态与国家资料位于运行管理。

## 默认全量采集

服务启动后默认持续接收 GDELT GKG English 与 Translingual 两条流，不需要关键词、国家或语言筛选，也没有单批250条截断。各流首次从当前公告的最新批次开始；在尚未开始时可指定UTC历史起点。之后逐15分钟补档，404/429/校验失败保留错误和原游标，不跳过缺口。

原ZIP和所有原始行保存在 `storage_root/intake/` 的档案与DuckDB接收库中，包括无标题、重复URL、非WEB引用和坏行。独立投影线程将可用记录分批送入事务库的新闻版本，进入原有抽取/图/专题流程。采集、投影、抽取完成程度分别显示；没有标题/正文的版本标记“仅元数据，等待原文”。GKG本身不提供文章全文。

在 `/intake` 查看积压、原档校验、失败、暂停/继续，以及事后检索。这里的检索不改变接收范围。旧 `/profiles` 保留定向检索和单批样本测试，新建默认为GKG样本；样本关键词可空，完整接收使用 `/intake`。

正式架构为 PostgreSQL（事务/版本/任务）＋DuckDB 1.4 LTS（批量接收与查询）＋FalkorDB（关系投影）。未配置数据库连接时保留SQLite轻量兼容。使用一个应用服务进程管理本地DuckDB和原档；更换事务库不意味着可以把同一目录交给多个进程写入。接收器采用进程所有权锁，崩溃后恢复租约。模型慢时显示积压，当前不自动删除原档。

[长期架构与数据模型](docs/INTELLIGENCE-ARCHITECTURE.md) · [PostgreSQL部署、迁移与恢复](docs/POSTGRES-OPERATIONS.md) · [外部文档JSON Schema](docs/contracts/intake-document-v1.json)

## 查询与运行状态

0.24 将计数、分面、列表分页与原文读取分开：状态计数直接在数据库执行，列表先选择当前页需要的列，避免为计数或翻页装载全量正文和版本快照。雷达首屏先显示预览列表、选题和翻页入口，浏览器再独立调用 `GET /api/radar/facets` 加载精确统计。统计中显示“…”或“统计中”，失败可重试，不显示假0；每次请求精确查询，没有跨请求TTL缓存。完整正文和不可变版本仍在需要时读取。

`GET /api/database/queries` 仅从当前应用进程内存读取命名查询统计，便于比较雷达、新闻库和状态页的查询次数、游标执行耗时与错误；不记录 SQL、参数、新闻原文或连接凭证。统计随进程重启清零，最多保留128个近期名称，不能代替全局慢查询日志或页面端到端计时。连接池等待、数据库锁等待和 SQL 执行各自有独立配置；增大连接池不能代替缩小查询范围。配置、可选响应头和排查步骤见[查询性能运维](docs/QUERY-PERFORMANCE.md)。

接口还从引擎和连接池对象读取实际数据库类型及池状态，不获取数据库连接。0.24的四个读取索引在新建/完整迁移库建立；已有库须显式维护，普通启动不补建或自动ANALYZE。默认 `news2douyin-db-query-index` 只打印离线plan；确认停写维护窗口与容量后，可用 `--apply --analyze` 建索引并更新指定统计，详见[维护步骤](docs/QUERY-PERFORMANCE.md#已有数据库的读取索引)。

首屏与完整精确统计分别计时。异步读取改变首屏工作量，首屏耗时比值不能直接称为SQL总加速；游标执行时间也不等于浏览器等待时间。

## 安装与升级

Python 3.10+。先停止原服务，备份原数据库、运行目录与配置，然后在原环境安装：

```bash
python -m pip install --upgrade "./release-0.25.1/news2douyin-0.25.1-py3-none-any.whl[research]"
```

源码安装使用 `python -m pip install '.[research]'`。CLI与服务默认读取 `NEWS2DOUYIN_DATABASE_URL`，显式 `--db-url` 优先；两者都未设置时才使用SQLite兼容默认值。`postgresql://` 自动规范为 `postgresql+psycopg://`。原SQLite用户可沿用原启动参数；迁移到PostgreSQL需要先停服务，运行迁移工具，保持原 `storage_root` 和图实例。不要通过连到空库代替迁移。

## 必要服务与连接

新装正式服务从源码包根目录复制并编辑 `.env`，填写随机十六进制的 `NEWS2DOUYIN_POSTGRES_PASSWORD`、模型地址和持久目录，再启动完整服务。旧库升级须先按[迁移步骤](docs/POSTGRES-OPERATIONS.md)导入，最后启动app。

```bash
cp .env.example .env
python -c "import secrets; print(secrets.token_hex(32))"
docker compose --env-file .env -f deploy/production.compose.yaml up -d --build
```

正式Compose内置PostgreSQL 17、FalkorDB和一个应用进程，强制使用内部PostgreSQL；数据库没有宿主端口。HTTP默认仅本机 `127.0.0.1:18080`。DuckDB、ZIP与作品位于app持久目录，PG和图分别使用持久卷；生产镜像升级前应保留一致备份。容器中的 `127.0.0.1` 指向容器自己，模型/研究服务地址必须从app可达。

轻量使用可保留SQLite事务库。完整知识功能仍需要 FalkorDB；使用已有实例，或启动独立图服务：

```bash
docker compose -f deploy/falkordb.compose.yaml up -d
```

复制 `.env.example` 为 `.env`，按实际环境配置：

```dotenv
NEWS2DOUYIN_FALKOR_URL=redis://127.0.0.1:16379
NEWS2DOUYIN_GRAPH_PREFIX=n2d
NEWS2DOUYIN_EVENT_READER=model
OPENAI_BASE_URL=http://你的模型服务:端口/v1
OPENAI_API_KEY=你的密钥
MODEL=你的模型名称
```

独立数据库使用独立图前缀；从SQLite完整迁移到PostgreSQL时保留原图实例和前缀。独立图Compose仅向本机开放图数据库和Browser，持久卷挂载到 `/var/lib/falkordb/data`。正式Compose默认不开放Browser；升级时须明确保留原图地址或迁移旧图卷，不能把新空图当作原图。

```bash
news2douyin-server --host 0.0.0.0 --port 18080 --db-url sqlite:///runs_v7/news2douyin_v7.db --storage-root runs_v7
```

打开 `/knowledge` 检查抽取与图分析状态。FalkorDB 未连接时可以查看原文和已有成果，图排序与关联会明确显示未就绪。未启用模型抽取时只有有限规则覆盖。

`/setup` 和 `/intake` 显示实际事务库类型及驱动，区分PostgreSQL正式模式和SQLite兼容模式。接收与投影后台错误独立显示；事务库不可读时，计数显示未知，仍可看到内存中保留的后台错误。

浏览器视图组件已随安装包本地提供，运行时无需第三方 CDN。国家图只绘制有明确国家角色依据的已报道行动；没有双边依据的材料仍留在列表。虚构示意图需要手动开启，不进入实际数据。

FalkorDB Browser 可在“运行管理 → 图数据库浏览器”配置网页入口。独立图Compose增加本机 `13000:3000` 端口；远程访问与认证地址配置见 [雷达视图设计与参考文献](docs/RADAR-VIEWS.md)。

深度研究和自动补查通过 `/research/settings` 连接 RunDesk/Codex，外部检索沿用 Exa 搜索票据和预算。图数据库、文本抽取模型和研究代理分别配置。

## 跑通核心流程

先在全量采集接收中心查看GKG两流的接收、投影和抽取状态。需要定向检索或验证单批样本时，再使用采集策略页；原有WorldNewsAPI、DOC和计划保持可用。GDELT媒体来源国、地点提及与事件行动地分别处理。

从雷达或专题“形成选题”，进入同一内容任务。选题阶段确定角度与原文，随后选择已有材料写稿、轻量补材或深度研究。研究结束后选择报告版本生成带引用的可编辑判断初稿，再整理脚本、审核、制作与导出。任务的作品阶段集中保存报告、制作包和视频；专题继续跟踪。

[雷达频道、国家关系与文献依据](docs/RADAR-VIEWS.md) · [完整机制、算法边界、旧页面处理与恢复](docs/CORE-WORKFLOW.md) · [图结构契约](src/news2douyin/extraction/graph-schema.yaml) · [历史抽取与综述研究](docs/EVENT-EXTRACTION-SURVEY.md) · [RunDesk 配置](docs/DOCKER-RESEARCH.md)

## 验证

```bash
python -m pip install '.[dev,research]'
pytest -q
```

`tests/test_content_foundation.py` 使用真实 FalkorDB 验证关联、更正、专题排除和图重建；未安装验证用 falkordblite 时，该项明确跳过。`tools/check_workbench_live.py` 可启动本地服务并运行浏览器流程，需 Node、playwright-core 和可用 Chromium；设置 `NEWS2DOUYIN_TEST_POSTGRES_URL` 可对独立测试库验证PG路径。研究文本为测试夹具，不能替代部署后的真实模型、RunDesk 和搜索验证。[0.24验证记录](docs/validation/0.24.0/README.md)包含实际测试与分阶段合成基准；[0.23 记录](docs/validation/0.23.0/README.md)仅代表上一版本。

本轮交互验证可运行 `python tools/check_radar_live.py /tmp/n2d-radar-check`，另需 Node、playwright-core 和 `N2D_QA_CHROMIUM` 指向可用Chromium。测试新闻及抽取均为明确标记的虚构夹具。

## 网络与外部触角

DOC与完整GKG流共同读取 `NEWS2DOUYIN_GDELT_CONNECT_TIMEOUT=30`、`NEWS2DOUYIN_GDELT_READ_TIMEOUT=90`（秒），保留现场超时修复。GKG遇429遵守Retry-After并保留断点。

其他应用可调用 `POST /api/intake/documents`，使用 `Authorization: Bearer <NEWS2DOUYIN_INTAKE_TOKEN>`。未配置令牌时该入口不可用。标准文档封套保留来源编号、观察时间、发布时间、内容及元数据；新正文形成新版本。官方渠道名保留，第三方应用应使用自己的provider编号。这个入口不会隐式抓取远程网页。

离线测试或只查看旧数据可设置 `NEWS2DOUYIN_GKG_STREAM_ENABLED=0`；独立暂停文章投影可设置 `NEWS2DOUYIN_GKG_PROJECTION_ENABLED=0`。生产默认两者为1。

## 部署与容量

事务库、原始ZIP、DuckDB、作品和图卷都属于持久数据。备份与迁移须停应用写入并保留同一时点的数据；仅复制PostgreSQL不能恢复原档和作品。暂停采集不停止全部后台写入。空间与积压需按实际流量观察，大小保护超限时整批报错，不截断冒充完整接收。

正式Compose与Dockerfile已提供；现场27GB库未在本轮修改、迁移或自动清理。离线基准的规模、数据分布与运行环境须与现场区分，实测结果以当前版本验证记录为准。真实迁移前需要容量、备份与恢复演练。PostgreSQL运维、迁移的 `--source-stopped` 确认、断点续传及回滚说明见[操作手册](docs/POSTGRES-OPERATIONS.md)。

新增接收链路验证：`python tools/check_intake_live.py /tmp/n2d-intake-qa`，依赖真实DuckDB/FalkorDB与浏览器；上游批次用受控夹具，不宣称已完成公网全量镜像。
