# 0.27.0 验证记录

本轮使用隔离SQLite、实际PostgreSQL16.2的C/en_US.utf8及实际FalkorDB；真实Chromium153验证桌面1440与手机390。没有访问或修改现场部署。各组检查有重叠，不能相加作为独立用例总数。

| 检查 | 实际结果 |
| --- | --- |
| 全量pytest首次运行 | 761 passed、116 skipped、2 failed，282.42秒；两失败为旧GDELT收录段标题断言，已修复并按下组复验 |
| 最终相关回归 | 28 passed、11个未配置PG跳过，9.17秒；包含GDELT、新闻译读、专题生命周期与跨功能旅程 |
| GDELT与新闻详情文案修复复验 | 16 passed，2.66秒，无失败 |
| 计数/六流进度与原选题后端，SQLite＋PG en_US | 35 passed，5.63秒；纯SQLite组23 passed、12个未配置PG跳过 |
| 后台译读、语言和任务监控，SQLite＋PG en_US | 30 passed，7.84秒 |
| 专题生命周期，SQLite＋PG en_US＋Falkor | 最终恢复版本校验后17 passed，9.71秒；旧专题与图增量回归48 passed、1个PG跳过 |
| 工作台/资料状态及旧研究交接 | 28 passed，41.95秒 |
| 新闻库/翻译UI与旧新闻数据 | 21 passed，4.22秒 |
| 雷达UI | 10 passed，3.31秒 |
| 新wheel隔离安装 | 18项旧工作流＋9项新功能安装冒烟通过，见installed-wheel.json |
| 跨功能旅程，实际PG C/en_US | 各6 passed；SQLite单独3 passed |

## 关键边界

- 打开官方雷达不做全量COUNT、不抓网页、不调用模型或图数据库；计数在明确点击后执行同一筛选，窄列、有SQL时限与短缓存。SQLite实际超时后连接仍可使用，原busy_timeout恢复。
- 归档与索引分开，已知早期缺口前的连续完成批次才作为索引进度；某一条流新批次不能证明其它流已处理。
- 译读HTTP不等待模型。后台使用真实TaskWorker、租约和持久任务；缓存与固定版本/文本范围/原文哈希相关，迟到或取消结果不改写原证据。
- 官方线索语言未知，拉丁文字不自动判英语；按文字推测有明确依据标签。超长材料只译受限前缀并说明，未取得全文不冒充正文。
- 专题删除与恢复校验版本、停止自动跟踪并保留独立成果；模型执行中删除后不能发布匹配结果；真实Falkor标记归档和恢复，不删除共享关系。
- 工作台元数据线索可打开冻结快照和正确雷达详情，无不存在的Article链接；界面提前禁用直接短稿，补材与研究继续原幂等流程。

## 浏览器和译文

报告：[综合旅程](ui-browser.json)、[新闻译读](article-translation-browser.json)、[雷达兼容](radar-compat-browser.json)。最终综合报告包含恢复版本校验后的走查、一次模拟模型失败及明确重试成功。

浏览器与API集成测试使用离线、确定性的模型返回，验证任务状态、失败重试、原文保留及恶意HTML按纯文本显示；未调用用户模型，不声称翻译语义质量已验收。截图仅作内部布局检查，不作为交付文件。

## 复现入口

```bash
python -m pytest -q
python tools/run_postgres_test_server.py --locale en_US.utf8 -- python -m pytest -q tests/test_selection_counts_overview.py tests/test_selection_backend.py tests/test_reading_translations.py tests/test_focus_lifecycle.py tests/test_ui0270_journey.py
python tools/check_ui0270_live.py /tmp/news2douyin-ui0270
python tools/check_article_translation_live.py /tmp/news2douyin-article-translation
```

PG测试创建并删除自己的随机临时数据库，不操作现有业务库。GitHub PG17双locale CI包含新增检查配置，本轮本地执行的是PG16.2，未声称GitHub CI已运行。浏览器依赖、pgserver与falkordblite仅用于QA，不是新增生产依赖。

全量收集后补充的恢复并发测试和新版API由最终针对性/实际PG组另验；没有把修复后的单独复验称作再次全量全部通过。可选依赖和未配置PG的参数化用例在默认全量中跳过，关键PG路径另在实际数据库执行。

安装验证在源码目录外使用Python3.12 `-I`，应用来自新wheel，通用依赖通过显式site .pth复用已有QA环境。可复现：`python -I tools/smoke_installed.py`及`python -I tools/smoke_intelligence_installed.py`；译文provider使用离线stub，不代表模型效果验收。
