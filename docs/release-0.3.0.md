# 0.3.0：脚本编辑与审核工作台

本版打通“事件 → 脚本草稿 → 人工编辑 → 版本审核 → 制作包导出”。继续使用 Python 服务与 HTML/JavaScript 页面，不增加前端构建或 Node.js 运行依赖。包含 0.1.1 的可靠性修复和 0.2.0 的后台采集任务。

## 页面流程

1. 打开事件页，点击“生成脚本草稿”，进入脚本工作台。
2. 编辑标题、口播正文、目标时长、画面/素材提示和编辑备注；在右侧查看资料摘录、打开原文并选择本稿使用的来源。
3. 保存新版本，可填写修改说明。版本历史可预览旧稿，并“恢复为新草稿”；已有历史不被覆盖。
4. 提交审核，核对来源与口播内容后通过；退回修改需要填写原因。
5. 当前版本通过后可导出 ZIP。历史导出仍可下载，内容不会随新稿变化。

脚本初稿使用已选来源中的资料摘录生成模板，不调用 LLM，不自动翻译或执行事实验证。原来固定的“今天”和交易建议模板已改为中性资料草稿，避免在历史补采时默认写成当天新闻。profile_name 保持兼容元数据用途，本版没有新增按 profile 区分的生成策略。

目标时长只是制作计划，不能据此断言真实配音时长。画面提示用于后续找素材或制作分镜；本版不自动下载素材、配音、生成字幕时码或渲染视频。

## 版本与审核

- `revision` 是内容版本号，初稿为 1；每次有效编辑或历史恢复增加 1。
- `version` 是并发控制号，内容变化和审核状态变化都会增加。保存、恢复、审核和导出请求必须提供读到的 `expected_version`。
- 两个页面同时编辑时，较旧请求返回 `409`，不覆盖新版本。页面保留输入，可先“保存本地副本”，再加载最新版本手动合并。
- 审核流程为 `draft → in_review → approved`；退回或编辑进入 `draft`，清除当前通过状态。没有内容变化的保存不会增加版本或撤销审核。
- 恢复旧稿会创建新版本，即使恢复的是曾通过审核的内容，也要重新审核。
- 通过审核需要至少选择一个已有来源，并显式确认已核对来源和口播。
- 审核记录包含对应内容版本、状态版本、动作、备注和署名。署名是手工备注，不代表已认证身份；本版没有新增登录、人员权限或双人审核约束。

离开有未保存修改的页面时会提示。保存、审核等写请求进行期间暂停编辑输入，避免响应返回后覆盖请求期间的新输入。页面不做自动合并或后台自动保存，本地副本是供保留/手动合并的 JSON 文件。

## 来源快照与兼容

生成脚本时最多保存 10 条关联来源，优先非重复报道，再按发布时间和文章键稳定排序。同一文章的重复关联只保留一次。快照包含文章键、标题、URL、来源域名、发布时间、最多 2000 字符的摘录和重复报道标记。

编辑和恢复沿用快照，后续新闻库更新不会悄悄改写脚本依据。来源选择只允许使用该快照内的文章键。人工审核仍需打开原文核对；重复报道数量不代表独立证据数量。

新旧 API 中的 `script_text`、`script_json`、`profile_name`、`output_dir`、`tts_status` 等字段保留。编辑后的 ScriptPackage 指向当前修订的独立目录，`script.txt` 和 `script.json` 与当前版本一致；旧目录不覆盖。编辑会把 tts_status 重置为 pending，避免把旧配音状态当成新稿结果。

旧 ScriptPackage 可以直接阅读，GET 不隐式迁移。点击“启用版本管理”后保留原稿作为第一版，并从当前事件补充缺失的来源身份信息；快照明确标为 `legacy_adopted_now`。旧文件保留。若事件已不存在或没有来源，仍可阅读编辑，但无法通过带来源要求的审核，应从有来源的事件重新生成脚本。

## HTTP API

| 方法与路径 | 用途 |
|---|---|
| `POST /api/scripts/build` | 从 event_key 生成脚本；原有字段保留，新增状态和编辑文档 |
| `GET /api/scripts?event_key=...&limit=50` | 列表，event_key 可省略，最多 200 条 |
| `GET /api/scripts/{key}` | 当前文档、状态、版本和来源快照 |
| `PUT /api/scripts/{key}` | 保存 document，必须提供 expected_version，可附 change_note |
| `POST /api/scripts/{key}/adopt` | 显式启用旧稿版本管理；重复调用返回当前稿 |
| `GET /api/scripts/{key}/revisions` | 按版本倒序；支持 before 和 limit |
| `GET /api/scripts/{key}/revisions/{revision}` | 查看完整历史快照 |
| `POST /api/scripts/{key}/revisions/{revision}/restore` | 以旧版本创建新草稿，带 expected_version |
| `POST /api/scripts/{key}/review` | submit / approve / request_changes，带 expected_version |
| `GET /api/scripts/{key}/reviews` | 审核与编辑记录，支持 before（记录 ID）和 limit |
| `POST /api/scripts/{key}/exports` | 导出当前通过的版本，带 expected_version；重复请求返回同一个导出 |
| `GET /api/scripts/{key}/exports` | 查看历史导出 |
| `GET /api/scripts/{key}/exports/{export_key}/download` | 校验归档后下载 ZIP |

