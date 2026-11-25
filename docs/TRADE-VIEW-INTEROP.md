# News2Douyin 与 TradeView 互通

两个服务独立运行，各自管理已有数据。事件接口读取 News2Douyin 已整理入库的事件。市场接口读取 TradeView 已存的日线行情。互通采用按需查询，无需迁移或同步数据库。

## 配置

| News2Douyin 环境变量 | 用途 |
| --- | --- |
| `NEWS2DOUYIN_INTEROP_API_KEY` | 事件接口密钥。与 TradeView 中的 `TRADE_VIEW_NEWS2DOUYIN_API_KEY` 一致 |
| `NEWS2DOUYIN_TRADE_VIEW_BASE_URL` | News2Douyin 进程可达的 TradeView 服务根地址，例如 `http://trade-view:8000` |
| `NEWS2DOUYIN_TRADE_VIEW_PUBLIC_URL` | 可选，供读者从已核对的商品打开 TradeView「商品观察」的公开根地址 |
| `NEWS2DOUYIN_TRADE_VIEW_API_KEY` | TradeView 的 `TRADE_VIEW_API_KEY`。由 News2Douyin 服务器保存和发送 |

两个方向可以独立配置。要让 TradeView 查询事件，在 TradeView 设置 `TRADE_VIEW_NEWS2DOUYIN_BASE_URL` 为它可访问的 News2Douyin 服务根地址。

## 核对商品与原文

打开已整理事件详情，在「商品与原文证据」选定报道版本并核对引文，为这条材料确认商品编号。例如铜用 `copper`。同一编号的已核关联可在 `/commodities/copper/events` 查看。这个页面的范围取决于人工核对的覆盖程度。要把新商品接到市场行情，在 TradeView 为同一个编号登记对应的原生序列。

程序可用现有互通密钥请求 `GET /api/v1/interop/product-evidence?product_id=copper`。返回当前有效关联。引文固定到原文版本，资料的公开时间、系统收录和人工确认各有各的时间含义。

配置市场地址和密钥后，任何能访问 News2Douyin 事件界面的用户都可按需查询 TradeView 日线。若界面通过互联网提供，请将界面和行情代理放在同一访问控制之后。容器内的 `127.0.0.1` 指向当前容器。跨主机时请使用 HTTPS 或可信私网。

## 查询已整理事件

`GET /api/v1/interop/news-events?start=2026-11-08&end=2026-11-10&limit=25&q=港口`

请求头为 `X-API-Key: <NEWS2DOUYIN_INTEROP_API_KEY>`。起止日期使用北京时间，包含两端，最多 366 天。关键词可以为空，在事件标题和类别中搜索。默认返回 25 条，最多 100 条。按最早记录时间降序返回，超过上限时 `truncated` 为 `true`，可以缩小日期范围或加关键词。

成功响应的 `data` 包含 `start`、`end`、`timezone`、`time_basis`、`items` 和 `truncated`。每项包括 `event_key`、`title`、`topic`、`first_seen_at`、`source_url` 与 `event_path`。缺少可用原文链接时 `source_url` 为 `null`。`event_path` 相对于 News2Douyin 服务根地址。已被合并的归档事件不会重复返回。

时间窗口依据 `first_seen_at`。这个字段是系统保存的最早记录时间，可随更早报道补充而变化。时间线中有单个经过核对、带证据及明确日期的发生节点时，事件项另附 `reviewed_moment_date`，精度为日。该日期对应这个脉络节点；查询窗口仍使用 `first_seen_at`。

## 查询日线行情

在事件详情展开「同期市场数据」，填写 TradeView 已收录的证券代码和日期。页面按已核对的进展日期或最早记录日期预填前后五天。也可直接查询同一个只读接口。

`GET /api/v1/interop/market-series?symbol=600519.SH&start=2026-11-06&end=2026-11-10`

日期和范围限制与事件查询一致。证券代码使用六位数字，可显式附加 `.SH`、`.SZ` 或 `.BJ`。`data` 包含带交易所的证券代码、日频标识、时区、日期范围、可用状态、数据来源与行情点。行情点包括交易日期、开盘价、最高价、最低价、收盘价、成交量、成交额及来源、复权和单位。源数据缺少的数值返回 `null`；所选窗口无行情时返回空数组。当前可查询的证券取决于 TradeView 的本地数据。

同一时间窗口内出现事件和市场波动只表示时间上的并置。日频数据可以用于选题和核对变化，盘中先后和因果关系需要更细的时间及独立证据。
