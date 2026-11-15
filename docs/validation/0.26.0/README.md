# 0.26.0 验证记录

本轮使用隔离本地SQLite、PostgreSQL16.2、DuckDB1.4.5及实际FalkorDB4.22.1；未访问或修改192.168.0.234部署。对官方GDELT仅只读获取四个小批次验证解析。QA之间有重叠，不累加成独立用例总数。

| 检查 | 实际结果 |
| --- | --- |
| 全量pytest，真实Falkor可用 | 717 passed、91 skipped，260.94秒，无失败 |
| 新功能实际PG C、en_US.utf8矩阵 | 各86 passed |
| 新默认GKG模式后的PG C、en_US矩阵 | 各39 passed |
| 最终PG en_US NULL排序／忙碌间隔／六流／存储／旧链路 | 33 passed，28.94秒 |
| 最终国名／ISO词项、归档引用及选题流程实际PG | 31 passed，7.40秒；US不会误匹配RUS，中文国家名可召回 |
| 根服务排序与研究元数据补充 | 22 passed、7个PG用例未配置时跳过，实际PG另执行 |
| 最终解析与UI补充 | 26 passed、9个未配置PG用例跳过，3.42秒 |
| 真实Chromium153桌面／手机 | 全部通过，见gdelt-events-browser.json |
| 最终wheel隔离安装 | 18项既有工作流冒烟＋六流资源／新SQL表／国家词项／显式选题等新路径通过，见installed-wheel.json |

全量运行未配置PG的参数化用例与可选依赖用例跳过；关键PG路径在真实C与en_US矩阵执行。上版曾出现的0.9秒任务心跳偶发用例本轮也通过，没有修改超时或断言。CI新增PG17双locale检查配置，但本轮没有在GitHub执行，不称现场PG17已验收。

## 六流与官方样本

官方源小样本：[official-source-samples.json](official-source-samples.json)。英文Events633／633、翻译Events452／452、英文Mentions2634／2634、翻译Mentions728／728全部通过解析。原始URL、成员名、批次及校验记录保留。实际源观测仅验证这些文件，不代表世界所有事件已经完整收录。

单元与集成覆盖61列Events、16列Mentions、CAMEO前导零、FIPS与ISO区分、空字段和坏日期／非有限数、无效UTF8、引用非网页文档、跨语言命名、429单流独立退避、完整归档后再确认游标、重放及乱序到达。原始Mentions全部保留，紧凑SQL索引重复观察不增文档数。

六流端到端测试通过：归档→单份载荷DuckDB→三类公平投影→官方事件／文档索引→来源筛选→明确选择→内容任务→元数据研究交接。默认GKG新发现不建立本地事件；显式兼容模式另测旧GKG→规则抽取→本地事件→真实Falkor链路。

## 筛选与一致性

按可见页查询媒体域名覆盖和已选状态，不读取整个原档或返回大payload，默认不执行精确全量COUNT。最低来源数使用本站实际Mentions域名数，不使用官方首次计数冒充累计热度。未知覆盖不显示为零报道，最优来源按实际置信度优先，PG明确NULLS LAST。时间窗按最近官方观察，旧发生日期但新提及仍能发现。字面百分号和下划线不会被当SQL通配符。

GET不创建Article、Event、RadarEntry或内容任务，也不联系图服务器。四并发相同选择生成一个任务；故障注入证明选题来源写失败时工作区、任务、研究起点、冻结来源及绑定全部回滚。选题后仅保存标明event_metadata的研究线索；直接短稿与将元数据作为正文证据被拒绝，读取原始报道后可继续补材／研究。

## 存储与旧数据

新版每行仅一份精确原始载荷，保存解析版本和摘要检索字段；旧表仍可读取，无启动大表重写。新PGGKG版本只复制必要在线字段及含SHA256的原档引用，外部正文和历史不可变快照保持原义。缺档、坏哈希、孤立记录、未知表、重叠格式、未知解析版本与损坏载荷都拒绝静默整理或重新解释。

离线复制从原ZIP恢复精确字节，并与源表解释比对；目标重新读取核对全记录逻辑哈希、行数和原始字节，才发布新文件。未知旧metadata键保留差量，源文件不覆盖、不删除，目标不自动激活。

[合成物理文件对照](intake-storage-benchmark.json)：1024条GKG、每条1000个变化GCAM计数项、中文标题。旧双份列文件41,431,040字节，新单份载荷文件5,779,456字节，复制和验证3480ms；逐行语义与原始字节校验通过，源保留。它只是小样本实际文件量测，不能外推现场58.75GiB会回收多少，更不能把length(VARCHAR)字符数当物理空间。

## 浏览器

真实Chromium153，桌面1440与手机390。验证默认自动进入官方线索、返回已整理事件、CAMEO筛选、来源与原档链接、缺少Mentions时显示未知与首次来源、保存筛选刷新后继续、形成选题跳入已有工作台及六流状态。无JS／控制台／请求错误或横向溢出。截图只作内部检查，未加入交付包。

## 复现工具

```bash
python -m pytest -q
python tools/run_postgres_test_server.py --locale en_US.utf8 -- python -m pytest -q tests/test_official_events.py tests/test_selection_backend.py tests/test_gdelt_pipeline_integration.py tests/test_intake_storage_v2.py tests/test_event_metadata_handoff.py tests/test_postgres_pipeline.py
python tools/benchmark_intake_storage.py --output intake-storage-benchmark.json
python tools/check_gdelt_events_live.py /tmp/news2douyin-gdelt-browser
```

实际PG测试夹具创建并删除自己的随机临时数据库，不使用指定库的现有业务数据。QA用pgserver、falkordblite和浏览器依赖不属于生产必需依赖。

安装包在源码目录外使用Python 3.12 `-I`运行；应用包为新wheel，通用依赖通过显式site .pth复用既有QA环境。它验证包资源和运行路径，不声称重新从互联网安装了全部依赖。
