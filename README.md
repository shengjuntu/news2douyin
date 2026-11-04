# news2douyin 0.21.0 · 持续理解与内容工作台

通过综合、外交与军事冲突线索频道，以及列表、交互事件网络与国家圆形图理解新变化，用专题持续积累知识，在同一内容任务中完成选题、研究、创作和作品交付。

| 主入口 | 用途 |
| --- | --- |
| 事件雷达 `/radar` | 事件、来源、时间和地理脉络；基于知识图的关联与可解释排序 |
| 我的专题 `/focus` | 长期关注、知识纲要、未读变化、缺口和主动补查 |
| 内容工作台 `/workbench` | 选题 → 研究 → 创作 → 作品；材料、引用和版本持续保留 |
| 新闻库 `/articles` | 原始报道、全文检索、来源及不可变原文版本 |

采集配置、执行记录、研究助手设置、知识处理状态与国家资料位于运行管理。

## 安装与升级

Python 3.10+。先停止原服务，备份原数据库、运行目录与配置，然后在原环境安装：

```bash
python -m pip install --upgrade "./release-0.21.0/news2douyin-0.21.0-py3-none-any.whl[research]"
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

在采集配置中新建或选择策略，试跑检查后点击“正式采集入库”，或安排定时采集。支持原有 WorldNewsAPI、GDELT DOC 和 GKG。GDELT 的媒体来源国、地点提及与事件行动地分别处理。

从雷达或专题“形成选题”，进入同一内容任务。选题阶段确定角度与原文，随后选择已有材料写稿、轻量补材或深度研究。研究结束后选择报告版本生成带引用的可编辑判断初稿，再整理脚本、审核、制作与导出。任务的作品阶段集中保存报告、制作包和视频；专题继续跟踪。

[雷达频道、国家关系与文献依据](docs/RADAR-VIEWS.md) · [完整机制、算法边界、旧页面处理与恢复](docs/CORE-WORKFLOW.md) · [图结构契约](src/news2douyin/extraction/graph-schema.yaml) · [历史抽取与综述研究](docs/EVENT-EXTRACTION-SURVEY.md) · [RunDesk 配置](docs/DOCKER-RESEARCH.md)

## 验证

```bash
python -m pip install '.[dev,research]' falkordblite==0.11.0
pytest -q
```

`tests/test_content_foundation.py` 使用真实 FalkorDB 验证关联、更正、专题排除和图重建；未安装验证用 falkordblite 时，该项明确跳过。`tools/check_workbench_live.py` 可启动本地服务并运行浏览器流程，需 Node、playwright-core 和可用 Chromium。研究文本为测试夹具，不能替代部署后的真实模型、RunDesk 和搜索验证。详细结果见 `docs/validation/0.21.0/README.md`。

本轮交互验证可运行 `python tools/check_radar_live.py /tmp/n2d-radar-check`，另需 Node、playwright-core 和 `N2D_QA_CHROMIUM` 指向可用Chromium。测试新闻及抽取均为明确标记的虚构夹具。
