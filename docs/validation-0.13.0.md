# 0.13.0 验证记录

日期：2026-10-12。配套 RunDesk 0.47.0、Kun 0.16.0。源码实现了从新闻应用准备 Docker 环境并提交研究的完整协议路径；本地验收没有实际 Docker Engine、Codex 或在线搜索。

| 检查 | 结果 |
| --- | --- |
| news2douyin 全量 Python 测试 | 274 通过，19 跳过；跳过项为缺少 Pillow/中文字体等依赖的可选媒体测试 |
| RunDesk 兼容性 | application、Docker、environment、v1、seed 和 containerruntime 相关测试通过 |
| RunDesk 运行环境并发 | 新增应用运行环境测试通过 Go race 检查；覆盖并发启动复用、活动连接保留、绑定与归属、应用权限及配置更新守卫 |
| 两端真实 HTTP 服务 | 应用注册、镜像配置、业务 MCP initialize/tools/list、开始研究、检索票据与结果、原文/判断/报告、轨迹和停止通过 |
| 容器复用 | 配置、检查与研究全流程只观察到一次容器 create 和 start；底层为 Docker CLI fixture |
| 页面交互契约 | 5 项通过；真实模板脚本在 Node DOM 替身中验证保存后配置、检查、启动、准备态按钮与停止刷新 |

原始结果在 [validation/0.13.0](validation/0.13.0/summary.json)。HTTP 联调脚本使用真实服务和真实业务 MCP；Docker、Codex 及外部搜索为测试替身，测试端显式替换运行发现结果。生产代码仍拒绝演示执行器用于正式研究。联调修复了首次配置持有设置读锁、从而阻塞业务 MCP 回调的问题，并有并发回归测试。

没有浏览器视觉验收：本机无浏览器，尝试下载 Chromium 时压缩包校验失败，页面测试范围仅限上述 DOM 交互契约。没有重跑上一版的视频渲染验收，也没有验证模型效果、搜索召回、浏览器正文提取或真实网络部署。

## 复验

在安装 `.[dev,research]` 的新闻项目中运行：

```bash
pytest -q -rs
python tools/check_research_ui.py
python tools/integration_docker_research.py --rundesk-root /absolute/path/to/rundesk --output /absolute/path/to/new-validation
python -m build --wheel
```

先在对应 RunDesk 源码执行 `go build -buildvcs=false -trimpath -o bin/rundesk ./cmd/rundesk`。HTTP 脚本自动使用临时业务数据，不依赖用户新闻库；详见脚本的测试替身说明。
