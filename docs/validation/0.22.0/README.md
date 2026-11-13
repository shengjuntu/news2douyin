# 0.22.0 验证记录

日期：2026-11-04。基线：0.21.0源码包；合并用户现场报告的30/90秒GDELT可配置网络超时。

## 完成的检查

- 相关回归141项全部通过（183.37秒，11项依赖弃用警告）。覆盖新接收/投影/API、GDELT旧路径、每日采集、事务并发、任务、雷达/专题/工作台、初始化检查、新闻版本和策略管理。
- 全量归档测试8项：超过250行、无标题、重复URL、非WEB引用、坏行、翻译记录ID、独立DATE、校验失败不发布、双流断点、Retry-After、DuckDB提交后SQLite确认中断的幂等恢复、进程锁与真实子进程退出后重启恢复租约。
- 投影测试6项：300条分块处理、无标题仅元数据、不造虚假事件、回退游标复用回执、完整正文保护、迟到批次、外部正文版本/内容校验及观察元数据台账。
- 接入API测试10项：状态/事后搜索不改变采集范围、记录详情、暂停恢复/起点守卫、跨站拒绝、外部Bearer令牌/保留渠道名拒绝/幂等版本、非持有者读取DuckDB拒绝。
- 真实DuckDB 1.4.5、真实FalkorDB 4.22.1与Chromium下验证后台接收→DuckDB→SQLite投影→抽取→图分析。两个受控上游批次共307行，全部进入接收库；306可用记录投影，1条坏行原样保留且显示跳过；无标题记录保留并处于metadata_only，不进入虚假事件。
- 浏览器验证双流、超过250行、真实后台线程、暂停/恢复不改cursor、事后检索不改范围、原始详情、外部API幂等及正文、GKG默认与可选样本关键词、图链路、390px窄屏无横向溢出，未出现页面JavaScript错误。SQLite integrity_check为ok。
- 从0.21.0 wheel生成真实旧库，以0.22连续启动两次，原ArticleVersion/TopicSource/TopicWorkspace/ContentTask行内容逐项保持一致；接收状态初始化但不创建空DuckDB文件或联网。
- 原有工作台浏览器回归通过：雷达、选题、研究、创作、作品、专题和采集页面可用；真实FalkorDB参与图查询，研究正文使用受控夹具，移动端通过且无JavaScript错误。
- Python编译、变更JS语法和由真实Pydantic模型导出的文档JSON Schema检查通过。安装包资源检查另见release-0.22.0/VALIDATION.txt。

## 明确的范围

上游HTTP、GKG批次与报道正文使用标记为演示的受控夹具。研究核验时实际公告的公网GDELT样本链接返回404，因此不声称已经在本环境完成公网全量镜像，也没有验证长期全球吞吐、存储增长或真实模型/RunDesk/Exa运行。HTTP失败的cursor/退避机制已经测试。

GKG不含文章全文。本轮真实浏览器验证不等于全体新闻都完成了深度研究或专业模型判断。地缘/经济/行业模型与预测是后续分层设计，未生成模拟专业结论冒充功能。

本轮未实际构建Dockerfile或部署Docker Browser。DuckDB单文件限制为一个应用进程管理；Linux x86-64的1.4.5测试wheel标记要求glibc2.26或更新，CentOS7建议容器环境。Windows分支锁实现存在，但本轮实际子进程锁测试在Linux运行。

## 复现

```bash
python -m pip install '.[dev,research]' falkordblite==0.11.0
pytest -q tests/test_intake_archive.py tests/test_intake_projection.py tests/test_intake_api.py tests/test_gdelt.py tests/test_daily_workflow.py tests/test_concurrency.py tests/test_tasks.py tests/test_dual_desk.py tests/test_content_foundation.py tests/test_readiness.py tests/test_news_data.py tests/test_strategy_management.py
```

浏览器另需Node、playwright-core、可用Chromium（N2D_QA_CHROMIUM），并可用NODE_PATH指定Node依赖目录。有中文字体配置时截图可正常显示中文。

```bash
python tools/check_intake_live.py /tmp/n2d-intake-qa
```

测试脚本使用独立临时库和受控GKG回应，不接入用户数据库。截图/服务日志只供内部检查，未包含在发布包。产品默认持续接收，普通pytest以环境变量关闭网络后台并显式驱动fixture，避免测试发起真实大规模下载。
