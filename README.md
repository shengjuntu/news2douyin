# news2douyin 0.22.0 · 持续理解与内容工作台

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

原ZIP和所有原始行保存在 `storage_root/intake/` 的档案与DuckDB接收库中，包括无标题、重复URL、非WEB引用和坏行。独立投影线程将可用记录分批送入SQLite新闻版本，进入原有抽取/图/专题流程。采集、投影、抽取完成程度分别显示；没有标题/正文的版本标记“仅元数据，等待原文”。GKG本身不提供文章全文。

在 `/intake` 查看积压、原档校验、失败、暂停/继续，以及事后检索。这里的检索不改变接收范围。旧 `/profiles` 保留定向检索和单批样本测试，新建默认为GKG样本；样本关键词可空，完整接收使用 `/intake`。

存储分工为 SQLite（事务/版本/任务）＋DuckDB 1.4 LTS（批量接收与查询）＋FalkorDB（关系投影）。使用一个应用服务进程；接收器采用进程所有权锁，崩溃后恢复租约。原始采集完整性不受模型处理速度影响，模型慢时显示积压。当前不自动删除原档。

[长期架构与数据模型](docs/INTELLIGENCE-ARCHITECTURE.md) · [外部文档JSON Schema](docs/contracts/intake-document-v1.json)

## 安装与升级

Python 3.10+。先停止原服务，备份原数据库、运行目录与配置，然后在原环境安装：

```bash
python -m pip install --upgrade "./release-0.22.0/news2douyin-0.22.0-py3-none-any.whl[research]"
```

源码安装使用 `python -m pip install '.[research]'`。沿用原有 `--db-url` 与 `--storage-root`，不要建立空库代替原库。升级新增表并建立已有选题的任务关联，保留旧研究、脚本和视频。

## 必要服务与连接

完整知识功能需要 FalkorDB。使用已有实例，或从源码包根目录启动配套服务：

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

不同数据库使用不同图前缀。默认 Compose 仅向本机开放数据库端口，持久卷挂载到 `/var/lib/falkordb/data`。当前部署使用单个应用服务进程。

```bash
news2douyin-server --host 0.0.0.0 --port 18080 --db-url sqlite:///runs_v7/news2douyin_v7.db --storage-root runs_v7
```

打开 `/knowledge` 检查抽取与图分析状态。FalkorDB 未连接时可以查看原文和已有成果，图排序与关联会明确显示未就绪。未启用模型抽取时只有有限规则覆盖。

浏览器视图组件已随安装包本地提供，运行时无需第三方 CDN。国家图只绘制有明确国家角色依据的已报道行动；没有双边依据的材料仍留在列表。虚构示意图需要手动开启，不进入实际数据。

FalkorDB Browser 可在“运行管理 → 图数据库浏览器”配置网页入口。配套 Compose 增加本机 `13000:3000` 端口；远程访问与认证地址配置见 [雷达视图设计与参考文献](docs/RADAR-VIEWS.md)。

深度研究和自动补查通过 `/research/settings` 连接 RunDesk/Codex，外部检索沿用 Exa 搜索票据和预算。图数据库、文本抽取模型和研究代理分别配置。

## 跑通核心流程

先在全量采集接收中心查看GKG两流的接收、投影和抽取状态。需要定向检索或验证单批样本时，再使用采集策略页；原有WorldNewsAPI、DOC和计划保持可用。GDELT媒体来源国、地点提及与事件行动地分别处理。

从雷达或专题“形成选题”，进入同一内容任务。选题阶段确定角度与原文，随后选择已有材料写稿、轻量补材或深度研究。研究结束后选择报告版本生成带引用的可编辑判断初稿，再整理脚本、审核、制作与导出。任务的作品阶段集中保存报告、制作包和视频；专题继续跟踪。

[雷达频道、国家关系与文献依据](docs/RADAR-VIEWS.md) · [完整机制、算法边界、旧页面处理与恢复](docs/CORE-WORKFLOW.md) · [图结构契约](src/news2douyin/extraction/graph-schema.yaml) · [历史抽取与综述研究](docs/EVENT-EXTRACTION-SURVEY.md) · [RunDesk 配置](docs/DOCKER-RESEARCH.md)

## 验证

```bash
python -m pip install '.[dev,research]' falkordblite==0.11.0
pytest -q
```

`tests/test_content_foundation.py` 使用真实 FalkorDB 验证关联、更正、专题排除和图重建；未安装验证用 falkordblite 时，该项明确跳过。`tools/check_workbench_live.py` 可启动本地服务并运行浏览器流程，需 Node、playwright-core 和可用 Chromium。研究文本为测试夹具，不能替代部署后的真实模型、RunDesk 和搜索验证。详细结果见 `docs/validation/0.22.0/README.md`。

本轮交互验证可运行 `python tools/check_radar_live.py /tmp/n2d-radar-check`，另需 Node、playwright-core 和 `N2D_QA_CHROMIUM` 指向可用Chromium。测试新闻及抽取均为明确标记的虚构夹具。

## 网络与外部触角

DOC与完整GKG流共同读取 `NEWS2DOUYIN_GDELT_CONNECT_TIMEOUT=30`、`NEWS2DOUYIN_GDELT_READ_TIMEOUT=90`（秒），保留现场超时修复。GKG遇429遵守Retry-After并保留断点。

其他应用可调用 `POST /api/intake/documents`，使用 `Authorization: Bearer <NEWS2DOUYIN_INTAKE_TOKEN>`。未配置令牌时该入口不可用。标准文档封套保留来源编号、观察时间、发布时间、内容及元数据；新正文形成新版本。官方渠道名保留，第三方应用应使用自己的provider编号。这个入口不会隐式抓取远程网页。

离线测试或只查看旧数据可设置 `NEWS2DOUYIN_GKG_STREAM_ENABLED=0`；独立暂停文章投影可设置 `NEWS2DOUYIN_GKG_PROJECTION_ENABLED=0`。生产默认两者为1。

## 部署与容量

原始ZIP、DuckDB和SQLite工作库都属于持久数据，升级/备份需先停止服务并一起保留原目录。应用暂停不自动清理材料；空间与积压需按真实流量观察。压缩/解压大小保护超限时整批报错，不截断冒充完整接收。

本轮验证使用DuckDB 1.4.5。Linux x86-64预编译wheel标记要求glibc 2.26或更新；CentOS 7可使用提供的 `deploy/Dockerfile`（Python3.12/Bookworm）。Dockerfile本轮未实际构建。Linux容器示例使用host网络，使既有本机FalkorDB/模型地址保持可达：

```bash
docker build -f deploy/Dockerfile -t news2douyin:0.22.0 .
docker run --rm --network host --env-file .env -v "$PWD/runs_v7:/data" news2douyin:0.22.0
```

容器默认数据库为 `/data/news2douyin_v7.db`。升级时必须核对原数据库真实文件名，并覆盖启动参数指向它，避免误连空库。

新增接收链路验证：`python tools/check_intake_live.py /tmp/n2d-intake-qa`，依赖真实DuckDB/FalkorDB与浏览器；上游批次用受控夹具，不宣称已完成公网全量镜像。
