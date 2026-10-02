# 0.11.1 验证记录

- Python 全量：262 项通过，17 项因当前环境未配置完整真实视频渲染依赖跳过。既有弃用警告保留，详见随包 pytest.log。
- 研究页面 Chromium 测试：13 项通过，包括配置、策略、发起研究、追加、证据与报告、刷新保留、继续/停止及移动布局。官方 MCP Python 客户端：6 项通过。此组使用契约夹具。
- 与真实 RunDesk 0.6.1 演示二进制联调：两应用共 13 项通过；新闻覆盖丢失创建会话/启动任务响应、运行时重建恢复、来源标记、停止旧运行不影响新运行，以及数据库重开。
- 使用发行包 0.11.0 的源码创建旧数据库，再以 0.11.1 打开：51 张既有业务表逐行比较不变，新建 ResearchSubmission 表。排除两个用于事务串行化、初始化会递增的锁表。
- 关键单元测试覆盖 processing/unconfirmed、完成但失败的回执、认证失效导致回执不可读、固定请求内容、重启、停止期间不得新启动，以及旧版运行归属核对。
- 构建 wheel/sdist，并在独立安装路径核对资源与版本；见 release/BUILD_INFO.txt。

真实模型登录、搜索账户、MCP 在真实 Codex 中的执行以及新闻研究质量，仍需部署环境验证。本轮没有重测原视频渲染与配音能力；旧版演示素材仅作历史记录。

原始记录见 research-0.11.1/、rundesk-v1/integration.json 与 rundesk-v1/news-upgrade.json。

## 联调复现

需要 Python 的新闻应用依赖、requests、Node、Playwright 和 Chromium。设置 BROWSER_BIN、CODEX_PRIMARY_RUNTIME_NODE、CODEX_PRIMARY_RUNTIME_NODE_MODULES；字体可通过 FONTCONFIG_FILE 指定。两个服务由同一测试进程启动，测试数据使用临时目录。

```bash
python tools/integration_rundesk_v1.py --rundesk /absolute/path/rundesk --news /absolute/path/news2douyin --video /absolute/path/video-app --output /tmp/rundesk-v1-check
```

视频包对应脚本位于 scripts/integration_rundesk_v1.py。测试连接真实 RunDesk 0.6.1 二进制的 --demo 环境，不发起真实模型或外部搜索。新闻生产代码仍拒绝 demo；只有测试客户端覆盖 meta.demo，用于验证协议格式。