`document` 包含：

```json
{
  "title": "稿件标题",
  "script_text": "完整口播正文",
  "target_duration_sec": 60,
  "visual_notes": "画面与素材提示",
  "notes": "编辑备注",
  "source_keys": ["来源文章的 article_key"]
}
```

审核请求示例：

```json
{
  "expected_version": 3,
  "action": "approve",
  "reviewer": "编辑备注名",
  "note": "已核对日期和数字",
  "sources_checked": true,
  "wording_checked": true
}
```

`404` 表示包或版本不存在；`409` 表示版本冲突、状态不允许或归档校验失败；`422` 表示输入校验失败；`410` 表示已登记的归档文件丢失。脚本内容上限 50,000 字符、标题 300 字符，目标时长范围 10–600 秒。

Python SDK 增加脚本列表、保存、版本查询/恢复、审核、导出和旧稿接管方法。SDK 的导出方法返回下载地址及 SHA-256，不隐式下载或发布文件。

## 制作包格式 v1

| 文件 | 内容 |
|---|---|
| `script.txt` | 当前审核版本的口播正文 |
| `script.md` | 标题、正文、画面提示、所选来源；HTML 字符转义 |
| `script.json` | 文档和完整来源快照，并保留旧顶层 script_text/editorial/profile_name 字段 |
| `revision.json` | 精确的规范化版本快照，用于计算 content_hash |
| `sources.json` | 本稿勾选的来源 |
| `assets_manifest.json` | 尚未分配的图片/视频清单和画面提示 |
| `manifest.json` | schema_version、包/事件/版本标识、审核记录、目标时长、文件 SHA-256 |

`manifest.json` 的 `kind` 为 `news2douyin.script_package`、`schema_version` 为 1。`content_hash` 等于 revision.json 文件字节的 SHA-256；`files` 给出其他文件（不含 manifest 自身）的 SHA-256。下载接口的 ETag 和导出记录的 archive_sha256 对应整个 ZIP。后续 Go 视频应用可以读取这些 JSON，不依赖 Python 内部对象。

审核只表明导出时的该版本通过了人工流程。旧 ZIP 不因新稿修改或退回而被撤销或改写，下载历史包时应核对版本号。

## 升级与持久化

1. 停止服务，备份数据库和整个 storage_root。
2. 安装 0.3.0，按原 db_url、storage_root 启动。
3. 启动时新增 ScriptState、ScriptRevision、ScriptReviewEvent、ScriptExport 四张表，不修改旧表列，不批量改写旧稿。
4. 从一个已有事件生成草稿，保存、审核并下载，确认旧采集任务和运行记录仍可访问。

保存采用数据库条件更新，版本、当前指针和审核记录在同一事务提交。每次修订与导出使用独立目录/文件，只有数据库已提交的记录对外可见。普通提交失败会清理本次新文件；进程突然崩溃可能留下未登记的目录/ZIP，不自动复用或显示。数据库和文件系统不是跨介质事务，备份应一起进行。内容快照保存在数据库中，归档下载会重新验证文件 SHA-256；缺失/损坏的归档不会伪装为可用结果。

本版未添加自动清理历史版本或素材文件的策略。多进程需要访问同一数据库和存储根目录，仍以可信内网/本机的 SQLite 部署为验证范围。

## 验证与后续范围

- 本地 Python 3.12：72 项自动化测试；新增 17 项覆盖版本不可变、来源快照、并发保存、过期审核、审核失效、恢复、来源约束、导出校验与去重、文件/数据库失败清理、提交后读错误保留文件、旧稿接管、接口及页面转义。
- 独立 wheel：16 项离线冒烟检查，新增工作台页面、编辑审核、制作包下载、恢复历史版本。
- 新旧稿页面 JavaScript 语法检查通过；未做真实浏览器交互验收、远程 CI 或生产吞吐测试。
- 没有连接真实新闻/模型服务，没有执行视频渲染或发布。

下一阶段可用这个制作包作为固定输入，接入素材管理、配音、字幕对齐和视频渲染任务；Rundesk/Codex 与 Go 视频 APP 的接入仍未实现。
