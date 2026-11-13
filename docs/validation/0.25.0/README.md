# 0.25.0 验证记录

验证日期：2026-11-12。测试均在隔离的临时数据库和存储目录运行，没有访问或迁移用户现场的27GB数据库。

## 执行结果

| 检查 | 结果 |
| --- | --- |
| 全量回归 | 558 passed，51 skipped，202.43秒 |
| 真实PostgreSQL 16.2混合回归 | 156 passed，61.08秒 |
| 最后修正后的专题、图、迁移与旧专题复核 | 55 passed，24.11秒 |
| 真实HTTP、后台线程、FalkorDB、Chromium | 通过，桌面与390px移动端无横向溢出，页面/控制台错误均为0 |
| 浏览器新闻与模型 | 明确的本地受控夹具，不代表真实新闻准确率 |

各组有重叠，不将其相加为独立测试数量。全量回归后针对恢复自动判断、历史补查触发及旧专题进度显示做了小幅修正，上述55项是在这些修正后的源码上执行。可选PG路径在全量回归中跳过，另在真实PG组执行。

## 验证的用户行为

- 文字描述提交后立即返回持久任务，后台生成关注对象、关系模式和初始问题；名称、问题与排除原则可修改后保存。
- 创建和修改专题不在HTTP请求中扫描历史正文。240条历史事件夹具的SQL审计确认创建前台没有读取历史material，后台完成后归集数量正确。
- 实体、动作角色、词项和真实全局图关联能提供候选；真实FalkorDB验证关联事件在模型判断前不会成为专题成员。
- 空专题图中的InterestScope/InterestEntity/InterestPattern不会生成虚构EventMention或Event。
- 判断带固定文章版本与原文摘录；格式错误、无证据、模型不可用时保留待核。发生时间不以发布/采集时间替代。
- 原文更正、专题范围更新及人工排除不会被旧任务覆盖；恢复自动判断后能重新处理纯图关联材料。
- 人工排除同步移除问题材料提示；范围变化后的旧自动材料从当前列表、时间线和研究取材隐藏，解释历史保留。
- 模型收录更新未读变化与问题材料；实时核心事件可按既有每日限制触发补查，历史回填不触发实时补查。
- 老SQLite新增表不破坏原专题与人工问题；新专题任务和判断状态可经SQLite→PG迁移，逐表行数、全行SHA256与原始版本一致。
- 浏览器覆盖请求失败保留描述、过期结果不覆盖新输入、图缩放/节点选择、收录依据、待核重试、范围编辑、旧手动入口和模型离线。

浏览器结构化结果见[focus-browser-results.json](focus-browser-results.json)。截图仅用于内部检查，未作为发布内容。

## 可复现入口

```bash
python -m pytest -q
python tools/run_postgres_test_server.py -- python -m pytest -q tests/test_focus_jobs.py tests/test_focus_integration.py tests/test_postgres_migration.py tests/test_worker_isolation.py
python tools/check_focus_live.py /tmp/news2douyin-topic-qa
```

PG启动工具需要可选QA依赖pgserver；浏览器工具需要Node、playwright-core、Chromium和测试用redislite/FalkorDB模块。生产应用不依赖这些QA组件。Chromium可通过N2D_QA_CHROMIUM指定。安装包检查见源码包中的release-0.25.0/VALIDATION.txt。

## 边界

本版没有新增密集向量语义检索。图关联算法仍使用最近最多5000个事件，每轮一跳扩展最多30项；历史回填遍历本地已索引事件，不意味着原始GKG每行都经过模型，也不代表上游采集无遗漏。

未验证真实模型在新闻流上的误收、漏收、长期运行成本和持续吞吐；未运行生产Docker Compose；真实PG验证版本为16.2，生产Compose声明的PG17部署仍需现场验证。本版未执行现场27GB迁移或自动清理。
