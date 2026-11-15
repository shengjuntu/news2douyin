# 0.28.0 验证记录

本轮基于0.27.0，用本地真实HTTP服务复现用户给出的NewsNow响应形态；使用隔离SQLite、实际PostgreSQL16.2 C/en_US.utf8和既有真实Falkor测试环境。未访问或改动现场数据库/容器；开发环境不能连接用户提供的私网服务，不把本地fixture通过称为现场35源通过。

| 检查 | 实际结果 |
| --- | --- |
| 完整pytest | 861 passed、131 skipped，302.00秒，无失败 |
| 新NewsNow真实HTTP集成/只读probe，PG C＋SQLite | 23 passed，16.63秒 |
| 同组，PG en_US＋SQLite | 23 passed，16.39秒 |
| 先NewsNow线索后真实全文升级，SQLite＋PG en_US | 2 passed，2.75秒 |
| 固定版本抽取保护API，SQLite＋PG en_US | 2 passed，2.65秒 |
| 抽取保护与原链路 | 27 passed、11个未配置PG跳过，9.60秒；最终SQLite＋实际PG抽取/交接矩阵22 passed，9.64秒 |
| provider／归档／真实HTTP probe联合 | 76 passed，1.54秒 |
| 原采集、任务、GDELT及元数据交接 | 76 passed，12.69秒 |
| 新NewsNow UI＋原新闻／配置回归 | 39 passed，9.37秒；最终状态措辞补充7 passed，2.02秒 |
| wheel隔离安装 | 既有核心、事件译读、NewsNow三组安装冒烟均通过，见installed-wheel.json |
| NewsNow研究交接新旧组 | 45 passed、5个PG跳过；实际PG＋SQLite新组10 passed |

各组检查重叠，不求和。完整收集后补充的固定版本抽取防线由针对性与真实PG/浏览器另验，没有声称再次完整全量。默认全量中的PG参数化及可选依赖用例跳过；关键路径另在实际数据库执行。GitHub PG17双locale CI已增加新文件配置，本轮本地执行的是PG16.2，未声称GitHub CI已运行。

## 验证重点

- 八种时间形态、时区假设与日/秒精度；缺发布时间保持未知，快照时间不冒充原文发布时间或真实上游重新抓取时刻。
- HTTP200 SPA、坏JSON、错误源ID、无效items、非有限数值与超额响应不能算成功；合法空榜、部分失败、全源失败、预算不足未尝试分别可见。
- 默认完整观测后轮转在线限额；股票榜只留原档；raw快照包含未选入在线列表/无效条目，报错响应也可追溯。
- 原始HTTP内容字节有SHA256，重复缓存复用文件；被篡改原档拒绝覆盖。任务checkpoint为小型校验文件指针，缺失/损坏拒绝静默重抓；已测report已存/raw未冻结的恢复窗口。
- Article按URL身份去重，重复快照/排名变化不增版本；既有真实报道不被摘要覆盖，先线索后真实全文可以进入原事件/图处理链。
- NewsNow摘要在研究中作为线索；固定r1即使当前Article已升级真正文r2仍不能直接排正文抽取或人工确认旧陈述。旧排队任务回到metadata_only，真全文新版本和GKG手动行为保留。
- HTTP不占SQL写事务，NewsNow短文本不逐条加载1000篇历史全文做近似去重，也不默认逐条建立本地Event或启动模型。

## 浏览器

真实Chromium153，桌面1440与手机390：[完整采集旅程](newsnow-browser.json)与[固定版本保护](newsnow-article-guard-browser.json)。

验证来源选择/保存、规则模式、未知语言、逐源诊断、试跑不写Article、真实TaskWorker正式采集3Article/0Event、缺发布时间、摘要标签、全源失败可见。另验r1无可用抽取按钮且API422，r2真实正文可抽取；最终1Article/2Versions/1Event。没有JS/请求错误、横向溢出或HTTP访问图服务器。截图只作内部目检，没有加入交付包。

## 复现

```bash
python -m pytest -q
python tools/run_postgres_test_server.py --locale en_US.utf8 -- python -m pytest -q tests/test_newsnow_integration.py tests/test_newsnow_handoff.py tests/test_newsnow_probe.py
python tools/check_newsnow_live.py /tmp/newsnow-browser
python tools/check_newsnow_live.py /tmp/newsnow-article-guard --article-guard-only
```

实际PG夹具创建并删除自己的随机数据库，未使用部署库已有业务数据。[机器可读汇总](qa-run-summary.json)记录原始组别和现场未验收边界。部署机另运行`tools/probe_newsnow.py`，它只GET NewsNow JSON，不写业务库。

安装在源码目录外使用Python3.12 `-I`；应用来自新wheel，通用依赖用显式site .pth复用QA环境。依次运行tools/smoke_installed.py、smoke_intelligence_installed.py、smoke_newsnow_installed.py；NewsNow使用真实本地HTTPfixture和实际TaskWorker，无现场访问。
