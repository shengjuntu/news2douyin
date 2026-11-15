# 接收归档与存储优化（0.26.0）

原始 GDELT ZIP 是不可变证据，保留 SHA256、MD5、大小和连续行号。DuckDB 是可查询的归档层；PostgreSQL 保存处理台账、紧凑在线目录、编辑/研究状态；FalkorDB 继续作为可重放投影。此次不删除原始 ZIP、不删除旧 SQLite，也不关闭 FalkorDB 持久化。

## 新归档格式

新批次写入新增 `intake_records_v2`：一份精确原始字节 payload，按单行 zlib 压缩后仅在更小时采用，另存 SHA256、原始长度、codec 和 parser_version。标题、URL、实体/主题/地点的紧凑检索文本及验证状态独立保存。完整 `raw_line` 与 `metadata_json` 不再同时物化。

`Lake.rows()` 在有界页内恢复元数据；单行详情仍保留原始行和无效 UTF-8 的 base64 证据。`Lake.raw_bytes()` 可校验/取得精确字节，包括原有换行。列表查询只返回窄列，不解压整页之外的记录，也不把 GCAM/XML 大字段发送到浏览器。新格式搜索范围是标题、URL、人物、机构、主题、地点及官方事件基本字段；不是任意 GCAM 字符串搜索。旧格式的兼容搜索保留原语义。

旧 `intake_records` 保持可读，首次升级只创建新表，**不会在启动时重写现场约 58GB 的旧湖**。新旧批次可以共存。每条记录带解析器版本；未知版本拒绝静默重解释，但精确原始字节仍可取出。

## PostgreSQL 的新写入

新 GKG 投影仅在 `Article.raw_json`、`ArticleVersion.payload_json` 的 raw 部分和 `GkgSource.attributes` 保存实际用于在线处理的字段及归档引用（feed、stamp、row_index、archive_sha256、parser_version）。GCAM、增强列表、扩展 XML 等仍能从不可变归档恢复。

历史不可变版本不改写；外部应用提交的正文和自定义 metadata 保持原有保存方式。已有 PG 大表不会仅因安装新版立即变小。重复采集仍按材料指纹去重，回执与投影游标同事务提交。

官方 Events/Mentions 使用独立紧凑源索引，不为每条原始事件创建本地研究事件或文章。新 GKG 自动发现默认仅保存紧凑文章目录与属性，`NEWS2DOUYIN_GKG_AUTO_EVENTS=0` 时不为未选中的每篇标题创建本地 Event，也不为它们安排模型抽取；新元数据版本标记为 metadata_only。已有选中/关联文章的更正仍刷新，外部应用提交真实材料仍走正常事件链。设为1可恢复旧的逐篇自动事件行为；旧历史与旧任务状态不会被批量改写。GKG、Events、Mentions 三种处理队列公平轮转，防止旧 GKG 积压堵住新来源。

## 对旧湖制作可核验的紧凑副本

先停止使用该目录的接收器和投影服务，保留当前数据库及全部 ZIP。准备足够额外空间容纳输出文件与临时写入，然后执行：

```bash
python tools/compact_intake_lake.py \
  --storage-root runs_v7 \
  --destination /backup/records-compact.duckdb \
  --source-stopped
```

可选 `--chunk-size` 为 1–1000，默认 128 行。工具取得接收器 OS 锁，并以只读方式打开原 DuckDB。它检查每批 ZIP 的 SHA256、MD5、大小、数据成员、行数及连续行号，按原档逐行复制；重新读取目标文件验证原始字节和逻辑记录哈希。旧 metadata 中无法由当前解析器产生的附加字段保存为小型差量 overlay，避免丢掉现场扩展信息。

只有所有批次通过验证后才发布新目标文件；不覆盖已有目标，失败不生成可激活副本。返回 `status=verified`、源/目标文件实际字节数、批次数、记录数和一致的逻辑哈希，且 `activated=false`。**不会自动替换活动库或删除旧库。** 缺失 ZIP、哈希不一致、孤立行、两种格式重复批次等情况会中止，保留原库。

需要启用副本时，保持服务停止，先把原 `runs_v7/intake/records.duckdb` 移到明确的备份位置，再将验证完成的目标文件移到该路径；保留原 ZIP 位置及所有 PG 连接和游标。启动后核对各 feed 的批次/行数、处理位置和单行详情。原库备份应在回放验收和保留策略确认后另外处理。

## 容量口径

报告中 `SUM(length(VARCHAR))` 是逻辑字符数，并非 DuckDB 列实际磁盘占用。DuckDB 已有内部压缩，单行 zlib 的样本压缩率也不能再直接乘在整个旧文件上。空间收益以完成副本后的 `destination_bytes` 实测为准，不能承诺固定回收 28GiB 或 36GiB。复制期间源文件仍保留，因此需要额外磁盘空间，安装版本本身不会释放这部分历史空间。

## 投影忙碌间隔

`NEWS2DOUYIN_GDELT_PROJECTION_INTERVAL` 非空时优先，否则沿用旧 `NEWS2DOUYIN_GKG_PROJECTION_INTERVAL`。两者都未显式设置时，PostgreSQL 忙碌间隔默认0.05秒、SQLite默认1秒；最小允许0.01秒。已有现场显式配置1秒仍保持1秒。需要缩短间隔可设置新别名为0.05；该设置只减少批次之间的固定等待，不保证持续数据流一定追平。空闲等待默认3秒与故障指数退避保持独立。
