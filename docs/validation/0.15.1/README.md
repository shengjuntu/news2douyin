# 0.15.1 入口整理与验证

当前入口：顶部“补充材料” → “选择新闻，开始补材”；脚本工作台和每日选题均有明显按钮。旧版 0.15.0 的位置是每日选题右侧“生成方式”下拉框里的“轻量补材并生成（RunDesk）”。

新直达地址为 `/daily?mode=short#script-builder`。仅预选并展开模式，不自动选择新闻或启动研究；保留当日已选新闻，用户确认后提交到原有接口。筛选、分页、刷新保留模式，补材结果从顶部导航找回。

2026-10-13 验证：

- 原有短稿、每日选题与后台任务回归：38 passed，0 failed。原始日志 `regression.log`。
- 真实 FastAPI/TestClient、临时 SQLite：预选及展开、分页参数、原选题保留、无效模式回落、三个页面入口、打开页面不创建研究任务，均通过，见 `entry-check.json`。
- 渲染后的 JavaScript 通过 Node 语法检查。
- 没有进行真实浏览器视觉验收，也未连接用户 RunDesk、Exa 或模型服务。本次未改动研究执行、预算、搜索和脚本保存逻辑。

升级：备份数据库、运行目录和配置，停止 news2douyin；在原 Python 环境从项目根目录执行 `python -m pip install -U './release-0.15.1/news2douyin-0.15.1-py3-none-any.whl[research]'`，按原参数重启，浏览器刷新应显示 0.15.1。无需因本次入口调整升级 RunDesk 或重建研究 Docker 镜像。
