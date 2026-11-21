# 查找前因

打开「图谱 → 焦点图」并选择一个焦点。可在图谱内打开「事件溯源」直接输入描述，也可选中事件节点并点击「以此事件查前因」。后者会带入标题，已有核对日期可用于查找。文字中只有一个明确日期时，系统可识别日期。日期按系统时区解释。同一天但无法确定先后时，线索会标为待核对。每条结果保留可核对的发生时间、来源公开时间和原文摘录。

## 程序逐层查找

焦点图谱默认使用「程序逐层查找」。根事件需要已核对的发生日期，可以在表单填写，也可以使用选中事件或文字中唯一明确的日期。可指定最早查找日期，留空时从根日期往前取 90 天。

程序对每个节点调用独立配置的归因模型，生成可核对的前因假设。关键词扩大对本地事件、报道和有本地来源的 GDELT 线索的召回范围。配置向量后，用事件向量和假设向量对齐候选，再结合时间与原文证据排序。关键词检索与向量匹配的分数只用于筛选，树里的「可能前因」仍是待审核的候选解释。

每层最多继续 3 个有日期和原文的分支，每棵树最多查找 18 个节点。任务按节点保存在现有队列，页面可以折叠或展开各层，查看假设、候选数、证据和停止理由。指定窗口没有找到候选时，该分支停在当前节点。可先核对 GDELT 归档，按需提交补采，再重查相应分支。

## RunDesk 解读

等程序树搜索结束后，在同一调查点击「交给 RunDesk 解读」。Agent 从已保存的树和原文摘录生成有证据引用的判断和报告。需要完成历史搜索的程序分支和报告中的资料缺口可在同一调查核对。

### 既有 RunDesk 调查

旧 RunDesk 调查仍可继续使用原搜索方式。手动建立的本地调查也可以点击「交给 RunDesk」，已有线索会一并提供给 Agent。

Agent 可按父事件生成检索词，在日期窗口内检索本地候选，读取来源，并把有依据的候选和停止理由保存在原调查树中。保存候选时可将本地原文摘录用于报告引用。每轮最多提交 12 次本地检索，每个节点最多 3 次，每层最多扩展 3 个节点。有确切历史发生日期的回顾性报道可以提供下一轮检索的时间锚点。报道晚于父事件时，树上保留其背景或文献关系。页面显示当前任务状态和报告入口。

在「研究设置」连接 RunDesk，默认复用已连接的研究环境。选用专门模型与 MCP 组合时，可在 RunDesk 配置并授权 News2Douyin 的运行预设，再将预设编号填入 `NEWS2DOUYIN_CAUSAL_RUN_PRESET`。需要手动指定绑定时，同时设置 `NEWS2DOUYIN_CAUSAL_RUN_INSTANCE_ID` 和 `NEWS2DOUYIN_CAUSAL_RUN_WORKSPACE_ID`。更新 News2Douyin 后刷新所选环境对 `news2douyin-research` MCP 的工具发现。

| 用途 | MCP 工具 |
| --- | --- |
| 读取和更新调查树 | `causal_get_investigation`、`causal_search_events`、`causal_save_step` |
| 查证与交付 | `research_get_case`、`research_read_source`、`research_save_claim`、`research_save_report` |
| 可选外部补材 | `research_search`、`research_record_search`、Exa 的 `web_search_exa` |

需要超出本地档案范围的资料时，Agent 可通过现有 Exa 搜索票据补材，或在报告中记录资料缺口。GDELT BigQuery 可作为后续独立检索源接入。

## 本地逐层查找

手动查找先给一层候选，可沿任意线索往前查。选择自动追查时，系统按归因模型的证据排序，最多沿三个有较早公开证据的分支再查一层。调查和候选解释保存在当前焦点。可以回到同一调查继续追或重新查找，模型分数仅用于排序。

### 检索和补采

本地检索使用已整理事件、已入库的报道，以及能关联本地报道或出版者快照的 GDELT 编码线索。检索窗口上限为目标时间之前 90 天。没有向量服务时仍可用本地关键词检索。调查和待处理任务存入现有关系库，任务运行时释放数据库连接。

可从同一调查提交指定北京时间日期的 GDELT 补采。沿用「日报管理」的历史补采和重试队列，补采可取得上游仍提供的原始批次。补采及本地投影完成后，在调查中重新查相应分支。需要额外来源时，可以从结果事件进入已有的深度研究流程。

### 归因模型

设置 `NEWS2DOUYIN_ATTRIBUTION_BASE_URL`、`NEWS2DOUYIN_ATTRIBUTION_MODEL` 和所需的 `NEWS2DOUYIN_ATTRIBUTION_API_KEY` 后重启应用。在 `/setup` 可核对服务公布的模型名称。本地逐层查找使用归因模型生成检索问题和排序。两种方式均可通过 `EMBEDDING_*` 启用向量召回。

## 调用接口

- `POST /api/causal/investigations` 提交 `{"text":"一段事件描述","runner":"program","event_at":"2022-02-24","date_from":"1989-01-01"}` 启动程序搜索。可附 `event_key` 和 `focus_key`，提供焦点时作为根的事件须属于该焦点。兼容已有的 `runner: "local"` 和 `runner: "agent"`。
- `GET /api/causal/investigations/{key}` 读取已保存的结果树。RunDesk 调查的 `agent` 字段提供本轮状态和研究链接。
- `POST /api/causal/investigations/{key}/agent` 将已经结束的程序搜索交给 RunDesk 解读，请求体传 `{}`。旧调查也可使用这一接口继续追查，按需传 `{"date_from":"YYYY-MM-DD"}`。
- `POST /api/causal/investigations/{key}/expand` 提交 `{"node_key":"..."}`，沿所选节点继续查。提交已查过的节点可更新线索。
- `POST /api/causal/investigations/{key}/archive` 提交 `{"day":"YYYY-MM-DD"}`，发起现有的日度补采任务，任务状态显示在该调查里。
