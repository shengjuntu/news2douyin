# news2douyin 与 RunDesk 接入

当前配套：news2douyin **0.13.0**、RunDesk **0.47.0**。日常使用和首次部署见 [一键 Docker 研究](DOCKER-RESEARCH.md)。

首次“连接并配置环境”使用管理员身份登记 `news2douyin`，复用持久化的 installationId、instanceId 和 workspaceId。Docker 模式先登记应用，再绑定管理员选择的已构建镜像、准备环境，最后在该环境安装缺失的业务 MCP 和 `news-research` Skill。这样业务工具不会只装到宿主 Codex HOME。

模型提供方、认证、镜像更新和其他能力由 RunDesk 管理；成功初始化后不重放默认配置，管理员删除或关闭的工具不会被恢复。应用地址变更或已有 MCP 回调不一致时提示管理员核对，不自动覆盖。重连不复制账号、不自动构建 Dockerfile，也不运行模型。

## 运行协议

- `GET /api/v1/meta` 必须声明 `application-runtime` 能力。
- `GET /api/v1/applications/news2douyin/runtime` 返回最近观察到的环境摘要，不启动环境。
- `POST /api/v1/applications/news2douyin/runtime/prepare` 携带预期的 instanceId、workspaceId 和 probe；准备当前已绑定环境。probe=true 时检查模型认证元数据和 MCP 工具发现，不调用模型。
- 应用运行凭据需要 read/run 和当前绑定；只能操作自身环境，不获得镜像、安装或全局配置权限。首次接入仍需要管理员。普通研究可使用已经配置好的应用运行凭据。
- 成功准备后才创建直接会话；再核对会话所属 Docker 环境后提交 turn。会话和 turn 的幂等键、回执先持久化，网络结果未知时保留恢复流程，不能换 key 盲目重跑。
- news2douyin 的 run_id 关联环境检查结果、session_id、turn_id、检索票据及报告。应用侧保存业务结果，RunDesk 保存执行轨迹和运行状态。

POST prepare 可以重复调用，复用已有环境，不断开活动会话。配置已变化但环境未更新时返回冲突，必须在 RunDesk 显式部署。GET 是缓存观察值，不能当成实时 Docker 健康检查。

## 兼容与边界

旧配置保留本机模式和原 WorldNewsAPI/Tavily 检索流程；本版新增 ResearchEnvironment 表，历史证据、报告与提交回执继续可读。新安装默认 Docker + RunDesk/Exa。应用访问地址面向浏览器，Docker 回调地址面向容器，跨机器部署必须分别确认可达性。

“连接完成”“发现认证和工具”“真实模型研究成功”是三个不同阶段。验证范围见 [0.13.0 验证](validation-0.13.0.md)。

从 0.14.0 起，研究外部搜索统一由 Codex / Exa 执行；显式连接操作启用 Exa 并保留自定义地址与认证，本机与 Docker 模式都在提交前检查工具发现。研究服务不再直连采集搜索 API，详见 [研究检索约定](RESEARCH-SEARCH.md)。
