# 检查雷达每天挑出的 100 条

今天的滚动版也可以导出。已发布版的盲评按当次发布的资料截止时刻取样，后台后来收到的消息不会计为该版遗漏。评审包的 `daily-key.json` 记录本次版本与截止时间。

安装包和 Docker 镜像均包含命令 `news2douyin-review`。选择已经结束的北京时间自然日。评审只读 PostgreSQL／SQLite 和 DuckDB。正在采集的部署优先使用事务库和原始采集目录的一致副本，以免多个进程争用线上 DuckDB 文件。

```bash
news2douyin-review export-daily --start 2026-11-01 --end 2026-11-07 --storage-root runs_v7 --outside 20 --out review-week
```

默认读取已发布日报中保存的原始卡片和排序。未发布的日期单独列出，不会触发生成，也不会拿回放卡片冒充当时结果。已经发布的空榜也能抽查当天可恢复的候选。如需从现有材料模拟新版本的选法，另行导出一个目录：

```bash
news2douyin-review export-daily --edition replay --start 2026-11-01 --end 2026-11-07 --storage-root runs_v7 --warmup-days 7 --outside 20 --out replay-week
```

`--db-url` 可指定数据库，缺省时沿用项目数据库配置。回放默认用前 7 天预热近期已选记录。`--limit` 只改变回放条数；已发布日报始终评当时所有入选卡片。固定相同数据库副本、归档、日期和 `--seed` 可以复现抽样。按不同地区和是否有关注度信号的组别优先抽样，因此不能把漏选样本比例直接外推到全部候选。落选总体只包括目前可按日回放、能形成可读卡片、且当日及此前 7 天没有入选的候选组。默认模式从真实已发布日报取历史，回放模式使用同策略的前序清单。报告单列近 7 天已选且未再抽样的组数。缺失或已清理的原始材料、当时未采集的报道均不在这个分母内。

## 在标准 Compose 部署上导出

在项目根目录运行。以下示例假设可读的采集目录副本挂载在 `/data/review-snapshot`，其中保存了 `intake/records.duckdb`。容器默认读取服务配置的 PostgreSQL；若需要逐次复现实验，可用 `--db-url` 指向与该采集副本同时保存的事务库备份。新导出的评审文件位于 `/data/review-week`。

```bash
docker compose --env-file .env -f deploy/production.compose.yaml exec app news2douyin-review export-daily --start 2026-11-01 --end 2026-11-07 --storage-root /data/review-snapshot --out /data/review-week
docker compose --env-file .env -f deploy/production.compose.yaml cp app:/data/review-week ./review-week
```

复制到本机后打开 `review-week/review.html`。本机已安装软件即可用下方命令计分。若只有容器安装，可把下载的评分表传回并计分：

```bash
docker compose --env-file .env -f deploy/production.compose.yaml cp ./ratings.csv app:/data/review-week/ratings.csv
docker compose --env-file .env -f deploy/production.compose.yaml exec app news2douyin-review score-daily /data/review-week --ratings /data/review-week/ratings.csv
```

## 怎么评

把导出目录的 **review.html** 给编辑，双击后离线评。可查找同日其他标题、返回刚才的卡片，重新打开同一份页面可以接着评。也可直接用表格软件填写 **review.csv**。同一天的真实入选与抽样落选混排，页面和这张表都没有公布名单、名次或机器分数。答案在 `daily-key.json`，交给负责统计的人保管。

| 字段 | 填法 |
| --- | --- |
| `important` | 这条材料当天值得花一个阅读名额填 `1`，可跳过填 `0`，未判断留空 |
| `fact_group` | 当天多张卡片描述同一项具体进展，填相同短名。对照当天全部评审卡片并确认独有时填半角减号 `-`，尚未核对留空 |
| `note` | 可选。记录为何值得读、证据缺口或确认重复的原因 |

两个国家各自表决不同的法案，应有不同组名。同一法案的“提交”“通过”“生效”也是不同进展。多家媒体转载同一次表决，即使标题不同，也可填同一组名。组名只在同一天内生效。

## 看结果

页面导出 `ratings.csv` 后计分。直接填写 `review.csv` 时可以省略 `--ratings`。

```bash
news2douyin-review score-daily review-week --ratings ratings.csv
news2douyin-review score-daily review-week --ratings ratings.csv --json
```

按天查看入选条数、其中已评多少条值得读、尚未判断多少条。另列已确认值得读的不同进展数和尚待归组的值得读卡片数。同一组的三张入选卡会计为两个重复占位；还有卡片未核对时，显示已经确认的最低数。已抽看的落选条目另算。值得读且已明确在入选卡中找到同组的算已覆盖；找不到且核对充分的列为确认遗漏并附代表标题和来源；还有可能和未核对的入选卡重复时列为覆盖待核。每项有独立分母，空白标签保留“未知”。

## 对照来源折算前后的名单

在同一份数据库和采集快照上运行以下命令。未打分时即可查看排名变化，填写 `review.csv` 后再次计分即可比较编辑判断。

```bash
news2douyin-review export --start 2026-11-01 --end 2026-11-07 --storage-root runs_v7 --warmup-days 7 --out policy-week
news2douyin-review score policy-week
```

`comparison_033_034` 对比 v0.33 与当前版本。两份清单沿用相同的标题归并规则，各自排除最近七天本策略已经入选的卡片。v0.33 按报道域名计广度，当前版本依据随版提供的媒体归属映射折算。用新版本回放过去某日的新闻时，这个结果反映新规则在旧数据上的选法。统计包括入选材料和位置均相同的席位、材料相同但顺序改变的条数，以及新入选和被替换材料的评审结果。各版本之间共用盲评标识相同的材料，同一条可以只评一次。

多策略导出也保留 v0.31、v0.32 的回放选法和原先按域名计数的两个对照策略。旧评审包可以继续用 `score` 读取，结果仅列出该包实际保存的策略。当时已经发布的日报仍通过 `export-daily` 读取。
