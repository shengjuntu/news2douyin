# 0.54.0

- 历史归档由服务器直接下载。本地中转入口、上传接口和独立客户端下载已移除。
- 慢连接按读空闲时间判断超时。ZIP 断线后保留已收部分，服务端接受 Range 时可继续下载，完整包通过官方长度和 MD5 校验后才进入归档。
- 临时网络故障自动退避重试。归档页显示当前文件字节进度和重试时间。

# 0.52.0

- 焦点图增加「全部 / 人物与组织」切换，同一画布可查看焦点中的人物、组织和有出处的关系。
- 点人物查看活动时间线，点关系核对关联事件和带版本的报道原文。可选择材料日期范围，保留现有人物详情链接。
- 人物与组织视图按需读取现有证据。升级无需迁移数据或重建 FalkorDB。

# 0.51.1

- 归档重试保留下载过程中提交的重试请求，并可恢复旧版停在待下载状态的批次。
- GDELT 返回 429 后，历史归档任务统一等待上游指定的时间。管理页显示恢复时间，重启保留等待状态。

# 0.51.0

- 增加独立的历史归档管理页，按日期倒序下载英文 GDELT 公开档案，支持暂停、继续、重试和覆盖检查。
- 验证后将整字段写入 ZSTD Parquet。区分已下载、已可查询、清单缺档和解析异常。原始 ZIP 在分区提交后按批清理。
- 事件溯源通过 DuckDB 查历史归档。原节点可提交时间窗补档并在完成后续查。

