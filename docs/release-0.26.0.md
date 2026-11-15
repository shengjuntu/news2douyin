# 0.26.0：先发现选题，再补材研究

本版围绕三个改动：增加官方 Events/Mentions 接收、面向选题的过滤、减少归档和在线元数据重复。继续使用 PostgreSQL＋DuckDB＋FalkorDB，保留既有专题、内容任务和创作流程。

## 接收官方事件与报道提及

GKG、Events、Mentions各有英文与跨语言流，共六条独立游标。新流首次默认从发现的最新批次开始，以后按15分钟补齐；不是自动下载全部历史。整批ZIP、有效及无效原始记录保留，失败重试不跳批。完整归档先于在线投影，GKG／Events／Mentions公平轮转，历史GKG积压不会独占投影。

官方事件采用独立OfficialEvent目录，报道提及采用事件＋文档去重的OfficialMention索引；同一文档的所有句子和重复观察仍在原档。CAMEO及外部ID保留为字符串，区分ISO、FIPS与CAMEO国别代码，区分事件日期、官方发现时间、提及时间和原文发布时间。空值保持未知。首次批次NumMentions不是累计报道量，域名数量不是独立证实数量，Goldstein不代表单次事件实际规模或视频价值。

## 事件雷达与选题

原事件雷达增加“全球事件线索”和“已整理事件”。官方目录有数据后，无参数首页自动进入全球线索；旧筛选书签和显式`source=local`继续查看原雷达。

基础筛选为人物/地点/国家词项、国家、CAMEO、时间；折叠选项包括专题词项召回、本站已观察媒体域名数、排序和保存方案。72小时是可调整的默认视窗，按最近官方观察时间计算，事件发生日期单列。专题筛选是轻量词项召回，不声称已经完成专题LLM精筛；关键词不覆盖任意正文/GCAM。列表分页不做全量原文装载或强制精确总数统计。

浏览、筛选及详情不创建本地事件或任务。明确点击形成选题后，在一个SQL事务中保存内容任务、选题工作区、来源绑定和不可变元数据起点；提交编号支持并发幂等，失败回滚不会留下孤立任务。接入已有内容工作台，可继续补材或深研究。元数据线索不冒充报道正文，不可直接作为核实结论的正文证据；只有元数据时不能直接“用已有材料写稿”。

## GKG自动事件边界

新版默认`NEWS2DOUYIN_GKG_AUTO_EVENTS=0`：新GKG发现保存紧凑文章目录和属性，不逐篇创建本地Event/RadarEntry，也不为未选元数据安排模型抽取。已有事件关联材料的更新仍刷新，外部真实文档与手动采集继续原链路。设为1可恢复旧自动事件行为，历史数据和旧任务不会被批量清空或改写。

## 存储

新归档写入`intake_records_v2`，保留单份精确原始载荷、必要时zlib压缩、SHA256、长度、codec、解析版本和紧凑检索字段；有界详情按需恢复metadata。旧表保持可读，启动不重写现场大库。新GKG向PG投影只保存在线必要元数据和可校验原档指针，GCAM/XML等大字段继续完整保存在归档；旧不可变版本不改写。

提供离线整理工具，复制到新文件并核验ZIP、行号、原始字节与逻辑记录。原库和原档保留，输出不自动激活。不能把报告中的字符串长度直接解释成可回收磁盘GiB。实际空间效果由完成副本后的文件大小报告。详见[归档存储说明](INTAKE-STORAGE.md)及[报告口径核对](storage-0.26.0.md)。本版不删除遗留SQLite，不删除事务关系或图边，不关闭FalkorDB持久化。

## 升级

1. 停止服务，备份配置、事务数据库、完整storage_root和FalkorDB持久数据；保留原连接、图命名空间及存储目录。
2. 使用实际服务的Python安装包内wheel：

```bash
python -m pip install --upgrade "./release-0.26.0/news2douyin-0.26.0-py3-none-any.whl[research]"
python -c "import importlib.metadata,news2douyin;print(importlib.metadata.version('news2douyin'));print(news2douyin.__file__)"
```

源码editable部署应完整更新源码和pyproject，再运行`pip install -e .`，避免只合并旧src残留。新版正常初始化会创建新增表，不需要清空历史数据。

3. 新总开关`NEWS2DOUYIN_GDELT_STREAM_ENABLED`、`NEWS2DOUYIN_GDELT_PROJECTION_ENABLED`留空时继承旧GKG开关，已有0不会被升级重新打开。到接收中心核对六条流启用、归档与投影进度。
4. GKG自动事件默认关闭；确需旧行为再明确设`NEWS2DOUYIN_GKG_AUTO_EVENTS=1`。新忙碌投影间隔允许最低0.01秒：`NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL`非空优先，否则保留旧显式间隔。两者均未设置时PG默认0.05秒，SQLite默认1秒；旧.env显式1秒保持。
5. 正式Compose启用`NEWS2DOUYIN_DATABASE_REQUIRE_POSTGRES=1`，缺失/非PG默认配置明确失败，避免误回落到旧SQLite。开发及离线迁移仍支持显式SQLite地址。

### 对已有58GB湖整理

升级后历史文件不会自动缩小。若需要整理，保持服务停止、原ZIP可读取，准备输出所需额外空间：

```bash
python tools/compact_intake_lake.py \
  --storage-root runs_v7 \
  --destination /backup/records-compact.duckdb \
  --source-stopped
```

确认返回`status=verified`及`activated=false`，按[存储说明](INTAKE-STORAGE.md)手动保留原库并切换。此次代码开发没有访问或整理现场数据库。

验证结果见[0.26.0验证记录](validation/0.26.0/README.md)。官方紧凑索引仍随不同事件/文档增长，没有承诺固定PG容量上限。现有全局图分析仍等待本地投影队列清空并使用最近5000个本地事件，尚未改成持续全历史增量图分析；新官方线索过滤独立于这项等待。
