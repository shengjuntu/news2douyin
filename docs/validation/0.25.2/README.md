# 0.25.2 验证记录

本轮均在隔离本地环境执行，未连接或修改现场数据库。PostgreSQL实际版本16.2，locale分别为C与en_US.utf8；实际FalkorDB4.22.1。新增CI配置覆盖PostgreSQL17的两种locale，但本轮没有在GitHub运行该CI，不能称现场PG17已验收。

## 回归与安装

| 检查 | 实际结果 |
| --- | --- |
| 全量pytest（默认SQLite，真实Falkor可用） | 646 passed、67 skipped、1 failed，440.07秒；唯一失败为原有0.9秒租约心跳测试，见下文 |
| 失败所属完整任务组独立复核 | tests/test_tasks.py：21 passed，10.52秒；没有修改断言或任务代码 |
| 原心跳用例五个独立进程复核 | 5次均通过，未复现；单次过期原因仍未证明 |
| 专题分批与关联恢复，批量SQL优化后 | 16 passed，54.73秒，真实Falkor |
| PostgreSQL C核心矩阵 | 66 passed、1 skipped，75.50秒 |
| PostgreSQL en_US.utf8核心矩阵 | 66 passed、1 skipped，86.13秒 |
| en_US.utf8公开专题行为＋队列＋迁移 | 28 passed，32.34秒 |
| 批量SQL优化后的en_US.utf8专题＋迁移 | 8 passed，35.11秒，包括stamp兼容断言 |
| 真实Chromium153桌面与手机 | 全部通过，见graph-maintenance-browser.json |
| 独立wheel安装与离线smoke | 通过；6个核心HTTP入口、3个新增表、资源和18项原有安装流程检查 |

这些检查范围有重叠，不相加成独立用例数量。PostgreSQL矩阵唯一跳过项是捆绑QA服务没有pg_trgm扩展的用例，队列、图投影与新检查点迁移实际执行。默认全量测试中未配置PG的用例及可选依赖用例跳过，另由真实PG矩阵覆盖关键路径。

全量检查的心跳用例在0.9秒租约、提供者阻塞1.2秒的条件下出现一次过期恢复；独立重跑完整任务组21项全部通过，随后该用例在5个独立进程中也均通过；单次过期原因仍未证明。该次失败不隐藏、不计作全量检查全过；本版未修改任务执行/心跳代码。图相关用例没有该失败，后续现场仍需观察真实负载下的租约和队列指标。

首次广覆盖运行是在核心实现仍变化时开始，曾捕获SQLModel Row写入游标的问题；修复后公开图流程、上述全量运行及批量SQL回归均覆盖了修正路径。所有发布结论以最终代码和这里列出的后续检查为准。

独立安装检查使用新虚拟环境安装实际0.25.2 wheel，复用QA依赖并补齐dev/assets/llm/research可选依赖；`python -I`从安装目录导入，不使用checkout源码。离线覆盖页面、模拟采集、任务取消/重试、脚本编辑审核导出与还原、提示词/静态资源、旧LLM模块及CLI入口；无外部模型/采集服务调用。见[installed-wheel.json](installed-wheel.json)。

## 关键正确性

- 实际en_US.utf8下重现旧`event/evt_intake_a < event0`为false；新队列正常取到任务，C排序查询及新索引保持一致。
- 2万待办事件＋1专题检查实际执行计划Index Cond及过滤行数，专题不依靠遍历事件前缀才能定位。
- 专题批次之间允许新事件执行；持续token变化不归零游标；重启和图写入后中断能续做/重放。
- 更正只重写变化材料，排除/重新纳入成员以及问题/声明删除后恢复关联正确；共享文章与行动仍被有效事件引用。
- 旧图六类业务节点以原生ID窗口盘点，真实Falkor的labels序列化由服务器CASE规避；范围变更、单独专题图丢失及全局图丢失均能恢复。
- 明确重建使用持久代次标记，修复SQL未变但图CITES损坏的情况。
- SQLite→PG逐表哈希验证保留新表组合键、Unicode、token、代次、游标和2**40级计数。
- GC暂停重启后仍有效，保留游标/周期和待办；恢复不伪造完成。分开诊断plan/highwater/read/delete超时。
- HTTP状态与控制不访问图、不建索引；单连接池读取可用。图线程未就绪时HTTP健康可用，启动错误只记录阶段和异常类型。