[归档配置与使用](docs/CAUSAL-SEARCH.md#gdelt-历史归档)

# 0.50.0

- 焦点图谱的事件溯源在本地缺少合格的带日期原文时，可以按需查询 GDELT BigQuery 的 Events 和 Mentions。程序核对出版方日期与原文摘录，再把候选并入当前节点的向量重排和逐层搜索。
- 节点显示本地覆盖线索、初筛计数、实际历史查询窗口及核实结果。已有调查可在原节点重新查找。启动时自动创建检索记录表。

[历史补查配置与使用](docs/CAUSAL-SEARCH.md#配置-gdelt-历史补查)

# 0.49.0

- 焦点增加「人物图」，可以查看当前焦点中的人物、组织关系和行动时间线。关系和行动可追到抽取陈述、原文版本及归集事件。
- 关系视图使用跨焦点可复用的身份和证据标识，并标记待核身份、同一行动参与和原文明示的关系。

[焦点人物图使用说明](docs/TOPIC-TRACKING.md#焦点人物图)

# 0.48.0

- 焦点图谱默认由程序逐层查找事件前因。归因模型提出待核假设，扩大本地关键词召回，再用事件向量和证据排序。每层只继续有限分支。
- 在焦点图谱里展开搜索树的各层与分支，查看检索词、候选数、证据和停止理由。搜索结束后可把同一棵树交给 RunDesk 解读。
- 事件溯源支持识别英文原文中的「22 February 2014」日期格式。

[使用方法与模型配置](docs/CAUSAL-SEARCH.md)

# 0.47.0

- 在焦点图谱的「事件溯源」中可指定最早日期，将新建或已有调查交给 RunDesk。Agent 可以逐层查本地事件、读原文证据、记录分支停止理由，并在同一调查中提交报告。
- 沿用已连接的 RunDesk 研究环境，也可为溯源单独绑定运行预设。按需沿用现有的 Exa 外部检索接口。

[查找前因与 RunDesk 配置](docs/CAUSAL-SEARCH.md)

# 0.46.1

- 把事件溯源放入焦点图谱。可用选中的事件节点作根，或在同一张图内输入描述。调查记录按焦点保存并加载。

# 0.46.0

- 事件雷达和已整理事件详情增加「查找前因」。归因模型根据目标生成检索线索并对本地候选排序。
- 保存可续查的调查树。支持手动逐层查找、限宽自动追查，以及在原调查中提交和查看 GDELT 日度补采。

[查找前因与接口说明](docs/CAUSAL-SEARCH.md)

# 0.45.1

- 地缘风险改为事件雷达的独立标签，旧链接保留查询参数并跳转到新入口。
- 后台汇总未启用时，页面显示对应的配置和重启方法。

# 0.45.0

- 已整理事件支持后台生成 BGE-M3 向量。知识处理状态页支持按日期补算、查看进度、重试失败及检索相似事件。
- 补算队列支持重启续跑。向量检索可使用 PostgreSQL 的 pgvector 扩展计算精确余弦距离。

# 0.44.2

- 增加独立的事件归因模型配置。首次运行检查可分别核对默认模型和归因推理模型的地址、鉴权配置及模型列表。

# 0.44.1

- 区分未核对归档、已归档待分析和已确认缺失。全部缺少数值时展示处理状态，保留每日明细。
- 修复 GKG 跨日恢复丢失待发布汇总，以及扩大回看范围时较完整结果被较少批次覆盖。
- 保留事件窗口中已核对的进展日期，修正界面版本号。

# 0.44.0

- 热点增加可选的地缘风险视图，按 GKG 地区、主题和日度批次覆盖查看报道升温及原文。
- 事件详情可以从原文发布时间或已核对进展日调用 Trade View 的交易日窗口，对照日线指标和基准。

[地缘风险与事件窗口](docs/GEORISK.md)

# 0.43.1

- 增加可选的 TradeView 只读互通。按北京时间日期查询已整理事件及来源，或在事件页查询所选证券的同期日线行情。

[互通配置与 API](docs/TRADE-VIEW-INTEROP.md)

# 0.43.0

- 事件雷达增加「热点」页，把近期冲突、重要会议、自然灾害和广泛影响事件归成可追踪的议题，保留报道及来源入口。
- 从热点预填本地焦点规则，可核对关注词再开始跟踪，也可从源报道手动形成选题。

[热点使用说明](docs/HOTSPOTS.md)

# 0.42.1

- 移除独立的「视频候选」自动挑选、生成入口和后台任务。已发布的旧版候选仍可通过历史链接查阅和下载。

# 0.42.0

- 焦点页的事件列表支持按最近收录日或已核对的发生日浏览。候选报道、时间线和研究记录有各自的入口。新建和调整范围时可以预览、修改本地关注规则。
- 增加独立的只读焦点 MCP。Agent 可按日期、国家、分类或分支统计和分页查阅焦点事件，并读取事件关联的本地报道版本。

[焦点与 Agent 接入](docs/FOCUS-MCP.md)

# 0.41.1

- 修复新建焦点时旧脚本与新版页面混用造成的预览错误。焦点脚本按内容版本加载，缺少辅助提示时仍可正常预览和保存。

# 0.41.0

- 雷达新增独立的「视频候选」。按日期从当天的共同候选层生成最多 100 条，优先选择有当事人、具体进展和生活场景的材料，每期可单独导出 Markdown。
- 候选卡可查看有依据的前期线索，并可按该条材料预填一个焦点草稿，持续跟踪后续变化。

[视频候选使用说明](docs/VIDEO-CANDIDATES.md)

# 0.40.2

- 支持配置和独立验收 OpenAI 兼容向量服务，包括 BGE-M3 的批量编码和 1024 维输出检查。
- 向量客户端使用核心安装的 HTTP 依赖。同步和异步调用保持同一种输出格式。模型 ID 留空时按服务公布的唯一 ID 连接。

[向量服务设置](docs/EMBEDDINGS.md)

# 0.40.1

- 图谱浏览按关系类型和方向分批读取邻域，对单节点展开和总读取时间设限。
- 后续批次超时或达到读取预算时，保留已经读到的 FalkorDB 节点和关系。页面分别显示查询超时、等待响应超时及连接失败。
- 图谱浏览的查询、读取预算和套接字超时可独立配置。

# 0.40.0

- 新增「图谱」页面，可切换全局活动图和任意焦点图。每个焦点页可直接打开自己的图。
- 使用随安装包提供的 Cosmos.gl 渲染关系图。可缩放、平移、拖动节点、搜索和按类型查看，点击节点可追到原事件、材料或相关关系。
- 图谱按范围和节点上限读取现有投影，投影不可用时标明关系库数据来源。图形渲染不可用时可继续用列表查阅。

[图谱使用说明](docs/GRAPH-EXPLORER.md)

# 0.39.0

- 统一日报、焦点和人工候选的来源读取接口。将 WorldNewsAPI 等定向信源在相关性筛选前保存为可查候选。
- 为候选增加「整理为新事件」和「加入已有事件」，由服务端核查来源、版本和采用记录。形成选题可以带入候选快照。
- 将各信源的语种代码映射移到统一配置，保留原语言值。日报继续使用现有可插拔 Top N 策略。

[候选与整理流程](docs/CANDIDATE-FLOW.md)

# 0.38.2

- 内容工作台可将选题阶段的项目移入回收站，并从回收站恢复。已进入研究、创作或作品阶段的项目继续在其阶段管理。
- 网页统一使用「焦点」。焦点卡片默认展示标题、时间、分类和操作，收录依据与原文版本按需展开。
- 删减雷达、新闻库、工作台和运行管理页面的重复说明。保留状态、错误、关键来源和必要操作提示。

# 0.38.1

- 移除 PyQt 桌面端和三条桌面启动命令。统一使用 `news2douyin-server` 打开浏览器工作台。
- 移除旧 `news2douyin-platform` 启动别名及独立原型。曾通过该别名启动的实例可执行 `news2douyin-server --host 0.0.0.0 --port 8000 --db-url sqlite:///news2douyin_platform.db --storage-root platform_storage`，或显式传入原数据库及目录。
- 退役离线历史证据试验及 `news2douyin-review compare-evidence`。需要复现实验时使用 v0.38.0。常用日报评审通过 `news2douyin-review export-daily`，同日报的选法对照通过 `news2douyin-review compare-shortlist`。
- 删掉无调用者的去重试验、过时升级种子、旧页面验收脚本和重复包装脚本。历史版本的验收材料可从相应发布包查阅。

# 0.38.0

- 每日精选从当日候选流调用可替换的 Top N 接口。TOML 配置决定策略、条数和参数，正式生成与按日重算使用同一入口。
- 提供 Python 插件接口和可独立安装的实例 `latest-diverse`。每期记录实际使用的策略、算法版本、N 和参数，保留已有日报的读取和导出。
- 现有排序与分组提取为内置实现，多个历史对照策略可以共用一次候选准备。

[配置和开发策略](docs/TOPN-SELECTION.md)

# 0.37.0

- 新增 `news2douyin-review compare-shortlist`。以同一天已发布的名单为基线，在同一可见候选集上分别比较前置分类、同行动归并和组合结果。
- 对照包提供组合版 Markdown、四组进出名单、归并依据和离线盲评材料。正式日报继续使用上一版选法。
- 公开样本回放、误合并复核与规模测试随源码一起提供。

[百条对照与使用方法](docs/SHORTLIST-TRIAL.md)

# 0.36.1

- 每日精选支持下载当期全清单，文件与页面使用同一版次，保留推荐顺序、条目标识、来源和可用材料。由编辑或 Codex 完成后续选题。
- 命令行支持 `news2douyin-daily-radar export --day YYYY-MM-DD --out radar.md`，可供每日制作流程直接读取。
- 百条入选名单仍沿用既有排序，便于以同日样本评估新的筛选方案。

[使用说明](docs/DAILY-BRIEF.md)

# 0.36.0

- 每日精选增加「为什么值得跟」，保存并说明报道中的新行动线索、佐证材料与专题匹配依据。
- 已发布的日报可以下载按日期与版次生成的十件大事 Markdown 编辑简报，区分视频候选与待核线索，保留来源、现有中文译读和核查事项。
- 支持通过命令行将当期简报直接写入指定的 UTF-8 Markdown 文件。
- 增加实验性选题对照，供编辑比较新旧策略入选质量。

[简报与选题验证](docs/DAILY-BRIEF.md)

# 0.35.0

- 每日精选默认打开北京今天，可以按已到数据重算。后台更新期间上一版继续可读，页面显示数据截至时间与版次。
- 日报管理分开显示缺期、GDELT 历史批次缺口和补采进度。缺数据的日期可提交后台修复。
- 增加国家、类别和来源分组、最近时间排序，以及逐条序号和阅读标记。保留国家归属依据与原报道链接。
- 每条日报材料可按需译成中文，复用翻译队列、缓存和模型配置。

[日报操作说明](docs/DAILY-OPERATIONS.md)

# 0.34.0

- 每日精选按已核实的媒体网络计算来源覆盖，保留原始域名与报道链接。可查看哪些域名按同一集团计数。
- 真实回放中，上一版第 100 席的同集团转载降到榜外。另三个预先留出的日期中，两版 300 个名额有 298 条相同。[回放范围与实例](docs/validation/0.34.0/public-validation.md)
- 增加本机新旧名单对照，按日查看入榜、落榜和名次变化。盲评保留 v0.33 策略，已发布日报不重写。[使用说明](docs/PUBLISHER-BREADTH.md)

# 0.33.0

- 对能用台站署名和报道链接核对的转载合并占位，保留可追溯的报道来源。
- 每日精选分别展示关联报道中的行动线索与当日新收录的报道，可展开来源和归并依据。旧日报继续使用当时保存的清单。
- 增加已发布日报的按天盲评，选中卡片与抽样落选卡片混排。离线网页支持暂存与导出评分，按日统计值得读的条目、重复占位和经核对的遗漏。[使用方法](docs/DAILY-REVIEW.md)
- 增加英文 GDELT 行动句的离线诊断，保留每条未通过的原因。真实历史批次的抽样判读与三天日报回放均可复核。

# 0.32.0

- 官方事件详情展示行动编码、来源批次、报道增长与原始链接。按需把官方编码和相关文档投影到全局活动图。
- 接入 gdeltnews 1.1.0 摘取关联报道的重建片段，后台可按预算排队。通过行动词与双方证据校验的材料可进入每日精选。
- 新版日报区分报道增加和报道中的行动线索，可与旧选法对照盲评。[使用和验证](docs/GDELT-EVIDENCE.md)

# 0.31.0

- 增加每天最多 100 条的雷达日报和归档，保留原实时雷达入口。
- 合并可追溯报道，比较传播广度、规则排序和兼顾覆盖的三种清单，支持盲评及落选抽样。
- 支持从未投影到工作库的 GKG 标题发现线索。[开发计划与使用说明](docs/DAILY-RADAR.md)

# 0.30.0

- 全球事件卡片优先显示已保存报道的真实标题、摘要、来源与语种，编码关系作为辅助；缺内容、仅标题与抓取失败分别展示。
- 增加有界后台来源补材和单条／本页请求，列表及详情保持只读，不在页面请求中抓网页或等待模型。
- 中文译读读取当前报道内容；选题冻结当时的标题和摘录，后续仍须取得原文并核查。
- 关联报道按固定长度URL身份批量读取；热点事件提及先按已有时间索引限量，再在候选内排序，避免历史越多页面越慢。

# 0.26.0

## 0.28.0

- 直接通过已有NewsNow的/api/s JSON接口接收多源榜单与快讯，原始响应内容寻址归档，逐轮保留快照和逐源状态。
- 配置页支持来源选择、试跑、正式采集和现有定时任务；明确区分部分失败、全失败与有效空榜。
- 八种日期格式与时间精度，缺出版时间保持未知，摘要和题名作为待取得原文的线索。
- 不逐条自动创建本地事件；真实原文后来补齐时进入原事件链。股票榜只归档。
- 重复快照不增加文章版本，在线限额轮转，任务恢复只保存校验文件指针，避免PG重复大载荷。


## 0.27.0

- 简化全球线索与新闻库，来源、时间、关系图和地点集中到事件侧栏；已选线索优先继续既有选题。
- 语言字段与明确标注的文字推测，按需后台中文译读，固定版本、原文不改写。
- 工作台材料阶段与下一步操作，修复官方元数据快照错误新闻链接，元数据-only不直接写稿。
- 专题回收站、停止自动跟踪、恢复后保持暂停；保留报道与独立任务成果。
- 当前筛选按需限时总量／页数，短缓存注明统计时间，归档与索引进度分开显示。


- 增加Events、Mentions英文／翻译四条流，与GKG组成六流独立接收；完整ZIP与无效记录保留，三类投影公平轮转。
- 官方来源事件与本地研究事件分离，紧凑Mentions索引按事件＋文档去重，保留原始观察与引用。
- 雷达新增全球事件线索，按国家、CAMEO、时间、人物地点、专题词项及已观察媒体域名数筛选，保存筛选方案；选中后原子创建既有内容任务。
- GKG默认仅保存紧凑文章目录，未选材料不逐篇创建本地事件或进行模型抽取；显式兼容开关保留旧流程。
- 事件元数据作为研究线索，不能直接用作正文证据或仅凭它生成短稿；补材／深研究可继续原流程。
- 新湖单份精确载荷按需解码，减少PG新写入大metadata副本；旧湖离线复制工具验证ZIP、连续行号、精确字节与逻辑哈希，保留源文件。
- 正式部署默认数据库配置增加PG保护，保留本地SQLite开发／离线迁移；不自动删除遗留库或关闭图持久化。

# 0.25.2

- 修复PostgreSQL非C排序下投影队列漏取，范围、游标、排序与新后台并发部分索引采用一致C规则，保留旧索引。
- 专题按持久化阶段和游标分批核对、指纹增量写图，批次间让出全局租约；连续新token不重置当前轮，完成仍条件确认捕获token。
- 指纹按页批量读取，检查点/指纹同事务批量UPSERT，避免每项多次SQL往返；支持旧图分批盘点、重启续做、共享来源、关联恢复和显式重建。
- 按事件/专题批次分别统计耗时、图命令数与确认；原知识状态页折叠详情显示分批进度，不增加菜单。
- GC新增独立持久暂停控制，保留游标/周期；分别诊断执行计划、高水位、读取和删除阶段。
- 启动分阶段日志和只读诊断；相同抽取模式重启跳过已成功的历史回填，首次升级仍执行一次。
- 增加真实C/en_US PostgreSQL和FalkorDB回归、迁移检查、浏览器桌面/手机验证，以及可复现专题性能对照。

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

