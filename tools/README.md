# 开发与运维工具

| 任务 | 入口 |
| --- | --- |
| 迁移 SQLite 到 PostgreSQL | `news2douyin-migrate-sqlite` |
| 导出和评审日报 | `news2douyin-review` |
| 验证和压实接收档案的副本 | `compact_intake_lake.py` |
| 观察持续接收进度 | `sample_intake_status.py` |
| 检查 NewsNow JSON 协议 | `probe_newsnow.py` |
| 检查 GDELT 事件与原文片段 | `audit_ngram_actions.py` |
| 在隔离 PostgreSQL 中跑集成测试 | `run_postgres_test_server.py` |
| 验证 wheel 的资源与接口 | `.github/workflows/tests.yml` 中的安装后检查 |
| 用浏览器检查事件雷达和工作台 | `check_radar_live.py`、`check_workbench_live.py` |

页面验证使用独立临时数据。数据库和图投影基准文件以 `benchmark_` 开头。历史版本的验收脚本和结果可从相应发行包复现。
