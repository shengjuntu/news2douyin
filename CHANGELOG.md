# 0.25.1

- 后台启动检查全局及实际使用的专题图属性索引，缺失时补建并等待OPERATIONAL，Web请求不触发建索引。
- 事件投影保留定向stamp剪边，将全图孤儿清理移至独立、有游标、限批维护；共享文章和专题引用受保护。
- 空集合不再发送无效写查询，事件及来源基础更新批量提交。
- 待办部分索引、类型公平轮转和热点游标避免事件积压阻塞专题；指标区分有效确认与过期确认。
- 减少成功后的固定等待，保留空闲与故障退避；全局图租约与单写者保持。
- 图分析使用确认水位而非全量载入队列表；状态页清楚区分投影和分析时间。

# 0.25.0

- 专题新增文字描述入口、可编辑关注范围与知识大纲，详细设置渐进展开。
- 关注实体和关系模式单独投影至专题图，明确区分用户意图与新闻事实。
- 候选经实体、动作关系、词项及有出处的图关联召回，专题级模型判别保存固定版本证据。
- 新建和调整专题只入队；历史回溯按块运行，进度可见，中断可恢复，模型不可用保留待核。
- 人工纳入/排除优先，专题版本和原文版本改变时旧判断不再发布。
- 原关键词专题与SQLite到PostgreSQL迁移保持兼容，继续沿用原档目录和FalkorDB命名空间。

# 0.24.0

- 新增统一的窄列计数与分页查询，状态页使用数据库COUNT，避免仅为计数装载文章、全文或版本快照。
- 雷达SSR先返回预览列表、选题和翻页，浏览器独立调用精确分面API；统计中/失败明确显示并支持重试，不用假0或跨请求TTL缓存代替查询。
- 雷达后端合并分面读取，按已维护的索引版本状态筛选最新结果，减少重复统计和运行时历史版本聚合；原有来源、日期和可见范围保持一致。
- 新增版本、来源与雷达范围共四个读取索引，提供显式维护入口 `news2douyin-db-query-index`；已有库启动不自动补建，默认只输出离线plan，`--apply` 时SQLite须停写、PG并发建索引，配合 `--analyze` 才更新指定索引/表的规划统计。
- 新增当前进程的命名SQL耗时、次数与错误观测，以及只读 `/api/database/queries` 入口；不保存新闻原文、SQL参数或连接凭证。
- 连接池容量和等待分别配置；SQLite文件库默认池5+10、忙等待30秒，PostgreSQL保持8+8，池获取等待默认10秒。
- 补充查询性能排查和升级说明。性能结果以 `docs/validation/0.24.0/` 的实际记录为准；本轮不修改现场27GB库，也不自动删除原档或历史正文。

# 0.23.0

- 雷达、专题、内容工作台读取路径不再同步写入；改为后台发布结果，数据库繁忙时返回可重试状态。
- PostgreSQL按文章、批次、任务等逻辑资源协调事务；队列采用行领取/令牌校验，图租约不持有跨FalkorDB调用的SQL事务。
- 投影使用行数和时间预算，重复观察不重写文章与全文索引；失败使用内存状态和退避，避免为报告锁错误再次争写。
- 正式存储架构采用PostgreSQL事务库、DuckDB原始接收库与FalkorDB关系图，保留SQLite轻量兼容；数据库连接统一读取环境配置。
- 提供停源确认、源一致性检查、分块复制与同源断点恢复的SQLite到PostgreSQL迁移入口，原始ZIP、DuckDB和作品继续沿用原存储目录。
- 新增PostgreSQL 17、FalkorDB、单进程app的正式Compose、健康检查与持久目录；数据库端口不向宿主公开。
- 首次运行和接收中心显示实际事务库类型与驱动，独立显示接收/投影内存错误；数据库故障时不把未知积压显示为零。
- 补充数据库迁移、图实例延续、备份恢复及升级操作说明；未宣称完成现场部署或大规模数据实迁。

# 0.22.0

