# 0.4.1：接入独立 Go 视频 APP

本次将 news2douyin 的既有制作包接入之前交付的 RunDesk Video App。两个项目独立运行：news2douyin 保留 Python；视频 APP 保持 Go + HTML/CSS/JavaScript；RunDesk 继续管理 Agent 生命周期、配置和事件。

配套版本是 **RunDesk Video App v0.3.0**，基于用户已有 v0.2.0 修改；对照 **RunDesk v0.5.4** 源码与实际接口实现。没有重新编写另一套视频 APP，也没有改动 RunDesk。

## news2douyin 的变更

- 新增 `/handoff` 页面，说明审核稿、成片和 RunDesk 工具的交接步骤。
- 脚本页与视频结果页增加交接入口。
- `tools/smoke_video.py --output-dir ...` 除原有样片外，同时保留配音 WAV、video_bundle.zip 和相同版本的 script_bundle.zip，方便跨应用复现。
- 版本升级到 0.4.1；数据库和现有 ZIP schema 均未变更，旧流程保持兼容。

## Go 视频 APP 配套能力

| 能力 | 行为 |
|---|---|
| 审核稿导入 | 导入既有 schema v1 脚本 ZIP，新建独立项目，保留完整原稿、来源和审核记录 |
| 完整性检查 | 固定平面文件、解压大小限制、逐文件哈希、快照/口播/来源匹配和审核版本检查 |
| 重复提交 | 同包返回已有项目，保留已有草稿；相同导出标识但不同包内容拒绝覆盖 |
| 成片接收 | 只接受同一 export_key、revision、script_hash 的 video_bundle.zip；检查实际音视频、配音、封面与字幕 |
| 预览下载 | 来源面板查看证据、播放原始成片、下载 MP4/WAV/SRT/完整包，支持 Range |
| Agent 依据 | MCP 新增 video_news_get，项目 Skill 指导 Agent 先读原稿和证据再修改制作草稿 |

导入分镜按正文生成，目标时长只是估算；不是实际配音时间轴。原稿审核状态只覆盖原稿，后续 APP 编辑属于待核对的制作草稿。导入原片不会随分镜变化，也不计入 APP 当前修订的渲染成功记录。接收 WAV 后可下载并上传至 APP 音轨，按实际时长调整分镜。

完整操作和格式限制见 Go APP 包内 docs/NEWS-HANDOFF.md。导入脚本不需要 HyperFrames；接收原片需要 FFprobe；Go APP 重新渲染仍使用原有 HyperFrames 工具链。

## 使用

1. 在新闻工作台审核并导出脚本 ZIP；在 Go APP v0.3.0 点击“导入新闻制作包”。
2. 可选：在新闻工作台制作视频，下载 video_bundle.zip；在对应 Go 项目接收原始成片。
3. 在 Go APP 连接 RunDesk，已有项目升级工具至 0.3.0。模型与登录沿用用户自己的专用实例配置。
4. 打开原稿和来源，提出修改要求；核对改稿，使用 APP 的既有工具继续制作和重新渲染。

本次不自动发布内容、不共享数据库、不自动向真实模型发送消息。包内哈希不是发布者签名，使用者需确认导入来源。

## 验证

- Python：89 项测试通过；独立 wheel 的 18 项流程/页面检查通过；实际离线配音成片的 8 项检查通过。
- Go APP：go test -race、go vet、前端 JavaScript 语法检查通过；Linux 实际运行，Windows 交叉编译。
- 跨应用：11 项检查通过，使用 Python 真实生成的中文脚本包和 7.83 秒成片，核验导入、原稿/产物保持、Range、RunDesk demo 配置、独立 MCP 读取与编辑、演示会话事件。
- 未执行真实 Codex 模型验收、新页面浏览器交互验收、Windows 真机或 HyperFrames 重新成片验收。浏览器运行时下载失败，未计为验证通过。

## 仍未完成

真实 Codex 读取新闻证据并制作成片的用户环境验收；自动 TTS 接入 Go APP、配音字幕导入为可编辑时间线；高质量剪辑、长文配音韵律和逐字对齐；文章版本、事件语义归并评测、全文检索/分页等原优化计划余项。自动发布仍是后续独立产品决策。
