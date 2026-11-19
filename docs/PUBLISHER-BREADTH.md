# 日报如何计算报道来源的广度

同一则报道可以同时出现在一个广播集团的许多台站网站上。日报保留观测到的域名和报道链接；对归属已核实的网站，排序会把同组域名合成一个单位。未知域名分别作为一个观测单位。两个没有匹配的域名仍可能由同一家公司经营，也可能刊登同一份通讯稿。

本版名录 `2026-11-15.v1` 核实于 2026 年 11 月 15 日。它记录核对当天可以确认的集团和网站。历史数据回放使用该版名录，无法据此还原某一历史日期的产权归属。它也不能判定某篇文章由谁首发、两个编辑部是否各自采访，或同一集团内不同地区的报道有多大差别。

| 已确认的网络 | 纳入的精确主机名 | 核对依据 |
| --- | ---: | --- |
| iHeartMedia | 879 | iHeartMedia 的 46 个州／特区目录页中，878 个不同的单层 `.iheart.com` 台站域名列在其台站官网入口。另有 WCCF 自己的广告页说明其集团归属，FCC 记录可核对呼号及持证公司。[集团台站目录](https://www.iheartmedia.com/stations) · [州目录示例](https://www.iheartmedia.com/stations/?state=CA) · [WCCF](https://wccfradio.iheart.com/advertise/) · [FCC 记录](https://docs.fcc.gov/public/attachments/DOC-404679A1.pdf) |
| NBCUniversal Local | 11 | 将集团发布的 NBC 自有电视台名单中的呼号，与每个地方站官网的联系或介绍页一一对应。范围限于 11 家 NBC 地方站。[集团自有电视台名单](https://together.nbcuni.com/network/nbc-owned-television-stations/) · [纽约站](https://www.nbcnewyork.com/send-feedback/) · [康涅狄格站](https://www.nbcconnecticut.com/contact-us/) |

iHeartRadio 也承载各类节目、用户内容和电台入口，网域后缀本身不能证明台站归属。普通 NBC 加盟台、NBC 其他业务和共享建站平台上的租户同样按具体主机名保留。对未知的 `foo.example.co.uk` 和 `bar.example.co.uk`，不会仅凭可注册域名将二者归在一起。

## 在自己的日报上比较两版

使用安装包或标准 Docker 中的命令。选择已经结束的北京时间日期；程序读取服务的数据库配置和 `NEWS2DOUYIN_STORAGE_ROOT`。也可用 `--db-url` 和 `--storage-root` 指向同一时点的事务库、DuckDB 归档副本。正在采集的实例优先比较归档副本。

```bash
news2douyin-review compare-publishers --start 2026-11-10 --end 2026-11-12 --out sources.html
```

可以先打开包内的[实际回放报告](validation/0.34.0/2025-03-18.html)，查看 3 月 18 日那条五个台站转载的线索。示例只使用所选的英文 GKG 批次。[三个新日期的验证结果](validation/0.34.0/public-validation.md)

打开 `sources.html` 可查看哪些进展新入榜或落榜，以及旧榜与新榜中的位置。榜内换位与已合并但位置不变的卡片可按日展开，标题可回到来源。省略 `--out` 会显示终端摘要；指定新的 `.json` 文件可导出结构化结果。Compose 部署可用 `docker compose --env-file .env -f deploy/production.compose.yaml exec app news2douyin-review compare-publishers` 加上日期、归档副本和输出参数，再用 `docker compose cp` 复制结果。

两套选法使用完全相同的候选，分别记住自己近七天选过的进展。默认预热七天，`--warmup-days 0` 可从空历史回放。某条新闻离榜也可能源于其他卡片的得分变化或前期已读记录。这里比较的是同一份本地资料在两种选法下的结果；查看编辑实际收到的清单可使用[已发布日报盲评](DAILY-REVIEW.md)。

已保存的 GDELT 官方事件统计未必带有新增媒体集团的历史身份。对满足证据与历史完整度条件的本期新增报道，排序最多给一次这类新鲜度加分。卡片仍可查看真实报道篇数、原始域名数和可用的来源链接。

## 如何核对名录

州页面的台站网站取自带有 `station-link websitelink` 两个类和台站呼号标题的链接；仅接受站点实际链接中形如 `<单个标签>.iheart.com` 的主机名。集团首页和非台站资产不参与匹配。该目录以外目前仅有逐站核实的 WCCF。NBC 的精确网址则逐台核验。正常的大小写、`www.`、尾部 DNS 根点和有效端口均会规范化。多出来一层子域或相似的域名保持未匹配。

每个州的 URL、当日响应 SHA-256、按主机名升序换行编码的清单 SHA-256、NBC 各台的呼号与来源页均保存在[名录核对记录](validation/0.34.0/publisher-directory.json)。在线目录会变化，因此此处的计数与哈希对应随版本分发的快照。排名时使用软件中打包的精确清单，页面请求和日报任务均无需访问这些站点。