## 专题性能对照

可复现工具：`tools/benchmark_topic_projection.py`。真实PG16.2/en_US.utf8和Falkor4.22.1，两个全新数据库及图命名空间；200事件，每事件1文章、1动作、2论元；没有专题意图模型、研究任务及共享文章。新旧当前事实哈希一致，均为200事件、200来源、200当前动作，共1009节点。

| 场景 | 保留的旧完整watch投影 | 新分批指纹投影 |
| --- | ---: | ---: |
| 更正1个成员：图写命令 | 2010 | 10 |
| 更正1个成员：含基准检查的完整耗时 | 1751.734ms | 1548.097ms |
| 更正1个成员：worker步骤耗时合计 | 1751.734ms | 1329.035ms |
| 更正1个成员：最大单次连续处理 | 1751.734ms | 57.485ms |
| 材料无变化：图写命令 | 2010 | 0 |
| 材料无变化：完整耗时 | 1950.911ms | 1745.650ms |
| 首次完整投影 | 1629.874ms | 3806.810ms |

新路径每轮仍分页检查全部200成员，产品SQL577次，基准自身111次检查另列；首次盘点和建立持久检查点增加开销。指纹按页读、检查点批量UPSERT已避免逐材料多次SELECT/UPDATE，但并非端到端O(变化量)。新路径的主要证据是减少重复图写入及缩短连续占有租约，而不是大幅提高本地完整轮次吞吐。

索引准备单列并排除于测量。完整JSON见[topic-projection-benchmark.json](topic-projection-benchmark.json)。并行QA使毫秒结果存在波动；小样本、单一动作及本机网络不能代表现场6万节点专题，也不能证明每条现场事件需要9秒，更不能推算追平天数。

## 浏览器

Chromium153，1440×1000及390×844。维护详情默认折叠，事件与专题耗时独立，partial不算专题完成；实际点击暂停、刷新、继续验证持久控制、游标和投影待办未变。无页面/控制台错误、失败请求或横向溢出。隔离HTTP fixture停用后台服务，断言HTTP不联系图数据库；遥测值是合成界面测试值，不是性能数据。结果见[graph-maintenance-browser.json](graph-maintenance-browser.json)。截图仅用于本轮内部检查，不加入交付包。

## 复现

QA依赖为pytest、falkordblite==0.11.0、pgserver==0.1.4和psycopg；它们不作为生产依赖。locale需在操作系统中已安装。

```bash
python -m pytest -q
python tools/run_postgres_test_server.py --locale C -- python -m pytest -q tests/test_graph_queue.py tests/test_postgres_integration.py tests/test_postgres_pipeline.py tests/test_postgres_migration.py tests/test_graph_focus_postgres.py tests/test_graph_focus_migration.py tests/test_graph_gc_pause_migration.py tests/test_graph_migration_state.py tests/test_startup.py
python tools/run_postgres_test_server.py --locale en_US.utf8 -- python -m pytest -q tests/test_graph_queue.py tests/test_postgres_integration.py tests/test_postgres_pipeline.py tests/test_postgres_migration.py tests/test_graph_focus_postgres.py tests/test_graph_focus_migration.py tests/test_graph_gc_pause_migration.py tests/test_graph_migration_state.py tests/test_startup.py
python tools/benchmark_topic_projection.py --events 200 --output topic-projection-benchmark.json
python tools/check_graph_maintenance_live.py /tmp/news2douyin-maintenance-browser
```

图分析发布仍等待全部投影队列清空，最多分析最近5000事件；本轮没有修改该机制。SQL schema/触发器及必要搜索索引仍同步初始化，首次升级仍有一次历史抽取回填。现场启动2分钟和radar首次60秒阻塞，需用新阶段日志、命名SQL和数据库锁等待观察进一步确认。
