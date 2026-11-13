# 启动时检查图索引

图索引检查由后台图工作线程执行，不在应用构造、Web请求或状态查询中等待大图建索引。图命名空间归属校验成功后，后台读取FalkorDB的索引清单，只补建缺失的属性范围索引。已经存在且正常工作的索引不会重建，也不会删除用户自行建立的索引。

每轮最多发起一个索引创建，然后继续轮询实际状态；`CREATE INDEX`命令返回不代表索引已经可用，只有FalkorDB报告`OPERATIONAL`才视为就绪。该图需要的索引准备好后，才开始其正常投影。全局图和每个专题图分别检查。索引构建期间Web界面仍可查看已有新闻、专题和处理状态。

## 检查范围

全局图检查以下属性：

- `GraphMeta.id`、`Event.id`、`Article.id`、`Entity.id`。
- `EventMention.id`、`Argument.id`、`Referent.id`、`OccurrenceCandidate.id`。
- `Focus.id`、`Place.iso`。

专题图还检查`Question`、`OutlinePoint`、`Investigation`、`ResearchQuestion`、`Claim`、`EvidenceRef`、`Report`、`InterestScope`、`InterestEntity`、`InterestPattern`的`id`属性。只接受节点范围索引；同名关系索引、全文或向量索引不能代替这些精确定位索引。

## 状态与恢复

状态包括检查中、构建中、就绪和错误，显示当前图、当前属性及已就绪数量。状态接口读取缓存，不会因为打开页面而触发图修改。

应用每次启动都重新核对实际索引；运行中每五分钟复核一次，图连接或图重建路径还可以使缓存立即失效。检查失败会保留错误状态并延迟重试，错误信息不回显连接地址、密码或原始查询参数。不要通过清空整张图来处理索引未就绪。

首次对已有大图建立索引仍会消耗图服务的资源。本版将该过程与Web响应分离，并保留单图写入保护；它不承诺索引构建期间其他图查询完全不受影响。索引状态和实际查询计划应共同验证。

## 验证方法

`CALL db.indexes()`列出真实状态。对于属性定位查询，`GRAPH.EXPLAIN`中应出现`Node By Index Scan`等索引计划，而不是标签扫描。`GRAPH.EXPLAIN`不执行查询；`GRAPH.PROFILE`会执行查询中的写入，因此不要把生产写查询的PROFILE当作无副作用的诊断。

参考：[FalkorDB范围索引](https://docs.falkordb.com/cypher/indexing/range-index)、[索引清单过程](https://docs.falkordb.com/cypher/procedures)、[PROFILE命令](https://docs.falkordb.com/commands/graph.profile)。本模块在真实FalkorDB4.22.1上验证了索引清单的字符串格式、异步构建状态与属性查询执行计划。
