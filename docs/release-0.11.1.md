# news2douyin 0.11.1

本版是 RunDesk 接入升级，保留“当日采集 → 选题 → 研究 → 脚本”及已有作品管理。

- 使用 `/api/v1` 并检查能力。需要 RunDesk 0.6.0+，建议使用已验证的 0.6.1；不自动回退旧 API。
- 配置实例、工作区时保存提交编号；每轮研究在本地事务中保存创建会话与启动任务的原始请求，再发往 RunDesk。
- 创建或启动的响应丢失后，查询 `/requests/{key}`。processing/unconfirmed 继续待确认；completed 仍需检查 httpStatus。只有 request_not_found 才可用同一编号、同一请求体重发。
- 新会话带 application 来源，appId=news2douyin，taskId=研究执行编号。旧会话标签不改写。
- 保存实际 RunDesk runId，停止携带 expectedRunId；会话若已用于另一项运行，不会停止它。旧版任务需从原 run/input 事件核对归属，无法证明时保持待确认。
- 设置页显示实例默认模型，检查使用配置总览。模型、Skill、MCP 仍由 RunDesk 管理；新闻应用负责业务界面、证据和报告持久化。

## 升级

1. 等活动研究结束，停止 news2douyin；备份原数据库、storage/runs 与研究 settings.json。RunDesk 使用原数据目录升级到 0.6.0+。
2. 解压本包替换程序，保留原 `.env`、数据库和存储目录。可安装 `release/news2douyin-0.11.1-py3-none-any.whl`，或在源码根目录 `pip install -e .`。
3. 使用原命令和数据目录启动。启动时只新增 ResearchSubmission 表；不重写历史研究表。
4. 在 `/research/settings` 检查连接。实例配置需要更新时，待研究助手空闲后点击配置，复用现有实例、工作区、Skill 与 MCP。真实认证与回连地址仍使用自己的环境。
5. 从新闻详情发起一次短研究，检查证据和报告写回；旧报告仍可阅读和导出。

不要删除待确认请求或换一个研究编号来“重试”。setup_requests 和 ResearchSubmission 是恢复所需数据，随原目录备份。若需要降级，恢复升级前的整套备份，避免混用旧代码和新提交状态。

本轮暂停视频制作新能力，未变更搜索提供方、新闻制作包协议或视频渲染器。真实模型研究质量与外部搜索认证需在部署环境验收。
