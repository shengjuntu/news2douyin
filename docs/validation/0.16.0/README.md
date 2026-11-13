# 0.16.0 验证记录

验证日期：2026-10-14。基于已交付0.15.1开发。

| 验证 | 结果 |
| --- | --- |
| Python 全量回归 | 334 passed、17 skipped；日志 all-tests.log，跳过依赖具体见日志 |
| 最终选题专项 | 13 passed；历史日期/分页/事件、版本冲突、原文快照、候选与引用隔离、连续会话、消息/交接并发幂等、停止后写入拒绝、原回执重启恢复、无Exa的本地选题、环境未配置时的持久交接；日志 topic-tests.log |
| 执行器与 Docker 合同回归 | 39 passed，日志 dispatch-tests.log；Docker/模型使用测试替身 |
| 前端交互 | 8项通过：一句话开始、采用候选、丢失消息响应后重试并复用会话、刷新恢复、固定原文预览、搜索不丢选择、素材报告交接丢失响应重试、不出现JS运行错误 |
| 真实应用 HTTP 与 MCP | 前端检查启动真实 ASGI 服务与SQLite，执行实际页面JS；RunDesk/模型为合同替身，模型通过实际 MCP 接口保存候选和回复 |
| 升级 | 以真实0.15.1生成旧库和每日选择后升级，旧表行数保持一致、历史资料可查、旧日选可读；仅新增6张选题表，见 upgrade.json |
| 静态与打包 | Python编译、JavaScript语法、git diff --check；安装包包含新模块、页面和脚本，发行包校验SHA-256 |

前端使用JSDOM执行实际页面脚本，不代表真实浏览器布局或跨浏览器视觉验收。当前环境未能下载 Chromium，因此没有提供或宣称真实浏览器截图验收。

未连接用户RunDesk、Codex、Exa或真实模型；未构建或运行研究Docker，未验证真实选题质量、外部资料和配图覆盖、端到端视频渲染。普通素材报告模式保存配图页面线索，不自动下载图片；短稿配图继续使用已有下载与授权状态记录机制。

运行复查：`python -m pytest -q`。界面合同复查需Node与jsdom26.1.0：设置 `TOPICS_JSDOM_MODULE` 为jsdom模块路径后执行 `python tools/check_topics_ui.py`。这些是开发验证依赖，运行news2douyin页面不需要安装jsdom。

部署与首次验收见 [TOPICS](../../TOPICS.md)。
