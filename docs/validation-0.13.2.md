# news2douyin 0.13.2 验证

配套 RunDesk 0.48.0，保留此前 GitHub 62ed074 的超时和 revision 重试合并，以及 Docker 完整访问/MCP 持续授权。

本版变化：新研究配置统一 Exa，旧 Wigolo 设置迁移为 Exa；旧研究环境快照保持原来源。显式“连接并配置环境”启用 Exa、停用 Wigolo，保留自定义 Exa 地址、认证及其他 MCP。镜像准备前移到 RunDesk 初始化向导，新闻侧不在研究时构建镜像。

验证：

- `tests/test_research.py`、`tests/test_research_v1.py`、`tests/test_research_docker.py` 共 **60 项通过**。
- 覆盖旧设置迁移、历史来源快照、Exa 自定义地址/认证保留、未配置前置条件拒绝执行、重复启动、停止与提交回执。
- `tools/integration_docker_research.py` 两个真实 HTTP 服务联调通过，业务 MCP 实际进行 initialize/tools/list 与证据保存；Docker CLI、Codex 和搜索为测试替身。
- 生成 0.13.2 wheel 与源码分发包；最终交付检查 wheel 中修改过的 Python/HTML 与当前源码一致。

当前没有真实 Docker Engine、在线模型或搜索质量验收。请先在 RunDesk 初始化向导准备镜像，完成该新闻环境的模型认证，再开始真实研究。