- 新增默认启用的GKG English/Translingual双流持续接收，独立UTC游标与补档，取消全量路径的检索词和250条截断。
- 完整原ZIP与全部行保留于档案/DuckDB，SQLite保留控制、版本和任务，FalkorDB继续关系分析；不将接收进度冒充抽取完成。
- 校验与幂等重放、失败退避/Retry-After、进程所有权锁及重启租约恢复；独立分块投影保留无标题/坏行，保护已有全文与历史版本。
- 新增全量接收中心、事后检索、原始行详情、积压与暂停恢复；保留旧定向采集策略和计划。
- 增加带令牌的通用文档接入、不可变版本及来源观察封套，供行业站点/其它应用扩展。
- 合并GDELT现场网络修复：连接/读取超时由环境配置，默认30/90秒。
- 补充广度、纵深、专业模型与情景预测的分层发展架构、文档JSON Schema和部署说明。

# 0.21.0

- 雷达新增综合、外交、军事冲突线索频道，统一来源/时间筛选，列表、事件网络、国家圆形图保留同一范围。
- 显示已记录采集渠道与原始媒体、原文及版本；恢复可验证的GKG/DOC/WorldNewsAPI历史路径，正确处理混合渠道。
- 本地打包Cytoscape.js与D3，支持拖拽、缩放、布局、定位、证据侧栏和放大对话框；国家动作类型及方向分别成弦。
- 国家关系只计已报道的显式主体/对象/参与方，保留未解析覆盖统计；可独立开启虚构示意图。
- 增加可配置FalkorDB Browser网页入口、命名图列表和配套端口；补充可视分析文献、边界及复现验证。

# 0.20.0

- 主导航收敛为事件雷达、我的专题、内容工作台、新闻库；采集和运行配置归入辅助入口，删除五个已退出流程的页面模板。
- 一个持久内容任务连接选题、研究、带证据初稿、编辑审核和作品；已有选题与短稿交接自动关联，独立旧研究/脚本可继续纳入。
- 从 FalkorDB 的有据关系计算候选关联、PageRank、相关群组和雷达分级；专题从种子作有限图扩展，人工排除与范围优先。
- 增加专题变化记录、知识点相关材料和优先覆盖缺口的自动复查；研究问题回写持久大纲，任务完成不停止专题。
- 增加图状态、命名空间归属检查、图丢失重建、抽取重试与模型补全；附固定版本 FalkorDB Compose。
- 保留采集结果范围、原文版本、报告与脚本引用；提供真实 FalkorDB、旧 wheel 升级与浏览器验证脚本。
- 升级与边界见 docs/CORE-WORKFLOW.md，验证范围见 docs/validation/0.20.0/README.md。

# 0.19.0

- 报道版本自动抽取、模型按需复核，有证据的动作角色、归属和日期，人工核对可追溯。
- 关联实体、候选匹配与专题问题树进入 FalkorDB 命名图；提供版本化图字典。
- 新增分节检索、冻结综述证据包和原文段落绑定。新增真实 GKG 标题回放。

# 0.18.0

- 新增事件雷达、持久专题、多对多归集、跨事时间线、来源关系和分层地图。
- 接入 GKG 2.1 原语种归档，并可将活动图、每专题的知识图分别投影入 FalkorDB。
- 专题支持后台复查与按截止时间生成有据综述。保留旧事件编号、证据、研究与制作工作流。
- 升级与使用见 docs/DUAL-WORKBENCH.md。

# 0.17.0

- 采集策略新增 GDELT DOC 2.0，可选择来源国家、语言和日期，与既有试跑、正式采集和定时任务共用流程。
- 报道线索保留独立的 GDELT 收录时间；相同网址已有的原文、发布时间和历史版本得到保留。

# 0.16.2

- 国家与地区资源页使用项目模板 `country-reference.html`。顶部导航和采集策略页直接打开该地址，旧资源地址继续可用。

# 0.16.1

- 新增离线国家与地区资源页，提供按英文名称、按洲浏览和中英检索，收录 249 项 ISO 3166-1 代码及首都或行政中心。
- 采集策略页增加查询入口。

# 0.16.0

- 首页升级为选题工作台：跨日期搜索历史报道与事件、固定原文、持久化编辑与选择，不绑定某一天。
- 新增 RunDesk 库内选题会话：一句话开始、候选卡片、带原文引用的回复、连续追问复用会话；采用建议需显式操作。
- 新增 5 个 topic_* MCP 工具，总工具数 13；保存检索快照与原文阅读记录，支持日期、分页、事件关联报道。
- 从选题交接仅用资料写短稿、轻量补材或深入研究；素材报告模式不强制生成脚本。原文版本、参数和交接编号固定，断线/重试沿用原任务。
- 新增6张选题表，旧每日选择、研究、脚本和素材继续读取；复用原执行器、环境准备、回执与停止机制。
- 详见 docs/TOPICS.md 与 docs/validation/0.16.0。真实模型、GPU、Exa 和 Docker 部署需环境联调。

