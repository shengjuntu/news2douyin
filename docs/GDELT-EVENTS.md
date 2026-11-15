# GDELT Events 与 Mentions 接收

0.26.0保留原GKG接收，增加官方Events和Mentions的英文及跨语言翻译流。三类来源互补：GKG描述文档内容特征，Events提供CAMEO行为编码，Mentions关联来源文档与事件记录。接收顺序不代表GKG是Events生成上游。

## 六条流

| 流 | 清单 | 批次ZIP后缀 |
| --- | --- | --- |
| gkg_english | lastupdate.txt | .gkg.csv.zip |
| gkg_translation | lastupdate-translation.txt | .translation.gkg.csv.zip |
| events_english | lastupdate.txt | .export.CSV.zip |
| events_translation | lastupdate-translation.txt | .translation.export.CSV.zip |
| mentions_english | lastupdate.txt | .mentions.CSV.zip |
| mentions_translation | lastupdate-translation.txt | .translation.mentions.CSV.zip |

地址前缀均为`https://data.gdeltproject.org/gdeltv2/`，批次是14位UTC整刻时间。每条流独立保存下一批次、最近完成、错误和退避；首次从发现的最新批次开始，此后按15分钟补齐。失败不推进该流游标，其他流仍可处理。状态页可分别暂停、继续，原始接收不应用选题过滤。

完整ZIP先保存并校验，DuckDB整批归档提交后才确认接收游标。解析异常记录也保留，标记错误而非静默丢弃。向PostgreSQL投影按GKG、Events、Mentions三类轮转，每类内部继续按批次时间处理；已有大量旧GKG积压不会使新官方事件长期得不到索引。

`NEWS2DOUYIN_GDELT_STREAM_ENABLED`和`NEWS2DOUYIN_GDELT_PROJECTION_ENABLED`为新的总开关。留空时分别继承旧`NEWS2DOUYIN_GKG_STREAM_ENABLED`和`NEWS2DOUYIN_GKG_PROJECTION_ENABLED`，已有关闭设置不会因升级被重新打开。两开关分别管理完整接收和后台工作索引。

## 来源索引与本地事件分离

- `OfficialEvent`以原始`GlobalEventID`字符串为身份，保存行为方、对象、CAMEO类型、事件日期、发现/最近提及时间、地理、原始来源及归档行指针。它是外部机器编码记录，不代表人工确认的现实事件。
- `OfficialMention`以`(GlobalEventID, MentionIdentifier)`的哈希为紧凑身份，记录文档标识、域名、首次/最近提及、代表性置信度与位置、首尾原档指针。重复观察同一事件和文档不增加这张表的行数。
- 同一文档多句提及、多次更新及全部原字段仍在不可变原始归档中。紧凑文档索引不冒充完整Mentions原表；代表位置与置信度取最近观察，并保留首次引用。
- 单独收到Mention不会虚构缺失的官方Event或创建本地Event。官方Event晚到时会读取已有文档索引的最近提及时间；后续Mention会推进官方事件的最近提及时间。
- 接收这些来源不会自动生成Article、ArticleVersion、RadarEntry或研究任务。用户确定选题后再按需补充报道材料、创建本地研究对象。

`initial_mentions / initial_sources / initial_articles`来自Events首次更新批次的统计，不是持续累计热度。文档索引的域名数也不等于独立核实来源数。同一通讯社的转载需要在研究阶段进一步核查。

## 编码和时间

- CAMEO事件类型保留前导零；EventCode为2至4位、EventBaseCode为2至3位、EventRootCode为2位字符串。
- ActorCountryCode保留官方CAMEO三字母代码，并仅在能明确对应时提供ISO alpha-2辅助字段。
- ActionGeo_CountryCode保留官方FIPS代码；例如`CH`在这里表示中国，辅助ISO字段为`cn`，不会当作ISO的瑞士。
- `event_date`来自SQLDATE，是事件发生日期；`added_at`来自DATEADDED，是官方首次检测/加入时间；Mention的时间来自其源字段。它们均不应当替代新闻原文的发布时间。
- `GoldsteinScale`依据事件类型赋分，不衡量某一次行动的实际规模，也不代表真实性置信度。
- 空地理、参与者、置信度等保持未知；非有限数字、越界经纬度及损坏日期标记异常。GDELT地理常为估计的地点/行政区中心，不能直接称精确事发坐标。

## 原始规范与本轮验证

规范：[Events/Mentions 2.0 Codebook](https://data.gdeltproject.org/documentation/GDELT-Event_Codebook-V2.0.pdf)。输入为无表头制表符分隔记录，Events 61列，Mentions 16列。完整原记录与解析版本随归档保留。

本轮读取四个官方小样本进行解析验证：英文Events 633行、翻译Events 452行、英文Mentions 2634行、翻译Mentions 728行全部通过。另有隔离SQLite与PostgreSQL16.2/en_US.utf8回归，覆盖字段位置、编码、异常记录、按批次重放、紧凑去重、乱序到达和旧GKG积压下的公平轮转。样本及版本记录见`validation/0.26.0/official-source-samples.json`。

这些检查不代表现场历史积压已补完，也不把代码样本中的机器编码视作已核实新闻事实。

## GKG兼容边界

`NEWS2DOUYIN_GKG_AUTO_EVENTS=0`为新版默认：新GKG自动发现仍保存紧凑文章目录与属性，但未选中标题不逐篇创建本地Event/RadarEntry，也不安排模型抽取。已有关联报道更新继续刷新。显式设为1恢复旧自动事件链；手动采集及外部真实文档保持既有流程。旧历史与旧任务不会自动改写或清空。

官方事件已索引后，无查询参数的`/radar`自动进入全球事件线索；`/radar?source=local`查看旧的已整理事件，原筛选书签保持兼容。新版筛选只影响候选展示，不能改变接收完整性。72小时按最近官方观察时间筛选，事件发生日期单独显示。
