# 0.13.1 验证记录

2026-10-12，配套 RunDesk 0.47.1。当前源码为合入 GitHub 提交 `62ed0741a6d0f96d26c01e85f92f556b335cf161` 后的 v0.13.1。

## GitHub 修改合并

来源：[shengjuntu/news2douyin 提交 62ed074](https://github.com/shengjuntu/news2douyin/commit/62ed0741a6d0f96d26c01e85f92f556b335cf161)，原修改仅涉及 rundesk.py，+19/-4，基于 v0.11.1。合并以已交付 v0.13.1 为底稿，生产代码仍只改此文件。

| 原修改 | v0.13.1 合并结果 |
| --- | --- |
| HTTP 超时改为 10/60 秒 | 普通请求采用 10/60；runtime/prepare 保留新版 10/180 秒 |
| 自动配置 Docker，版本冲突后重试 | 保留新版显式 Docker 模式、当前镜像和资源设置；仅 revision_conflict 重新读取并重试一次，其他错误立即返回 |
| 不再以宿主 Codex 路径匹配 Skill | 保留新版按名称及实例作用域管理 Skill，由 RunDesk 在实际环境发现；不向任务发送旧宿主 skill_path |

58 项研究回归通过，新增 11 个用例覆盖超时、冲突恢复、并发镜像变更、失败不重试和旧宿主 Skill 路径。两端真实 HTTP 与业务 MCP 联调通过，重建 wheel 后核对其 rundesk.py 与源码相同。Docker/Codex/搜索仍为替身，未做真实容器或模型验收。本轮未修改前端、数据库、Kun 或 RunDesk，也未推送 GitHub。

[合并验证](validation/0.13.1/merge-summary.json) · [研究测试](validation/0.13.1/merge-research.log) · [HTTP 联调](validation/0.13.1/merge-http/summary.json) · [相对原 v0.13.1 的补丁](validation/0.13.1/merge-62ed074.patch) · [GitHub 原始补丁](validation/0.13.1/github-62ed074.patch)

## 权限修复版验证基线


- 全量 Python：279 通过、19 个可选媒体测试因依赖缺失跳过；[日志](validation/0.13.1/pytest.log)。随后修复 Codex 旧实例空 kind 兼容，Docker 专项 18 通过；[日志](validation/0.13.1/docker-permissions.log)。
- 页面：真实模板与 JS 在 DOM 替身上验证 5 项交互；[日志](validation/0.13.1/ui.log)。未做浏览器视觉验收。
- 两端：实际 RunDesk/news2douyin HTTP 服务和业务 MCP，验证完整访问配置、旧配置升级、脱敏凭据保留、另一实例权限保持、容器复用、启动、证据/报告、轨迹、停止；[汇总](validation/0.13.1/http/summary.json)。
- 本轮新增 5 个回归测试：旧授权升级、保留配置模式、本机拒绝放宽、旧 RunDesk 能力检查、非法配置值。
- 构建 news2douyin 0.13.1 wheel；原始日志随包提供。

HTTP 联调使用 Docker CLI、Codex 和搜索替身，不代表真实容器、模型或研究质量验收。部署后按 [Docker 研究说明](DOCKER-RESEARCH.md) 停止旧研究，连接配置完整访问，再执行连续原文读取验证无重复授权。原生 MCP 数据表单、OAuth 和服务端强制策略仍可能需要交互。

```bash
python -m pip install -e '.[research,dev]'
python -m pytest -q
python tools/check_research_ui.py
python tools/integration_docker_research.py --rundesk-root /absolute/path/to/rundesk --output /absolute/path/to/validation
```
