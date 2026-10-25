# 0.1.1：第一阶段可靠性修复

基线：`1ec932c`。本版本保留 Python/FastAPI/SQLite 与现有 API，聚焦数据正确性和可安装性。

## 修复内容

- 重复文章沿原文章链复用已存在的事件；新事件不再在标题/正文签名之间切换。
- 文章、事件关联、事件更新使用同一事务。数据库失败先 rollback，再记录任务失败；并发相同文章的唯一键竞争视为已存在。
- 运行目录增加独立随机 ID，支持同秒运行。原始采集数据在过滤之前存档；补采不会倒退事件的最近发布时间。
- 调度触发用 UTC 分钟作为统一身份，并通过新增 `ScheduleOccurrence` 表的主键认领。同数据库的不同实例、或同一分钟重启后不会重复认领；单个任务失败不阻止其他任务。
- 查询条件先入 SQL，再排序限量；页面和 REST 共用查询服务；搜索中的 `%`、`_` 按普通字符处理。
- 来源白/黑名单、排除词在模型调用前执行。LLM 返回校验 ID、重复 ID、布尔值、类别和情绪字段；缺失项单独走规则兜底，有效拒绝判断仍然保留。
- REST `extra` 与 YAML 顶层扩展选项统一，核心字段不被 extra 覆盖；WebUI 尊重 `storage_root`。
- 服务工厂与 CLI 统一加载环境配置；V7 模型设置按操作读取，健康检查与生成使用一致鉴权。
- 页面模板、提示词和脚本模板纳入 wheel；素材准备器迁入包内，旧 tools 入口保留兼容。
- 服务端不再强制安装 PyQt/TTS；旧 LLM 和素材依赖通过 extras 声明。补充测试与 Linux/Windows、Python 3.10/3.12 CI 配置。

## 安装与运行

源码目录：

```bash
python -m pip install -e .
news2douyin v7-init --profile configs/v7/profiles/mock_market.yaml
news2douyin v7-run-now --profile-name mock_market
news2douyin-server --host 127.0.0.1 --port 18080
```

安装 wheel 时，将 `.` 换为 wheel 文件路径。所需可选功能另加 `llm`、`assets`、`tts`、`desktop` extras。

原本把 `prompts/` 或 `templates/douyin/` 当作自定义内容目录的配置仍有效：本地文件优先，内置默认资源从已安装包回退读取。自定义的其他目录不存在时仍报错，不会静默使用默认模板。

运行前的 `.env` 不覆盖已有进程环境变量；可用 `NEWS2DOUYIN_ENV_FILE` 指定文件。Python 最低版本明确为 3.10，与项目使用的运行时类型注解一致。

## 已有数据库

启动时只新增缺失的触发认领表，不自动删除或重写已有文章、事件和脚本。修复新的采集行为不会自动纠正历史上已拆分的事件。

历史修复工具默认只预览：

```bash
news2douyin-repair-events --db runs_v7/news2douyin_v7.db
```

停止服务与采集任务、检查预览后应用：

```bash
news2douyin-repair-events --db runs_v7/news2douyin_v7.db --apply
```

应用前通过 SQLite backup API 生成备份。工具只沿明确的原文章链修正链接并更新计数/时间范围；缺失、循环或歧义关系会列入 skipped。旧 Event 行和 ScriptPackage 快照保留，因此可能出现文章数为 0 的历史事件；本版不删除它们。

## 验证方式

本次本地验证：Python 3.12，34 项回归测试通过；独立虚拟环境中的
8 项 wheel 冒烟检查通过。测试涵盖两个采集器同时写同一文章、两个
调度实例竞争、历史事件修复备份、事务故障和 API/UI 查询一致性。
真实新闻源、LLM、TTS 与 Windows 运行尚未在本次会话中验证。

```bash
python -m pip install -e ".[dev,assets,llm]"
python -m pytest -q
python -m build
```

发布包另在干净虚拟环境安装，再运行：

```bash
python -I /path/to/checkout/tools/smoke_installed.py
```

该脚本切换至临时目录，验证首页、Mock 采集、脚本包、内置提示词/模板、旧 LLM 客户端导入、包内素材进程和 CLI。安装环境需包含 dev、assets、llm extras；测试不调用真实新闻、模型或 TTS 服务。

## 保留到下一阶段的边界

- 调度认领提供同一 UTC 分钟的持久化去重，不等同于 exactly-once 工作流。进程被强制终止后可能留下 claimed/running，本版不自动恢复或重试；持久化队列、租约、取消与阶段恢复属于下一阶段。
- API run-now 仍同步等待；多个不同新闻同时归并的完整语义和自动事件合并/拆分仍需后续事件服务处理。
- Cron 仍使用原项目的数字、范围、列表和 `*/n` 子集解析器；复杂 Cron 表达式尚未标准化。
- 旧报告格式、脚本模板内容质量、文章去重与事件聚类的语义拆分、视频制作闭环未在本版实现。
- V7 模型请求仍沿用原有服务的 `chat_template_kwargs`；对其他提供方的能力协商、无 `/models` 服务的探测策略留待客户端统一改造。
- CI 工作流已加入，但本地测试通过不代表 GitHub 的跨平台任务已经运行。
