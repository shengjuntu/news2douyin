# 查找前因

打开「图谱 → 焦点图」并选择一个焦点。可在图谱内打开「事件溯源」直接输入描述，也可选中事件节点并点击「以此事件查前因」。后者会带入标题，已有核对日期可用于查找。文字中只有一个明确日期时，系统可识别日期。日期按系统时区解释。同一天但无法确定先后时，线索会标为待核对。每条结果保留可核对的发生时间、来源公开时间和原文摘录。

## RunDesk 调查

在「时间与查找方式」选择 RunDesk，可指定最早日期。最早日期留空时，从目标日期往前取 90 天。已保存的本地调查可以在原调查中点击「交给 RunDesk」，已有线索会一并提供给 Agent。

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

- `POST /api/causal/investigations` 提交 `{"text":"一段事件描述"}`，可附 `event_at`、`event_key`、`focus_key` 和 `automatic`。使用 RunDesk 时设置 `runner: "agent"`，可附最早日期 `date_from: "YYYY-MM-DD"`。提供焦点时，作为根的事件须属于该焦点。响应返回调查 ID 和队列状态。
- `GET /api/causal/investigations/{key}` 读取已保存的结果树。RunDesk 调查的 `agent` 字段提供本轮状态和研究链接。
- `POST /api/causal/investigations/{key}/agent` 将已有调查交给 RunDesk，或在本轮结束后继续追查。请求体可传 `{"date_from":"YYYY-MM-DD"}`。
- `POST /api/causal/investigations/{key}/expand` 提交 `{"node_key":"..."}`，沿所选节点继续查。提交已查过的节点可更新线索。
- `POST /api/causal/investigations/{key}/archive` 提交 `{"day":"YYYY-MM-DD"}`，发起现有的日度补采任务，任务状态显示在该调查里。