# 0.15.1

- 新增顶部“补充材料”导航；补材任务页提供明显的发起按钮。
- 脚本工作台、每日选题和深入研究页新增补材入口，直达已展开的补材模式。
- `/daily?mode=short#script-builder` 预选补材，筛选、分页和刷新保留模式；快捷切换保留当日选题与筛选条件，仍需明确点击启动。
- 补材搜索、RunDesk 调用、预算、稿件保存及既有任务 API 保持兼容。

# 0.15.0

- Add explicit Chinese/English/Japanese output language and short duration options to daily and evidence-based script generation; preserve source-language extraction without pretending to translate.
- Add a bounded short-material pass using the existing RunDesk/Codex/Exa runtime: up to six selected articles, three external searches and six new source reads, with a five-minute material budget.
- Add `research_save_short_script`, which validates claim ownership and fact/analysis citations and atomically saves an evidence-bound editable draft plus material report.
- Discover image candidates from actual source pages; retrieve up to four bounded public JPEG/PNG/WebP files, show previews/provenance, and include images and an unverified-rights manifest in reviewed exports. Image failures retain text results.
- Preserve idempotency, cancellation fences and immutable evidence. New language/duration requests retain previous drafts. Add the additive ShortScriptBrief table; RunDesk 0.48.0 and its Docker image remain compatible.
- See docs/SHORT-SCRIPTS.md and docs/validation/0.15.0 for upgrade and validation scope. No live model/search or actual container verification is claimed.

# 0.14.0

- Remove direct WorldNewsAPI/Tavily adapters from agent research. Codex / Exa is the sole external research search route; collection and event material gathering remain unchanged.
- Keep the seven MCP tool names. `research_search(web)` reserves a ticket only; `library` searches stored articles without consuming external search budget.
- Deduplicate default/explicit web scope and surrounding query whitespace, reuse recorded outcomes and existing Exa tickets, and serialize concurrent reservations.
- Migrate legacy search settings, preserve credentials/history, and require a fresh run for legacy routes instead of returning cached HTTP 400 errors.
- Check Exa discovery before submission in Docker and local mode; explain HTTP 400 without assuming quota exhaustion. Simplify settings to a single search owner.
- Verify 72 research tests, 4 collection/event search tests and 8 two-service HTTP/MCP checks; Docker, Codex and external search remain fixtures.

# 0.13.3

- Validate WorldNewsAPI search keywords (3–100 characters) before reserving research search budget; preserve longer Exa ticket queries.
- Normalize direct search cutoff timestamps to UTC and serialize WorldNewsAPI timestamps to whole seconds; keep Tavily date-only fields.
- Identify the actual search provider and retain bounded, redacted JSON error details; explicitly label repeated cached failures without automatic retries.
- Display each run's frozen search provider and explain how to switch new runs to RunDesk MCP / Exa.
- Align MCP server version with the package. Research regressions: 71 passed; live paid search and model execution not tested.

# 0.12.0

## 0.13.0

- Add one-click preparation of the application's bound Docker environment before research; require RunDesk 0.47.0.
- Install missing business MCP/Skill in the container during initial setup; preserve administrator edits and deleted capabilities after initialization.
- Check binding, authentication metadata and tool discovery before submitting a model turn; retain durable receipt recovery and guarded stop.
- Reuse active research on duplicate start and persist per-run environment/search configuration.
- Record Exa/Wigolo search tickets and returned leads without treating snippets as original evidence.
- Release the settings lock during network setup to allow business MCP callbacks.
- Preserve local execution for existing settings. Document first-time image/auth/network setup and unverified live-deployment boundaries.


- 应用主动注册并复用旧实例；连接不覆盖后台配置。
- Skills / MCP / 模型集中在 RunDesk 管理；应用只提交业务任务。
- 会话查看入口与默认协作配置简化。
- 详见 docs/APPLICATION-CONNECT.md。

