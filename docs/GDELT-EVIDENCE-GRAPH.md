# GDELT 官方事件的来源图

在官方事件里请求建图或对已存档的官方记录形成选题后，后台把这条记录的来源关系写进已有的 FalkorDB 全局活动图。打开雷达、翻页和读取详情只查询已经收到的资料。图由事务库的官方索引生成，原始 GDELT 文件和完整提及记录保存在采集档案中。

| 从哪里看 | 图里记录什么 |
| --- | --- |
| 官方事件 | `SourceEvent.key` 形如 `gdelt/123`。它保留 GlobalEventID、CAMEO 事件类型、官方事件日期及原始事件所在的 feed、批次、行号。 |
| 行动双方和地点 | `CODED_ACTOR1`、`CODED_ACTOR2` 和 `CODED_ACTION_AT` 沿用 Events 行里的角色和行动地。行动方节点按官方名称和国家代码组合区分。 |
| 相关文档 | 每个精确文档标识对应一个 `GdeltDocument`，允许多项官方行动共同指向一篇报道。每对“官方事件＋文档”的 `MENTIONED_IN` 边留存首次、最近观测及对应原始行，最近一次命中的句子编号、字符偏移和上游置信度也随边保存。 |
| 报道提及的人物、机构 | 精确文档标识能对应到工作库的 GKG 记录时，文档上有 GKG 记录号和可用的批次、行号。人名和机构名通过文档的 `GKG_MENTIONS` 边表示。对某方参与某行动的判断应回到官方角色字段或报道的具体语句。 |

每条入图的官方事件最多保留最近 32 个不同文档，根节点上的 `projected_documents` 和 `has_more_documents` 给出显示数及是否还有更多。每个已匹配的 GKG 文档最多显示 12 个不同的人名或机构名，`has_more_subjects` 记录是否截断。URL 规范化只用于找到候选，精确标识仍需相符。缺少精确匹配时，相关报道和上游 Mentions 证据依然可以查看。

GKG 晚于 Mentions 投影时，系统通过已选文档的索引重新排队。新的官方事件观测或报道提及也会更新已选事件。后台重试、重复入队和全局图丢失重建均沿用同一套来源标识。本模块只为已选官方事件建立文档和角色关系。中文热榜和官方事件有关联时，原有雷达也可以建立共享的 SourceEvent 节点。全量统计应查事务库或 DuckDB。这个视图保留当前关联，研究时若需核对历史状态，应查看原始批次及当期冻结的证据。

在原生图浏览器中选当前部署的 `active_global` 图，把示例编号换成页面上的 GlobalEventID：

```cypher
MATCH (e:SourceEvent {key:'gdelt/123'})-[r]->(n)
RETURN e,r,n LIMIT 40
```

要顺着一篇报道查看它提到的人物和机构：

```cypher
MATCH (e:SourceEvent {key:'gdelt/123'})-[r:MENTIONED_IN]->(d:GdeltDocument)
OPTIONAL MATCH (d)-[g:GKG_MENTIONS]->(n:GkgSubject)
RETURN e,r,d,g,n LIMIT 100
```
